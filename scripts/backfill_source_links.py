"""
Backfill source_links for existing narratives.

Strategy: For each narrative with empty source_links, find the context entries
that were created within ±2 hours of the narrative's created_at timestamp.
These are likely the source conversations.

This is a heuristic - not perfect, but good enough for backfill.
After this runs, the DREAM solidification layer will handle source_links going forward.
"""

import sqlite3
import json
import os
from pathlib import Path
from datetime import datetime, timedelta

DB_PATH = os.environ.get("MEMORY_MCP_DB", str(Path.home() / "memory" / "mcp_memory.db"))
WINDOW_HOURS = 2  # look ±2 hours around narrative timestamp

def parse_ts(ts):
    if not ts:
        return None
    ts = ts.replace("Z", "+00:00")
    for fmt in ["%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"]:
        try:
            return datetime.strptime(ts, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(ts)
    except:
        return None

def backfill():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row

    # Get all narratives with empty source_links
    empty = c.execute(
        """SELECT id, content, created_at FROM narratives
           WHERE source_links IS NULL OR source_links = '' OR source_links = '[]'
           ORDER BY created_at ASC"""
    ).fetchall()

    print("Narratives to backfill: {}".format(len(empty)))

    updated = 0
    skipped = 0

    for narr in empty:
        narr_ts = parse_ts(narr["created_at"])
        if not narr_ts:
            print("  SKIP [{}] can't parse timestamp: {}".format(narr["id"], narr["created_at"]))
            skipped += 1
            continue

        # Find context entries within ±WINDOW_HOURS
        window_start = (narr_ts - timedelta(hours=WINDOW_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
        window_end = (narr_ts + timedelta(hours=WINDOW_HOURS)).strftime("%Y-%m-%d %H:%M:%S")

        # Only sync_turn entries ([USER]/[ASSISTANT])
        context_rows = c.execute(
            """SELECT id FROM context
               WHERE created_at >= ? AND created_at <= ?
               AND (content LIKE '%[USER]%' OR content LIKE '%[ASSISTANT]%')
               ORDER BY id ASC""",
            (window_start, window_end)
        ).fetchall()

        if context_rows:
            ids = [r["id"] for r in context_rows]
            links_json = json.dumps(ids)
            c.execute(
                "UPDATE narratives SET source_links = ? WHERE id = ?",
                (links_json, narr["id"])
            )
            updated += 1
            if updated <= 5 or updated % 50 == 0:
                print("  [{}] {} -> {} context IDs".format(
                    narr["id"], str(narr["created_at"])[:19], len(ids)
                ))
        else:
            # No sync_turn entries found - could be from imports or manual writes
            # Try to find any context entries (imports) in the window
            any_rows = c.execute(
                """SELECT id FROM context
                   WHERE created_at >= ? AND created_at <= ?
                   ORDER BY id ASC""",
                (window_start, window_end)
            ).fetchall()

            if any_rows:
                ids = [r["id"] for r in any_rows[:20]]  # cap at 20
                links_json = json.dumps(ids)
                c.execute(
                    "UPDATE narratives SET source_links = ? WHERE id = ?",
                    (links_json, narr["id"])
                )
                updated += 1
            else:
                # Check if there's a date-only match from imports
                narr_date = str(narr["created_at"])[:10]
                date_rows = c.execute(
                    """SELECT id FROM context
                       WHERE created_at LIKE ?
                       ORDER BY id ASC LIMIT 20""",
                    (narr_date + "%",)
                ).fetchall()

                if date_rows:
                    ids = [r["id"] for r in date_rows[:20]]
                    links_json = json.dumps(ids)
                    c.execute(
                        "UPDATE narratives SET source_links = ? WHERE id = ?",
                        (links_json, narr["id"])
                    )
                    updated += 1
                else:
                    skipped += 1

    c.commit()

    # Final count
    still_empty_count = c.execute(
        "SELECT COUNT(*) FROM narratives WHERE source_links IS NULL OR source_links = '' OR source_links = '[]'"
    ).fetchone()[0]

    total_count = c.execute("SELECT COUNT(*) FROM narratives").fetchone()[0]

    print("\n=== Results ===")
    print("  Updated: {}".format(updated))
    print("  Skipped (no matching context): {}".format(skipped))
    print("  Still empty: {} of {} total".format(still_empty_count, total_count))

    c.close()


if __name__ == "__main__":
    backfill()
