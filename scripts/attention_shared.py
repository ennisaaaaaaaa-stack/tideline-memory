"""Shared attention tracking logic — used by both tideline_provider and server.

Single source of truth for logging narrative retrieval hits across all paths.
Both provider and MCP server import log_attention() from here.
"""
import json
import sqlite3
from datetime import datetime, timezone


def log_attention(c: sqlite3.Connection, scored_results: list, source: str = "t1_prefetch"):
    """Log retrieval hits for attention distribution tracking.

    Args:
        c: sqlite3 connection (with row_factory set)
        scored_results: list of (sim_score, row_with_id) tuples
        source: which path brought these narratives into model view:
            - t1_prefetch: T1 semantic search on each turn (active, query-driven)
            - t0_inject: T0 system_prompt_block prefetch pool (passive, weight-driven)
            - mcp_search: memory_search MCP tool (manual, query-driven)
            - mcp_recall: memory_recall MCP tool (browsing, not query-driven)
            - dream: DREAM layer retrieval
    """
    if not scored_results:
        return
    try:
        c.execute("""
            CREATE TABLE IF NOT EXISTS attention_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                narrative_id INTEGER NOT NULL,
                sim REAL,
                cluster_name TEXT,
                source TEXT DEFAULT 't1_prefetch',
                created_at TEXT NOT NULL
            )
        """)
        # Migration: add source column if missing (existing tables pre-v2.4)
        cols = {r[1] for r in c.execute("PRAGMA table_info(attention_log)").fetchall()}
        if "source" not in cols:
            c.execute("ALTER TABLE attention_log ADD COLUMN source TEXT DEFAULT 't1_prefetch'")

        c.execute("""
            CREATE TABLE IF NOT EXISTS attention_stats (
                cluster_name TEXT PRIMARY KEY,
                hit_count INTEGER DEFAULT 0,
                last_hit TEXT,
                last_narrative_id INTEGER
            )
        """)

        # Build cluster lookup from BOTH topic_clusters (jieba) AND emb_cluster_members
        jieba_map = {}
        rows = c.execute("SELECT cluster_name, narrative_ids FROM topic_clusters").fetchall()
        for r in rows:
            try:
                nids = json.loads(r["narrative_ids"])
                for nid in nids:
                    jieba_map[nid] = r["cluster_name"]
            except (json.JSONDecodeError, TypeError):
                pass

        emb_map = {}
        try:
            emb_rows = c.execute("SELECT narrative_id, cluster_id FROM emb_cluster_members").fetchall()
            for r in emb_rows:
                emb_map.setdefault(r["narrative_id"], []).append(r["cluster_id"])
        except sqlite3.OperationalError:
            pass  # emb tables not built yet

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for sim, r in scored_results:
            nid = r["id"]
            names = []
            if nid in jieba_map:
                names.append(f"jieba:{jieba_map[nid]}")
            if nid in emb_map:
                names.append(f"emb:#{','.join(str(ci) for ci in emb_map[nid])}")
            cname = " | ".join(names) if names else "_unclassified"
            c.execute(
                "INSERT INTO attention_log (narrative_id, sim, cluster_name, source, created_at) VALUES (?,?,?,?,?)",
                (nid, float(sim), cname, source, now)
            )
            c.execute("""
                INSERT INTO attention_stats (cluster_name, hit_count, last_hit, last_narrative_id)
                VALUES (?, 1, ?, ?)
                ON CONFLICT(cluster_name) DO UPDATE SET
                    hit_count = hit_count + 1,
                    last_hit = excluded.last_hit,
                    last_narrative_id = excluded.last_narrative_id
            """, (cname, now, nid))

        c.commit()
    except Exception:
        pass  # attention tracking should never break the calling path
