#!/usr/bin/env python3
"""
Conflict Candidates Scanner — 发现层（确定性，零LLM，零并发）

两层设计的发现层（2026-09-01 甜心批准）：
  发现层（本脚本）：SQL+正则捞「新旧断言打架」嫌疑对 → conflict_candidates 审计清单
  裁决层：DREAM 固化夜车多看一眼清单——能判的按家规改写，判不了的记 threads

纪律：
  - 标签不落记忆本体——嫌疑对落在独立审计表 conflict_candidates，记忆零污染
  - 幂等：同一 pair 不重复入库
  - 支持 MEMORY_MCP_DB 环境变量（隔离测试用）

Confidence 分层：
  high   = 推翻型信号词命中（新条显式声明旧状态作废）+ related_entities 交集
  medium = 实体交集≥2 + tags 交集≥2 + 时间差>14天（无显式推翻词，靠结构重叠）
"""

import os
import sys
import json
import re
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

DB_PATH = os.environ.get("MEMORY_MCP_DB", str(Path.home() / "memory" / "mcp_memory.db"))

# 推翻型信号词——正则只干它干得动的：抓「这条显式声明旧状态作废」的句式
OVERTURN_PATTERN = re.compile(
    r"不再|已经不是|不再是|取消|推翻|翻案|改口|其实不是|勘误|过时|过期|证伪|坐实是错|实际是错"
)

# 近全库共有的泛化词——实体和tags里都一样，不能当「同一件事」的证据
GENERIC_WORDS = {"洄", "甜心", "用户", "user", "我", "self"}

MIN_SPECIFIC_TAGS_HIGH = 2  # 真案采样：#218⟂#217(共读进度+海蜃馆)、#227⟂#226(叙述陷阱+青丝断)都≥2个具体tag
MIN_ENTITY_OVERLAP_MEDIUM = 2
MIN_TAG_OVERLAP_MEDIUM = 2
MIN_DAYS_GAP_MEDIUM = 14
MAX_PAIRS_PER_NEW = 5      # 同一旧断言被推翻通常1-3条；超过=词太泛的信号


def _parse_json_field(raw):
    if not raw:
        return set()
    try:
        vals = json.loads(raw)
        if isinstance(vals, list):
            return {str(v) for v in vals if v}
    except (json.JSONDecodeError, TypeError):
        pass
    return set()


def _parse_ts(ts):
    if not ts:
        return None
    ts = str(ts).replace("Z", "+00:00")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(ts[:26] if "." in ts else ts[:19], fmt)
        except ValueError:
            continue
    return None


def ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS conflict_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            narrative_id_new INTEGER NOT NULL,
            narrative_id_old INTEGER NOT NULL,
            confidence TEXT NOT NULL,          -- high / medium
            reason TEXT NOT NULL,              -- 命中信号描述
            status TEXT DEFAULT 'open',        -- open / resolved / parked / dismissed
            note TEXT DEFAULT '',
            detected_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_conflict_pair ON conflict_candidates(narrative_id_new, narrative_id_old)")
    conn.commit()


def load_narratives(conn):
    rows = conn.execute(
        "SELECT id, gesture, context_layer, cognition_direction, tags, related_entities, created_at FROM narratives"
    ).fetchall()
    items = []
    for rid, gesture, ctx, cog, tags, ents, created in rows:
        text = " ".join(t for t in [gesture, ctx, cog] if t)
        items.append({
            "id": rid,
            "text": text,
            "tags": _parse_json_field(tags),
            "entities": _parse_json_field(ents),
            "ts": _parse_ts(created),
            "created_at": created,
        })
    items.sort(key=lambda x: (x["ts"] is None, x["ts"]))
    return items


