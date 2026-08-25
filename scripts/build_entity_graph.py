#!/usr/bin/env python3
"""Build entity relationship graph from entities_role fields.

Parses the semi-structured entities_role text (e.g. "A=判断+执行; B=审查")
into graph tables: nodes, edges, and co-occurrence.

Deterministic — no LLM needed. Run after solidification or manually:
    python3 scripts/build_entity_graph.py

The graph is rebuilt from scratch each run (idempotent).
"""

import json
import re
import sqlite3
import sys
from pathlib import Path

# ── Config ──
DEFAULT_DB = Path.home() / "memory" / "mcp_memory.db"

# Known persons for relay-chain fan-out (canonical()). Comma-separated env
# override: KNOWN_PERSONS="甜心,照照,洄洄". Keep in sync with server.py's
# KNOWN_PERSONS config.
import os
_KNOWN = os.environ.get(
    "KNOWN_PERSONS",
    "甜心,照照,洄洄,鸣鸣,ZCode,Kimi3,Eve,Tim King",
)
KNOWN_PERSONS = {p.strip() for p in _KNOWN.split(",") if p.strip()}


def init_graph_tables(c: sqlite3.Connection):
    """Create graph tables if they don't exist."""
    c.executescript("""
        CREATE TABLE IF NOT EXISTS graph_nodes (
            entity TEXT PRIMARY KEY,
            mention_count INTEGER DEFAULT 0,
            first_seen TEXT,
            last_seen TEXT
        );

        CREATE TABLE IF NOT EXISTS graph_edges (
            entity_a TEXT,
            entity_b TEXT,
            narrative_id INTEGER,
            role_a TEXT,
            role_b TEXT,
            created_at TEXT,
            FOREIGN KEY (narrative_id) REFERENCES narratives(id)
        );

        CREATE TABLE IF NOT EXISTS graph_cooccur (
            entity_a TEXT,
            entity_b TEXT,
            cooccur_count INTEGER DEFAULT 0,
            PRIMARY KEY (entity_a, entity_b)
        );

        CREATE INDEX IF NOT EXISTS idx_graph_edges_a ON graph_edges(entity_a);
        CREATE INDEX IF NOT EXISTS idx_graph_edges_b ON graph_edges(entity_b);
        CREATE INDEX IF NOT EXISTS idx_graph_edges_narrative ON graph_edges(narrative_id);
    """)


def parse_entities_role(text: str) -> dict:
    """Parse entities_role text into {entity: role_description}.

    Handles formats like:
        "A=判断+执行; B=审查"
        "甜心(用户)=解释为什么做; 洄=观察协作模式"
        "A=ssh救援; B=架构判断; C=在挂"
        "甜心（用户）→ 需求拆解；洄洄（云实例）→ 写代码；照照（本地实例）→ review"  (v2: 全角；/→)
        "甜心引入→照照主动接触→Kimi3回应"  (v2: 链式事件流, 共现, 角色留空)

    v2 changes (2026-08-26, 照照 — fix: 47/47 production entities_role rows
    matched ZERO of the old patterns, graph tables stayed empty for months):
      1. separators: ';' also '；'
      2. assignment: '=' also '→'
      3. chain format (no separator, arrows between clauses): treat as
         co-occurrence — entities enter the graph, roles left empty
      4. name-length cap 12 chars: longer "names" are unparseable prose,
         dropped rather than becoming garbage nodes
      5. canonical(): a name fragment containing multiple known persons
         fans out to all of them ("甜心转述洄洄修复消息" → 甜心, 洄洄) —
         the messenger AND the letter-writer both get credited
    """
    if not text or not text.strip():
        return {}

    MAX_NAME_LEN = 12
    result = {}
    text = text.strip()

    # 形态1: ；或; 分隔的段，每段 实体=角色 或 实体→角色
    seg_pat = re.compile(r"^(.{1,%d}?)[（(]?\s*(?:→|=)\s*(.+)$" % MAX_NAME_LEN)
    if re.search(r"[；;]", text):
        for part in re.split(r"[；;]", text):
            part = part.strip()
            if not part:
                continue
            m = seg_pat.match(part)
            if m:
                name = re.sub(r"\s*[（(][^)）]*[)）]\s*", "", m.group(1)).strip()
                role = m.group(2).strip()
                for ent in canonical(name, MAX_NAME_LEN):
                    result[ent] = role
        return result

    # 形态2: 链式 实体A动作→实体B动作→实体C动作 —— 共现，角色留空
    if text.count("→") >= 1:
        entities = set()
        for seg in (s.strip() for s in text.split("→")):
            known = [k for k in KNOWN_PERSONS if k in seg]
            if known:
                entities.update(known)
            else:
                m = re.match(r"^([\u4e00-\u9fa5A-Za-z0-9 ]{1,6}?)(?:主动|发现|提出|设计|写|修|审|转|否|采|接|回|承|指|推|做)", seg)
                if m and m.group(1).strip():
                    entities.add(m.group(1).strip())
        if entities:
            return {e: "" for e in entities}
    return {}


