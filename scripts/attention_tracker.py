#!/usr/bin/env python3
"""
Tideline v2.4 — Attention Tracker

Tracks which memory clusters get "lit up" during T1 semantic search.
Every time a narrative is retrieved, we log it. This builds an objective
attention distribution — mechanical, not self-reported.

Two tables:
- attention_log: every T1 hit (narrative_id, cluster_id, sim, timestamp)
- attention_stats: aggregated hit counts per cluster (updated incrementally)

Usage:
  python3 attention_tracker.py log <narrative_ids_json> <sim_scores_json>
  python3 attention_tracker.py stats [--days N]
  python3 attention_tracker.py heatmap [--days N]
  python3 attention_tracker.py init

Runs from T1 prefetch hook (called after semantic search completes).
Zero LLM cost — pure bookkeeping.
"""

import os, sys, json, sqlite3
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections import Counter, defaultdict

DB_PATH = os.environ.get("MEMORY_MCP_DB", str(Path.home() / "memory" / "mcp_memory.db"))

def _now():
    return datetime.now(timezone.utc).isoformat()

def _db():
    """Safe DB connection with WAL + busy timeout."""
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=5000")
    return c

def init_tables():
    """Create attention tracking tables. Idempotent."""
    c = _db()
    c.execute("""
        CREATE TABLE IF NOT EXISTS attention_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            narrative_id INTEGER NOT NULL,
            sim REAL,
            cluster_name TEXT,
            created_at TEXT NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS attention_stats (
            cluster_name TEXT PRIMARY KEY,
            hit_count INTEGER DEFAULT 0,
            last_hit TEXT,
            last_narrative_id INTEGER
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_attn_log_ts ON attention_log(created_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_attn_log_nid ON attention_log(narrative_id)")
    c.commit()
    c.close()
    print("attention_log + attention_stats tables ready.")

def log_hits(narrative_ids, sim_scores, cluster_map=None):
    """Log T1 retrieval hits. Called after each prefetch().
    
    Records cluster_name from BOTH topic_clusters (jieba) AND emb_clusters (embedding).
    Format: "jieba:session" or "emb:#3" to distinguish the two layers.
    
    Args:
        narrative_ids: list of narrative IDs that were retrieved
        sim_scores: list of similarity scores (same order)
        cluster_map: optional dict {narrative_id: cluster_name}
                     If None, looks up from both topic_clusters and emb_cluster_members.
    """
    c = _db()

    if cluster_map is None:
        # Build jieba cluster map
        jieba_map = {}
        rows = c.execute("SELECT cluster_name, narrative_ids FROM topic_clusters").fetchall()
        for r in rows:
            try:
                nids = json.loads(r["narrative_ids"])
                for nid in nids:
                    jieba_map[nid] = r["cluster_name"]
            except (json.JSONDecodeError, TypeError):
                pass
        
        # Build emb cluster map
        emb_map = {}
        rows = c.execute("""
            SELECT narrative_id, cluster_id FROM emb_cluster_members
        """).fetchall()
        for r in rows:
            emb_map.setdefault(r["narrative_id"], []).append(r["cluster_id"])
        
        cluster_map = {}
        for nid in narrative_ids:
            names = []
            if nid in jieba_map:
                names.append(f"jieba:{jieba_map[nid]}")
            if nid in emb_map:
                names.append(f"emb:#{','.join(str(c) for c in emb_map[nid])}")
            cluster_map[nid] = " | ".join(names) if names else "_unclassified"

    now = _now()
    for nid, sim in zip(narrative_ids, sim_scores):
        cname = cluster_map.get(nid, "_unclassified")
        c.execute(
            "INSERT INTO attention_log (narrative_id, sim, cluster_name, created_at) VALUES (?,?,?,?)",
            (nid, sim, cname, now)
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
    c.close()

def get_stats(days=7):
    """Return attention distribution for the last N days."""
    c = _db()
    
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    
    rows = c.execute("""
        SELECT cluster_name, 
               COUNT(*) as hits,
               AVG(sim) as avg_sim,
               MAX(created_at) as last_seen
        FROM attention_log
        WHERE created_at > ?
        GROUP BY cluster_name
        ORDER BY hits DESC
    """, (cutoff,)).fetchall()
    
    c.close()
    return [dict(r) for r in rows]

def format_heatmap(days=7):
    """Format attention distribution as a readable report for DREAM self_reflection."""
    stats = get_stats(days)
    if not stats:
        return f"# 注意力分布（最近{days}天）\n\n无数据。T1语义检索可能尚未记录命中。"
    
    total_hits = sum(s["hits"] for s in stats)
    lines = [f"# 注意力分布（最近{days}天，共{total_hits}次命中）\n"]
    
    for s in stats:
        pct = s["hits"] / total_hits * 100 if total_hits else 0
        bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
        avg_sim = f"{s['avg_sim']:.2f}" if s["avg_sim"] else "N/A"
        lines.append(f"  {s['cluster_name']:20s} {bar} {s['hits']:4d}次 ({pct:4.1f}%) avg_sim={avg_sim}")
    
    # Detect attention deserts (clusters in topic_clusters with 0 hits)
    c = _db()
    all_clusters = c.execute("SELECT cluster_name FROM topic_clusters").fetchall()
    c.close()
    lit = {s["cluster_name"] for s in stats}
    deserts = [r[0] for r in all_clusters if r[0] not in lit]
    
    if deserts:
        lines.append(f"\n  ⚠ 从未被照亮: {', '.join(deserts)}")
    
    return "\n".join(lines)

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"
    
    if cmd == "init":
        init_tables()
    elif cmd == "log":
        # Log hits from T1 retrieval
        nids = json.loads(sys.argv[2])
        sims = json.loads(sys.argv[3])
        log_hits(nids, sims)
        print(f"Logged {len(nids)} attention hits.")
    elif cmd == "stats":
        days = int(sys.argv[2]) if len(sys.argv) > 2 else 7
        stats = get_stats(days)
        for s in stats:
            avg = s['avg_sim'] if s['avg_sim'] else 0
            print(f"  {s['cluster_name']:20s} {s['hits']:4d} hits  avg_sim={avg:.2f}")
    elif cmd == "heatmap":
        days = int(sys.argv[2]) if len(sys.argv) > 2 else 7
        print(format_heatmap(days))
    else:
        print(f"Unknown command: {cmd}")
        print("Usage: init | log <nids_json> <sims_json> | stats [days] | heatmap [days]")