def detect(items):
    """返回 [(new_id, old_id, confidence, reason)] 候选列表。"""
    candidates = []
    seen_pairs = set()

    for i, new in enumerate(items):
        new_hits = OVERTURN_PATTERN.findall(new["text"] or "")
        pair_count = 0

        for old in items[:i]:  # 只跟更早的比
            if new["id"] == old["id"]:
                continue
            ent_overlap = new["tags"] and old["tags"] and (new["entities"] & old["entities"])
            tag_overlap = new["tags"] & old["tags"]
            pair = (new["id"], old["id"])
            if pair in seen_pairs:
                continue
            if pair_count >= MAX_PAIRS_PER_NEW:
                break  # 该new的配额满，实体太泛的信号

            # high：显式推翻词 + 实体交集 + 具体tags交集≥2（「同类断言」的 proxy）
            if ent_overlap and new_hits:
                specific_tags = tag_overlap - GENERIC_WORDS
                specific_ents = ent_overlap - GENERIC_WORDS
                if len(specific_tags) >= MIN_SPECIFIC_TAGS_HIGH or specific_ents:
                    reason = f"推翻词{new_hits[:2]}+实体{sorted(ent_overlap)[:3]}+tags{sorted(tag_overlap)[:3]}"
                    candidates.append((new["id"], old["id"], "high", reason))
                    seen_pairs.add(pair)
                    pair_count += 1
                    continue

            # medium：结构重叠（无推翻词，靠实体+tags+时间跨度）
            if (len(ent_overlap) >= MIN_ENTITY_OVERLAP_MEDIUM
                    and len(tag_overlap) >= MIN_TAG_OVERLAP_MEDIUM
                    and new["ts"] and old["ts"]):
                gap_days = (new["ts"] - old["ts"]).days
                if gap_days >= MIN_DAYS_GAP_MEDIUM:
                    reason = f"实体{len(ent_overlap)}重叠+tags{sorted(tag_overlap)[:3]}+间隔{gap_days}天"
                    candidates.append((new["id"], old["id"], "medium", reason))
                    seen_pairs.add(pair)

    return candidates


def insert_candidates(conn, candidates):
    """幂等入库，返回新插入条数。"""
    inserted = 0
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    for new_id, old_id, confidence, reason in candidates:
        cur = conn.execute(
            "INSERT OR IGNORE INTO conflict_candidates (narrative_id_new, narrative_id_old, confidence, reason, detected_at) VALUES (?,?,?,?,?)",
            (new_id, old_id, confidence, reason, now),
        )
        inserted += cur.rowcount
    conn.commit()
    return inserted


def main():
    dry_run = "--dry-run" in sys.argv
    conn = sqlite3.connect(DB_PATH)

    if not dry_run:
        ensure_table(conn)
    items = load_narratives(conn)
    candidates = detect(items)

    open_before = 0
    if not dry_run:
        open_before = conn.execute("SELECT COUNT(*) FROM conflict_candidates WHERE status='open'").fetchone()[0]

    inserted = 0 if dry_run else insert_candidates(conn, candidates)

    # 报告
    high = [c for c in candidates if c[2] == "high"]
    medium = [c for c in candidates if c[2] == "medium"]
    print(f"## conflict_candidates 扫描报告")
    print(f"- narratives 总量: {len(items)}")
    print(f"- 本轮候选: {len(candidates)} (high={len(high)}, medium={len(medium)})")
    print(f"- 新入库: {inserted}" + ("（dry-run 未入库）" if dry_run else ""))
    if not dry_run:
        open_now = conn.execute("SELECT COUNT(*) FROM conflict_candidates WHERE status='open'").fetchone()[0]
        print(f"- 清单待判(open): {open_before} → {open_now}")
    print()
    for new_id, old_id, conf, reason in sorted(candidates, key=lambda c: c[2])[:20]:
        print(f"  [{conf}] #{new_id} ⟂ #{old_id} — {reason}")
    if len(candidates) > 20:
        print(f"  …另有 {len(candidates)-20} 条，查 conflict_candidates 表")

    conn.close()


if __name__ == "__main__":
    main()