def canonical(name: str, max_len: int = 12) -> list:
    """Name fragment → canonical entity list.

    Fragment containing several known persons fans out to all of them
    (relay-chain credit: messenger + letter-writer both recorded).
    Unknown fragments: keep if short (dream figures like 当铺掌柜),
    drop if long (unparseable prose)."""
    found = [k for k in KNOWN_PERSONS if k in name]
    if found:
        return found
    return [name] if 0 < len(name) <= max_len else []


def rebuild_graph(db_path: Path):
    """Rebuild the entire entity graph from narratives."""
    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=30000")

    # Clear existing graph data
    db.executescript("""
        DELETE FROM graph_nodes;
        DELETE FROM graph_edges;
        DELETE FROM graph_cooccur;
    """)

    # Fetch all narratives with entities_role
    rows = db.execute("""
        SELECT id, entities_role, created_at
        FROM narratives
        WHERE entities_role IS NOT NULL AND entities_role != ''
        ORDER BY created_at
    """).fetchall()

    # Accumulate data
    node_mentions: dict[str, int] = {}
    node_first: dict[str, str] = {}
    node_last: dict[str, str] = {}
    edges: list[tuple] = []
    cooccur: dict[tuple[str, str], int] = {}

    for r in rows:
        parsed = parse_entities_role(r["entities_role"])
        if not parsed:
            continue

        entities = list(parsed.keys())
        ts = r["created_at"]

        for ent in entities:
            node_mentions[ent] = node_mentions.get(ent, 0) + 1
            if ent not in node_first:
                node_first[ent] = ts
            node_last[ent] = ts

        # Create edges for all pairs in this narrative
        for i, a in enumerate(entities):
            for j, b in enumerate(entities):
                if i >= j:
                    continue
                edges.append((a, b, r["id"], parsed[a], parsed[b], ts))

                # Co-occurrence (sorted pair as key)
                pair = tuple(sorted([a, b]))
                cooccur[pair] = cooccur.get(pair, 0) + 1

    # Write nodes
    for ent, count in sorted(node_mentions.items(), key=lambda x: -x[1]):
        db.execute("""
            INSERT INTO graph_nodes (entity, mention_count, first_seen, last_seen)
            VALUES (?, ?, ?, ?)
        """, (ent, count, node_first.get(ent), node_last.get(ent)))

    # Write edges
    for e in edges:
        db.execute("""
            INSERT INTO graph_edges (entity_a, entity_b, narrative_id, role_a, role_b, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, e)

    # Write co-occurrence
    for (a, b), count in sorted(cooccur.items(), key=lambda x: -x[1]):
        db.execute("""
            INSERT INTO graph_cooccur (entity_a, entity_b, cooccur_count)
            VALUES (?, ?, ?)
        """, (a, b, count))

    db.commit()

    # Report
    print(f"✅ Entity graph rebuilt from {len(rows)} narratives")
    print(f"   Nodes: {len(node_mentions)}")
    print(f"   Edges: {len(edges)}")
    print(f"   Co-occurrence pairs: {len(cooccur)}")
    print()
    print("Top nodes:")
    for ent, count in sorted(node_mentions.items(), key=lambda x: -x[1])[:10]:
        print(f"   {ent}: {count} mentions")
    print()
    print("Top co-occurrences:")
    for (a, b), count in sorted(cooccur.items(), key=lambda x: -x[1])[:10]:
        print(f"   {a} ↔ {b}: {count} times")

    db.close()


if __name__ == "__main__":
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DB
    if not db_path.exists():
        print(f"❌ Database not found: {db_path}")
        sys.exit(1)

    # Ensure tables exist
    db = sqlite3.connect(str(db_path))
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=30000")
    init_graph_tables(db)
    db.close()

    rebuild_graph(db_path)
