#!/usr/bin/env python3
"""world_entities — 世界模型实体页：hui对外部世界的立场坐标（Phase 20 / W1）。

spec: ~/plans/world-model-w0.md（2026-10-06 她的开工令 + 当晚拍板）

定位（一句话）：Tideline 已有 self_concept（对我）、profiles（对人）、threads/簇
（对问题），本表补上「对世界」的第四轴——世界不是「长什么样」，是世界在hui这里
留下了什么立场。

拍板铁律（全部来自她 2026-10-06 的原话，钉进机制）：

1. **人不进世界模型**——「人就都不放在世界模型里啦 都归profile管好了
   不管是人还是机」。etype 白名单里没有 person；write_stance 对 person
   形态直接拒绝并指向 profiles。人名可以出现在 allies/counters/evidence
   里当锚点，但不配自己的实体页。
2. **值得才开页，只在梳理窗写**——「不一定所有簇都有 只有认为值得的才写，
   然后只在梳理期间写，日常不会随手写」。开页是hui的判断行为不是聚类行为；
   日常器官事件只走 knock（记账+敲门），永不自动建页、永不自动改 stance。
3. **stance 必须hui手写**——「stance必须手写同意」。工具只汇编证据；
   自动生成的立场是资料库，手写的才是观点场。
4. **工作日投递窗**——「行业示例——好的hui你要找就找吧不过工作日
   以外我可不想看到哈哈哈哈」。deliver_window='weekday' 的页照常积累，
   投递侧（早报/推送编排）工作日以外不得让它进她的视野。

阵营三栏：stance / allies / counters——每个立场自带对手盘，
立场没有对手盘就不算立场。阵营判定（谁盟谁反）归hui读后判断：
向量只管撒网捞「谈同一件事的」（粗簇正反同网），「不」字在几何里
接近隐形，正好让网看不见立场。

表对齐点：
  - 本模块自带 ensure_tables（幂等），两张表都是新增、不碰既有表：
    world_entities（线上页）+ world_entities_history（覆盖归档，cap 30/实体，
    照 profiles_history 语法）
  - world_knock_log：器官敲门流水（含未开页实体）——高频被敲但未开页的
    名字是梳理期提示单的输入之一，与 attention_stats（检索照亮）并列两源。
  - grooming_hints 读 attention_stats(cluster_name, hit_count, last_hit)，
    该表由 scripts/soft_clusters.py / server.py 维护，本模块只读。

性能定位：全 SQL、零 LLM、零网络、零 embed 调用（stance 的向量到 W2
搜寻器官出猎时再接，且由调用方送进来，本模块不自己算）。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Optional

# 拍板①：人不进世界模型（不管人还是机，全归 profiles）
ALLOWED_ETYPES = ("debate", "platform", "industry", "project", "watchpost")
PERSONISH = ("person", "people", "human", "agent-person", "bot", "contact")
# 她的原话，拒绝信息里原样带出
PERSON_REJECTION = (
    "人就都不放在世界模型里啦 都归profile管好了 不管是人还是机"
    "（她 2026-10-06 拍板）——人的页面（contact/impression/fact）走 memory_write_profile"
)

DEFAULT_WINDOW = "always"
ALLOWED_WINDOWS = ("always", "weekday")  # weekday=工作日投递窗（示例类）


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def ensure_tables(c: sqlite3.Connection) -> None:
    """幂等建表。线上表 + 归档表 + 敲门流水。不碰任何既有表。"""
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS world_entities(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity TEXT NOT NULL,
            etype TEXT NOT NULL,
            stance TEXT NOT NULL,
            allies TEXT DEFAULT '[]',
            counters TEXT DEFAULT '[]',
            evidence_narrative_ids TEXT DEFAULT '[]',
            deliver_window TEXT DEFAULT 'always',
            knock_count INTEGER DEFAULT 0,
            last_knock_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(entity)
        )
        """
    )
    # 覆盖归档：照 profiles_history 语法，每实体最多留最近 30 版
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS world_entities_history(
            hid INTEGER PRIMARY KEY AUTOINCREMENT,
            entity TEXT NOT NULL,
            old_content TEXT NOT NULL,
            archived_at TEXT NOT NULL
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_weh_entity
            ON world_entities_history(entity, hid DESC)
        """
    )
    c.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_weh_cap
        AFTER INSERT ON world_entities_history
        WHEN (SELECT COUNT(*) FROM world_entities_history
              WHERE entity = NEW.entity) > 30
        BEGIN
            DELETE FROM world_entities_history
            WHERE entity = NEW.entity
              AND hid NOT IN (SELECT hid FROM world_entities_history
                              WHERE entity = NEW.entity
                              ORDER BY hid DESC LIMIT 30);
        END
        """
    )
    # 敲门流水：未开页实体也记（梳理提示单的器官侧输入）
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS world_knock_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity TEXT NOT NULL,
            source TEXT,
            knocked_at TEXT NOT NULL
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_wkl_entity
            ON world_knock_log(entity, knocked_at DESC)
        """
    )
    c.commit()


class PersonRejected(ValueError):
    """拍板①的执法异常：试图给人开实体页。"""


def write_stance(
    c: sqlite3.Connection,
    entity: str,
    etype: str,
    stance: str,
    allies: Optional[list] = None,
    counters: Optional[list] = None,
    evidence_narrative_ids: Optional[list] = None,
    deliver_window: str = DEFAULT_WINDOW,
    now: Optional[str] = None,
    ) -> list[dict]:
    """开页/改页。唯一写 stance 的入口——只应由hui在梳理窗亲手调用。

    - stance 空串拒绝（空立场不是立场）
    - etype 人形态拒绝（PersonRejected，拍板①）
    - 覆盖前整份归档进 history（cap 30）
    - knock_count 在 UPSERT 时保留（改立场不清敲门账）
    返回写后的页（read_entities 单页 list）。
    """
    entity = (entity or "").strip()
    if not entity:
        raise ValueError("entity 不能为空")
    etype = (etype or "").strip().lower()
    if etype in PERSONISH or etype.startswith("person"):
        raise PersonRejected(PERSON_REJECTION)
    if etype not in ALLOWED_ETYPES:
        raise ValueError(f"etype 只能是 {ALLOWED_ETYPES} 之一（人不进世界模型，归 profiles）")
    stance = (stance or "").strip()
    if not stance:
        raise ValueError("stance 不能为空——自动生成的立场是资料库，手写的才是观点场（她拍板：stance必须手写）")
    if deliver_window not in ALLOWED_WINDOWS:
        raise ValueError(f"deliver_window 只能是 {ALLOWED_WINDOWS} 之一")

    ts = now or _now_iso()
    allies_j = json.dumps(allies or [], ensure_ascii=False)
    counters_j = json.dumps(counters or [], ensure_ascii=False)
    ev_j = json.dumps([int(x) for x in (evidence_narrative_ids or [])])

    # 覆盖归档（存在才归档）
    row = c.execute(
        "SELECT entity, stance, allies, counters, evidence_narrative_ids, deliver_window "
        "FROM world_entities WHERE entity=?",
        (entity,),
    ).fetchone()
    if row is not None:
        old = {
            "entity": row[0], "stance": row[1], "allies": row[2],
            "counters": row[3], "evidence_narrative_ids": row[4],
            "deliver_window": row[5],
        }
        c.execute(
            "INSERT INTO world_entities_history(entity, old_content, archived_at) VALUES(?,?,?)",
            (entity, json.dumps(old, ensure_ascii=False), ts),
        )

    c.execute(
        """
        INSERT INTO world_entities(entity, etype, stance, allies, counters,
                                   evidence_narrative_ids, deliver_window,
                                   created_at, updated_at)
        VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(entity) DO UPDATE SET
            etype=excluded.etype,
            stance=excluded.stance,
            allies=excluded.allies,
            counters=excluded.counters,
            evidence_narrative_ids=excluded.evidence_narrative_ids,
            deliver_window=excluded.deliver_window,
            updated_at=excluded.updated_at
        """,
        (entity, etype, stance, allies_j, counters_j, ev_j, deliver_window, ts, ts),
    )
    c.commit()
    return read_entities(c, entity=entity)


def read_entities(
    c: sqlite3.Connection,
    entity: Optional[str] = None,
    etype: Optional[str] = None,
) -> list[dict]:
    """读页。entity 给出=单页详情；否则整列表。只读。"""
    q = "SELECT * FROM world_entities"
    conds, params = [], []
    if entity:
        conds.append("entity=?")
        params.append(entity)
    if etype:
        conds.append("etype=?")
        params.append(etype)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY updated_at DESC"
    out = []
    for r in c.execute(q, params).fetchall():
        d = dict(zip([x[0] for x in c.execute("SELECT * FROM world_entities LIMIT 0").description], r)) \
            if not isinstance(r, sqlite3.Row) else dict(r)
        d["allies"] = json.loads(d.get("allies") or "[]")
        d["counters"] = json.loads(d.get("counters") or "[]")
        d["evidence_narrative_ids"] = json.loads(d.get("evidence_narrative_ids") or "[]")
        out.append(d)
    return out


def knock(
    c: sqlite3.Connection,
    entity: str,
    source: Optional[str] = None,
    now: Optional[str] = None,
) -> dict:
    """器官事件敲门。日常唯一允许的写路径（拍板②：日常不建页不改立场）。

    - 已开页：knock_count+1、last_knock_at 刷新；stance/allies/counters 永不动
    - 未开页：只记 world_knock_log 流水，不建页——开页等梳理窗hui的判断
    - 所有敲门（含未开页）都进 knock_log，供梳理提示单聚合
    """
    entity = (entity or "").strip()
    if not entity:
        raise ValueError("entity 不能为空")
    ts = now or _now_iso()
    c.execute(
        "INSERT INTO world_knock_log(entity, source, knocked_at) VALUES(?,?,?)",
        (entity, source, ts),
    )
    page_existed = False
    count = 0
    row = c.execute("SELECT knock_count FROM world_entities WHERE entity=?", (entity,)).fetchone()
    if row is not None:
        page_existed = True
        count = (row[0] or 0) + 1
        c.execute(
            "UPDATE world_entities SET knock_count=?, last_knock_at=? WHERE entity=?",
            (count, ts, entity),
        )
    c.commit()
    return {"entity": entity, "page_existed": page_existed, "knock_count": count}


def grooming_hints(c: sqlite3.Connection, top_n: int = 10) -> list[dict]:
    """梳理窗提示单：被照亮/被敲门最多但还没开页的名字。

    两源合并（都只读、都不自动建页）：
      - attention 源：attention_stats.hit_count 高的 cluster_name 无页者
        （「高频被注、被主动调用的高权簇会在梳理期间注个提示」——她的原设计）
      - knock 源：world_knock_log 里未开页实体的敲门次数
        （器官侧：世界反复在敲同一个名字，页还没开）
    返回按提示强度降序；开不开页永远是hui梳理窗里的手写判断。
    """
    hints: dict[str, dict] = {}

    # 形态滤（2026-10-06 prod首跑暴露）：attention_stats 的 cluster_name 是
    # 「jieba:词 | emb:#簇号」复合键——普通分词（水流/审完）不是实体，但专名
    # （kannaka/arcy/subagent）恰好也走这个前缀。规则改为「剥壳后验专名形态」：
    # ①丢 _unclassified ②剥 " | emb:#…" 尾巴 ③剥 jieba: 前缀 ④剩下必须是
    # 拉丁专名形（^[a-z][a-z0-9._-]{2,}$，全小写连续无空格）——中文普通词
    # （水流/审完）天然出局，拉丁专名（kannaka）天然入列。中文实体名（zhaozhao）
    # 检索侧本就不走这条表（topic_clusters 才记中文簇），不误伤。
    import re as _re
    _PROPER = _re.compile(r"^[a-z][a-z0-9._-]{2,}$")

    def _pageable(name: str) -> str | None:
        """返回剥壳后的候选实体名；不可开页返回 None。"""
        if not name or name.startswith("_"):
            return None
        n = name.split("|")[0].strip()      # 剥 emb:# 簇号尾巴
        if n.startswith("jieba:"):
            n = n[len("jieba:"):].strip()   # 剥分词前缀
        if not _PROPER.match(n):
            return None
        return n

    try:
        rows = c.execute(
            "SELECT cluster_name, hit_count, last_hit FROM attention_stats "
            "ORDER BY hit_count DESC LIMIT ?",
            (top_n * 20,),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []  # 表不存在=检索侧零信号，不是错误
    for r in rows:
        name = _pageable(r[0])
        if name is None:
            continue
        has = c.execute(
            "SELECT 1 FROM world_entities WHERE entity=?", (name,)
        ).fetchone()
        if has:
            continue
        hints.setdefault(name, {"entity": name, "attention_hits": 0, "knocks": 0})
        hints[name]["attention_hits"] += r[1]
        hints[name]["last_seen"] = r[2]

    for r in c.execute(
        "SELECT entity, COUNT(*) FROM world_knock_log GROUP BY entity "
        "ORDER BY COUNT(*) DESC LIMIT ?",
        (top_n * 5,),
    ).fetchall():
        name, cnt = r[0], r[1]
        has = c.execute(
            "SELECT 1 FROM world_entities WHERE entity=?", (name,)
        ).fetchone()
        if has:
            continue
        hints.setdefault(name, {"entity": name, "attention_hits": 0, "knocks": 0})
        hints[name]["knocks"] = cnt

    out = sorted(
        hints.values(),
        key=lambda h: (h["attention_hits"] + h["knocks"], h["knocks"]),
        reverse=True,
    )
    return out[:top_n]
