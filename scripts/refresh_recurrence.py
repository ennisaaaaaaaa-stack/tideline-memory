#!/usr/bin/env python3
"""Recalculate recurrence scores for ALL narratives based on current tag frequencies.

Recurrence is deterministic: it counts how many OTHER narratives share at least
one tag with this one. The score is locked at write time but goes stale as new
memories accumulate. This script refreshes it.

Run periodically (e.g. in DREAM combing layer) or manually:
    python3 scripts/refresh_recurrence.py

The script also recomputes the composite weight for each narrative.
"""

import json
import math
import sqlite3
import sys
from pathlib import Path

DEFAULT_DB = Path.home() / "memory" / "mcp_memory.db"


def _compute_weight(imp, emo, rec, unr):
    """Same formula as server.py."""
    imp = imp if imp is not None else 3
    emo = emo if emo is not None else 3
    rec = rec if rec is not None else 3
    unr = unr if unr is not None else 3
    raw = imp * 0.35 + emo * 0.25 + rec * 0.25 + unr * 0.15
    return raw / 5.0


def _recurrence_from_freq(freq: int) -> int:
    """Map co-occurrence count to recurrence score."""
    if freq <= 1:
        return 1
    elif freq <= 3:
        return 2
    elif freq <= 6:
        return 3
    elif freq <= 11:
        return 4
    else:
        return 5


def refresh(db_path: Path):
    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")

    # 1. Build tag frequency map across ALL narratives
    all_tags = {}
    rows = db.execute("SELECT id, tags FROM narratives WHERE tags IS NOT NULL").fetchall()
    for r in rows:
        try:
            tags = json.loads(r["tags"])
            for t in tags:
                all_tags[t] = all_tags.get(t, 0) + 1
        except (json.JSONDecodeError, TypeError):
            pass

    # 2. Recalculate recurrence + weight for each narrative
    updated = 0
    skipped = 0
    for r in rows:
        try:
            tags = json.loads(r["tags"])
        except (json.JSONDecodeError, TypeError):
            tags = []

        if not tags:
            skipped += 1
            continue

        # Count how many OTHER narratives share at least one tag
        co_count = 0
        for t in tags:
            co_count = max(co_count, all_tags.get(t, 0) - 1)  # -1 to exclude self

        new_rec = _recurrence_from_freq(co_count)

        # Recompute weight
        # We need the other dimensions — read them from the narratives row
        full = db.execute(
            "SELECT importance, emotional, unresolved FROM narratives WHERE id = ?",
            (r["id"],),
        ).fetchone()
        if not full:
            continue

        new_weight = _compute_weight(
            full["importance"], full["emotional"], new_rec, full["unresolved"]
        )

        db.execute(
            "UPDATE narratives SET recurrence = ?, weight = ? WHERE id = ?",
            (new_rec, new_weight, r["id"]),
        )
        updated += 1

    db.commit()

    # Report
    print(f"✅ Recurrence refreshed for {updated} narratives ({skipped} skipped, no tags)")
    print(f"   Tag vocabulary: {len(all_tags)} unique tags")
    print(f"   Most frequent tags:")
    for tag, count in sorted(all_tags.items(), key=lambda x: -x[1])[:5]:
        print(f"     {tag}: {count} → recurrence {_recurrence_from_freq(count - 1)}")

    db.close()


if __name__ == "__main__":
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DB
    if not db_path.exists():
        print(f"❌ Database not found: {db_path}")
        sys.exit(1)
    refresh(db_path)
