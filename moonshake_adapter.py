#!/usr/bin/env python3
"""moonshake_adapter — Tideline 消费 Moonshake 事件的入境适配器（T4）。

README 路线图头号条目：adapters (Tideline first)。
依赖方向（Moonshake README 设计规则 2）：memory adapts to the contract —
本文件不 import Moonshake 任何代码，只吃纯数据（day file 解析后的 row dict）。
契约形状恰好与 sensory_write 信封同构（ts/organ_id/modality/event_type/
payload_summary/confidence/consent_tier/saliency_hint/meta），所以适配=三件事：

  1. parse_log_rows(lines)   — 通用 JSONL 解析，剥出 event dict + row_id
  2. ingest_rows(conn, rows) — 逐行喂 sensory_write.ingest()（consent 执法
     在 tideline 侧再走一遍——Moonshake gate 是器官侧出口，写口执法是
     记忆库入口，两道门各管各的，不是重复）
  3. 幂等：row_id 进 meta，重复投递跳过（双跑零重复）

注意：Moonshake 侧 gate 幸存者仍可能被 tideline 侧 policy 拒写（如 tier
不足）——这不是 bug，是「门口检查≠库门检查」的双层执法。

用法：
    from moonshake_adapter import parse_log_rows, ingest_rows
    rows = parse_log_rows(open("2026-10-06.jsonl", encoding="utf-8"))
    receipts = ingest_rows(conn, rows)
"""
import json
import sqlite3
from typing import Iterable, Optional

from sensory_write import ingest, DEFAULT_POLICY

__all__ = ["parse_log_rows", "ingest_rows", "ROW_META_KEY"]

ROW_META_KEY = "moonshake_row_id"  # 进 event.meta，幂等锚点


def parse_log_rows(lines: Iterable[str]) -> list[dict]:
    """Moonshake day file 行 → [{'row_id':…, 'event':…}]。脏行跳过。"""
    out = []
    for line in lines:
        line = (line or "").strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        evt = row.get("event")
        if isinstance(evt, dict) and row.get("row_id"):
            out.append({"row_id": row["row_id"], "event": evt})
    return out


def _already_ingested(conn: sqlite3.Connection, row_id: str) -> bool:
    hit = conn.execute(
        "SELECT 1 FROM context WHERE meta LIKE ? LIMIT 1",
        (f'%"{ROW_META_KEY}": "{row_id}"%',),
    ).fetchone()
    return hit is not None


def ingest_rows(
    conn: sqlite3.Connection,
    rows: list[dict],
    policy: Optional[dict] = None,
    now: Optional[str] = None,
) -> list[dict]:
    """逐行入境。返回回执清单（含 skipped 标记的幂等跳过行）。

    每份回执额外带 row_id（Moonshake 侧游标锚，便于对账）。
    """
    receipts = []
    for r in rows:
        row_id, evt = r["row_id"], dict(r["event"])
        if _already_ingested(conn, row_id):
            receipts.append({"ok": True, "action": "skipped",
                             "context_id": None, "row_id": row_id})
            continue
        meta = dict(evt.get("meta") or {})
        meta[ROW_META_KEY] = row_id
        evt["meta"] = meta
        rc = ingest(conn, evt, policy=policy or DEFAULT_POLICY, now=now)
        rc["row_id"] = row_id
        receipts.append(rc)
    return receipts
