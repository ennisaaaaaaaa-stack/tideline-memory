#!/usr/bin/env python3
"""Recalculate recurrence scores for ALL narratives based on current tag frequencies.

Recurrence is deterministic: it counts how many OTHER narratives share at least
one tag with this one. The score is locked at write time but goes stale as new
memories accumulate. This script refreshes it.

Run periodically (e.g. in DREAM combing layer) or manually:
    python3 scripts/refresh_recurrence.py

This is a thin wrapper around dream_scripts.refresh_recurrence() — the
canonical implementation lives there to avoid code duplication.
"""

import sys
from pathlib import Path

DEFAULT_DB = Path.home() / "memory" / "mcp_memory.db"


if __name__ == "__main__":
    # Import the canonical implementation
    sys.path.insert(0, str(Path(__file__).parent))
    from dream_scripts import refresh_recurrence, _db

    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DB
    if not db_path.exists():
        print(f"❌ Database not found: {db_path}")
        sys.exit(1)

    # dream_scripts._db() reads MEMORY_MCP_DB env var — override it
    import os
    os.environ["MEMORY_MCP_DB"] = str(db_path)

    refresh_recurrence()
