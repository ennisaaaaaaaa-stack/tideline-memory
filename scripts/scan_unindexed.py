#!/usr/bin/env python3
"""
Layer 0 — Solidification scanner (deterministic, no LLM)

Two-track detection of unindexed context:
  Track A: source_links — narratives with empty source_links = potentially unindexed
  Track B: timestamp gap — sync_turn entries after latest narrative = definitely unindexed

Output: markdown with conversation chunks for LLM to process.

Usage:
  python3 scan_unindexed.py              # scan + print markdown
  python3 scan_unindexed.py --json       # output as JSON
  python3 scan_unindexed.py --since "2026-08-05 00:00"
"""

import os, sys, json, sqlite3
from pathlib import Path
from datetime import datetime

DB_PATH = os.environ.get("MEMORY_MCP_DB", str(Path.home() / "memory" / "mcp_memory.db"))
GAP_MINUTES = 10
MIN_CHUNK_SIZE = 3


def _parse_ts(ts):
    """Parse timestamp string flexibly."""
    if not ts:
        return None
    ts = ts.replace("Z", "+00:00")
    for fmt in ["%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"]:
        try:
            return datetime.strptime(ts, fmt)
        except ValueError:
            continue
    return None


def _group_chunks(rows):
    """Group sync_turn rows into conversation chunks by time proximity."""
    if not rows:
        return []

    chunks = []
    current = [rows[0]]

    for i in range(1, len(rows)):
        prev = _parse_ts(rows[i-1]["created_at"])
        curr = _parse_ts(rows[i]["created_at"])
        if prev and curr:
            gap_min = (curr - prev).total_seconds() / 60
        else:
            gap_min = 0

        if gap_min <= GAP_MINUTES:
            current.append(rows[i])
        else:
            if len(current) >= MIN_CHUNK_SIZE:
                chunks.append(current)
            current = [rows[i]]

    if len(current) >= MIN_CHUNK_SIZE:
        chunks.append(current)

    return chunks


def scan(since=None, output_format="markdown"):
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=30000")

    # ── Track A: narratives with empty source_links ──
    # These are potentially unindexed — need LLM to check if their content
    # is already covered by existing narratives
    no_link_rows = c.execute(
        """SELECT id, content, created_at, source_links FROM narratives
           WHERE source_links IS NULL
              OR source_links = ''
              OR source_links = '[]'
           ORDER BY created_at ASC"""
    ).fetchall()

    # ── Track B: timestamp gap ──
    # sync_turn entries after latest narrative
    if since:
        start_ts = since
    else:
        row = c.execute("SELECT MAX(created_at) as latest FROM narratives").fetchone()
        start_ts = row["latest"] or "2000-01-01"

    gap_rows = c.execute(
        """SELECT id, content, created_at FROM context
           WHERE created_at > ?
             AND (content LIKE '%[USER]%' OR content LIKE '%[ASSISTANT]%')
           ORDER BY created_at ASC""",
        (start_ts,)
    ).fetchall()

    # ── Track C: narratives missing embeddings ──
    no_emb_rows = c.execute(
        """SELECT id, content, created_at FROM narratives
           WHERE embedding IS NULL OR embedding = ''
           ORDER BY created_at ASC"""
    ).fetchall()

    # ── Track D: context entries missing embeddings ──
    no_emb_ctx = c.execute(
        """SELECT COUNT(*) as cnt FROM context
           WHERE embedding IS NULL OR embedding = ''"""
    ).fetchone()["cnt"]

    # Build conversation chunks from gap_rows
    chunks = _group_chunks(gap_rows)
    total_chunked = sum(len(ch) for ch in chunks)

    c.close()

    if output_format == "json":
        result = {
            "track_a_empty_source_links": len(no_link_rows),
            "track_b_gap_entries": len(gap_rows),
            "track_b_chunks": len(chunks),
            "track_c_narratives_missing_embedding": len(no_emb_rows),
            "track_d_context_missing_embedding": no_emb_ctx,
            "chunks": []
        }
        for i, chunk in enumerate(chunks):
            result["chunks"].append({
                "chunk_id": i,
                "start": chunk[0]["created_at"],
                "end": chunk[-1]["created_at"],
                "entry_count": len(chunk),
                "context_ids": [r["id"] for r in chunk],
                "content": "\n".join(r["content"][:300] for r in chunk)
            })
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    # Markdown output
    print("# Layer 0 — Unindexed Context Scan")
    print()
    print("## Track A: narratives without source_links")
    print("Count: {} (these may need retrospective linking)".format(len(no_link_rows)))
    if no_link_rows and len(no_link_rows) <= 10:
        for r in no_link_rows:
            print("  [{}] {} | {}".format(
                r["id"],
                str(r["created_at"])[:19],
                str(r["content"])[:60]
            ))
    print()

    # Track C/D: embedding coverage
    print("## Track C/D: embedding coverage")
    print("Narratives missing embedding: {}".format(len(no_emb_rows)))
    print("Context entries missing embedding: {}".format(no_emb_ctx))
    if no_emb_rows:
        print("⚠️  These narratives are stored but NOT searchable via semantic retrieval!")
        for r in no_emb_rows[:5]:
            print("  [{}] {} | {}".format(
                r["id"],
                str(r["created_at"])[:19],
                str(r["content"])[:60]
            ))
        if len(no_emb_rows) > 5:
            print("  ... and {} more".format(len(no_emb_rows) - 5))
    print()

    print("## Track B: timestamp gap scan")
    print("Scanning from: {}".format(start_ts))
    print("Unindexed entries: {}".format(len(gap_rows)))
    print("Conversation chunks (>= {} entries): {}".format(MIN_CHUNK_SIZE, len(chunks)))
    print()

    for i, chunk in enumerate(chunks):
        print("---")
        print("## Chunk {} ({}) — {} entries".format(
            i,
            str(chunk[0]["created_at"])[:19],
            len(chunk)
        ))
        print("context_ids: [{}]".format(", ".join(str(r["id"]) for r in chunk)))
        print()
        for r in chunk:
            content = str(r["content"]).replace("\n", "\n  ")[:300]
            print("  {}".format(content))
        print()


if __name__ == "__main__":
    since = None
    fmt = "markdown"
    args = sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == "--json":
            fmt = "json"
        elif arg == "--since" and i + 1 < len(args):
            since = args[i + 1]

    scan(since=since, output_format=fmt)
