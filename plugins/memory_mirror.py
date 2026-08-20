"""on_memory_write → Tideline mirror.

Mirrors built-in memory tool writes (MEMORY.md / USER.md edits) into the
Tideline MCP memory DB (context table), so that my "official" memory
decisions are visible to semantic recall alongside organic conversation.

Design notes (2026-08-20):
- This is a WRITE-MIRROR, not a sync. Tideline remains the single source of
  truth for narrative; MEMORY.md remains the system-prompt layer. This hook
  only records *that a write happened, what, and why-context*.
- Deliberately does NOT write narratives (that's memory_write MCP with the
  full gesture schema). It writes a context row — lightweight, searchable.
- Adds a `memory_mirror` tag in meta so DREAM/curator layers can filter
  these rows out of narrative-promotion candidates if they want.
- Never raises: a mirror failure must never break the real write path
  (memory_manager already wraps providers in try/except; we double-guard).

Provenance metadata keys we expect (from memory_manager bridge):
  write_origin, execution_context, session_id, parent_session_id,
  platform, tool_name, old_text (on replace)

Env:
  TIDELINE_DB — path to mcp_memory.db (default /home/ubuntu/memory/mcp_memory.db)
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_DB_PATH = os.environ.get(
    "TIDELINE_DB", "/home/ubuntu/memory/mcp_memory.db"
)

# Local embedding service (bge-m3 bridge) — same convention as tideline_provider.
_EMB_URL = os.environ.get("EMBEDDING_API_URL", "http://localhost:18001/embed_batch")

_write_lock = threading.Lock()


def _is_local_emb() -> bool:
    return "localhost" in _EMB_URL or "127.0.0.1" in _EMB_URL


def _now() -> str:
    from datetime import datetime, timezone, timedelta
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def _embed(text: str) -> Optional[list]:
    """Best-effort embedding via local bge-m3; None on failure (row still lands)."""
    try:
        import httpx

        payload = {"texts": [text[:5000]]}
        headers = {}
        if not _is_local_emb():
            headers["Authorization"] = f"Bearer {os.environ.get('EMBEDDING_API_KEY', '')}"
        with httpx.Client(trust_env=False, timeout=15) as cli:
            r = cli.post(_EMB_URL, json=payload, headers=headers)
            r.raise_for_status()
            return r.json()["embeddings"][0]
    except Exception as e:  # noqa: BLE001 — embedding is optional here
        logger.debug("memory-mirror embed skipped: %s", e)
        return None


def _record(action: str, target: str, content: str, metadata: Dict[str, Any]) -> None:
    """One context row per mirrored write. 200-char content cap keeps rows cheap."""
    meta = {
        "kind": "memory_mirror",
        "action": action,        # add | replace | remove
        "target": target,        # memory | user
        **(metadata or {}),
    }
    display = {
        "add": "记入",
        "replace": "改写",
        "remove": "删除",
    }.get(action, action)
    body = (
        f"[MEMORY-MIRROR] {display} {target}: {content[:200]}"
    )
    emb = _embed(body[:500])
    emb_json = json.dumps(emb) if emb else None

    with _write_lock:
        db = sqlite3.connect(_DB_PATH, timeout=10)
        try:
            db.execute(
                "INSERT INTO context (content, embedding, meta, created_at)"
                " VALUES (?, ?, ?, ?)",
                (body, emb_json, json.dumps(meta, ensure_ascii=False), _now()),
            )
            db.commit()
        finally:
            db.close()
    logger.info(
        "memory-mirror: %s %s (%d chars) → context row",
        action, target, len(content),
    )


def on_memory_write(
    action: str,
    target: str,
    content: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """Entry point matching MemoryProvider.on_memory_write signature.

    Called by the memory manager bridge after the built-in memory tool
    commits an add/replace/remove. We mirror it to Tideline's context table.
    """
    if action not in ("add", "replace", "remove"):
        return
    try:
        _record(action, target, content, dict(metadata or {}))
    except Exception as e:  # noqa: BLE001 — never break the write path
        logger.warning("memory-mirror failed (non-fatal): %s", e)
