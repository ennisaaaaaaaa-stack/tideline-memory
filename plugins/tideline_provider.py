"""Tideline Memory Provider — auto-injects from MCP DB.

Reads the same SQLite DB as the MCP server (configured via MEMORY_MCP_DB env var).
Only does auto-injection (system_prompt_block + prefetch). Interactive tools
stayed in MCP server — this provider exposes NO tools, avoiding conflicts.

Layers:
  T0 (system_prompt_block): self_concept + latest snapshot + open threads + prefetch pool
  T1 (prefetch): semantic search narratives matching the conversation
  T3 (system_prompt_block): topic cluster map + entity profiles
  T4 (prefetch fallback): full-corpus FTS5 search when T1 returns <2 results
  sync_turn: write user+assistant turn to context table
  on_pre_compress: rescue high-weight memories before context compression
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

from agent.memory_provider import MemoryProvider

logger = logging.getLogger(__name__)

import os as _os

import re as _re

def _strip_tags(text: str) -> str:
    """Remove structured tags like [gesture: xxx], [self_reflection] from injection text.

    Tags are useful for storage/classification but break memory texture on injection.
    Keeps arrows (→) and natural parentheses — only strips [bracket-tags].
    """
    if not text:
        return text
    return _re.sub(
        r'\[(?:gesture|self_reflection|terrain|context|moment|general|insight|observation|reflection)\s*:?\s*[^\]]*\]',
        '', text, flags=_re.IGNORECASE
    ).strip()

_DB_PATH = _os.environ.get("MEMORY_MCP_DB", str(Path.home() / "memory" / "mcp_memory.db"))
_EMBED_URL = _os.environ.get("EMBEDDING_API_URL", "http://127.0.0.1:18001/embed_batch")
_EMBED_KEY = _os.environ.get("EMBEDDING_API_KEY", "")
_EMBED_MODEL = _os.environ.get("EMBEDDING_MODEL", "embedding-3")

def _is_local_emb() -> bool:
    return "localhost" in _EMBED_URL or "127.0.0.1" in _EMBED_URL

# ─── Helpers ──────────────────────────────────────────────

def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _db() -> sqlite3.Connection:
    c = sqlite3.connect(_DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=30000")
    return c

def _cosine(a: list, b: list) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)

def _relative_time(created_at: str) -> str:
    """Convert timestamp to natural relative time (几天前, 昨天, etc)."""
    if not created_at:
        return ""
    try:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        ts = created_at.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        days = (now - dt).days
        if days == 0: return "今天"
        elif days == 1: return "昨天"
        elif days <= 6: return f"{days}天前"
        elif days <= 13: return "上周"
        elif days <= 29: return f"{days // 7}周前"
        elif days <= 59: return "上个月"
        else: return f"{days // 30}个月前"
    except Exception:
        return ""

def _embed(text: str) -> list:
    """Get embedding from local bge-m3 or remote OpenAI-compatible API."""
    import urllib.request, json
    try:
        if _is_local_emb():
            # Local bge-m3: POST {"texts": [...]} → {"embeddings": [[...]]}
            req = urllib.request.Request(
                _EMBED_URL,
                data=json.dumps({"texts": [text]}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
                embeddings = data.get("embeddings", [])
                return embeddings[0] if embeddings else []
        else:
            # Remote OpenAI-compatible: POST {"model":..., "input":...} with Bearer
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {_EMB_KEY}",
            }
            req = urllib.request.Request(
                _EMBED_URL,
                data=json.dumps({"model": _EMB_MODEL, "input": text}).encode(),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                if "data" in data:
                    return data["data"][0].get("embedding", [])
                embeddings = data.get("embeddings", [])
                return embeddings[0] if embeddings else []
    except Exception as e:
        logger.debug("embed failed: %s", e)
        return []

# ─── Conversation extraction helper ──────────────────────

def _extract_conversation(messages: list) -> list:
    """Extract (role, text) pairs from OpenAI-style messages.

    Filters out:
    - system messages (already in system prompt)
    - tool calls / tool results (no semantic value for memory)
    - trivial messages (under 10 chars)
    - duplicate consecutive messages
    """
    extracted = []
    seen = set()
    for msg in messages:
        role = msg.get("role", "")
        if role == "system":
            continue

        content = msg.get("content", "")
        # Handle content that's a list (multimodal) or string
        if isinstance(content, list):
            text_parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    text_parts.append(part.get("text", ""))
                elif isinstance(part, str):
                    text_parts.append(part)
            content = " ".join(text_parts)
        elif not isinstance(content, str):
            continue

        content = content.strip()
        if len(content) < 10:
            continue

        # Dedup
        key = content[:200]
        if key in seen:
            continue
        seen.add(key)

        extracted.append((role.upper(), content))

    return extracted


# ─── Provider ─────────────────────────────────────────────

class TidelineMemoryProvider(MemoryProvider):
    """Read-only auto-injection from Tideline MCP memory DB."""

    def __init__(self):
        self._db_path = _DB_PATH
        self._session_id = ""
        self._prefetch_cache: str = ""
        self._turn_count = 0
        self._injected_ids: set = set()  # cross-turn dedup within a session

    @property
    def name(self) -> str:
        return "tideline"

    def is_available(self) -> bool:
        return Path(self._db_path).exists()

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = session_id
        self._turn_count = 0
        self._injected_ids = set()  # reset dedup set for new session
        logger.info("Tideline memory provider initialized (session=%s, db=%s)",
                     session_id, self._db_path)

    # ═══ T0: System Prompt Block ═══════════════════════════

    def system_prompt_block(self) -> str:
        """Inject at session start: who I am, what state I'm in, what threads are open."""
        try:
            c = _db()
            parts = []

            # 1. Self-concept (who I am)
            sc_rows = c.execute(
                "SELECT field, content FROM self_concept ORDER BY field"
            ).fetchall()
            if sc_rows:
                lines = []
                for r in sc_rows:
                    lines.append(r['content'])
                parts.append("\n".join(lines))

            # 2. Latest snapshot (current state)
            snap = c.execute(
                "SELECT content FROM snapshots ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            if snap:
                parts.append(f"## 最新状态快照\n{snap['content']}")

            # 3. Open threads (self-generated clues)
            threads = c.execute(
                "SELECT id, content, weight FROM threads WHERE status='open' ORDER BY weight DESC LIMIT 5"
            ).fetchall()
            if threads:
                lines = ["## 待探索的线索\n"]
                for t in threads:
                    lines.append(f"- {t['content']}")
                parts.append("\n".join(lines))

            # 4. Prefetch pool (high-weight recent memories, deduplicated by tag)
            pool = c.execute(
                """SELECT id, gesture, cognition_direction, weight, tags FROM narratives
                   WHERE weight > 0.6
                   AND created_at > datetime('now', '-7 days')
                   ORDER BY weight DESC LIMIT 10"""
            ).fetchall()
            if pool:
                # Deduplicate: keep highest-weight per tag, no tag overlap between slots
                seen_tags = set()
                deduped = []
                for m in pool:
                    try:
                        m_tags = json.loads(m["tags"]) if m["tags"] else []
                    except (json.JSONDecodeError, TypeError):
                        m_tags = []
                    # Skip if any of this memory's tags already seen in pool
                    if m_tags and any(t in seen_tags for t in m_tags):
                        continue
                    deduped.append(m)
                    seen_tags.update(m_tags)
                    if len(deduped) >= 3:
                        break
                if deduped:
                    lines = ["## 近期高权重记忆\n"]
                    for m in deduped:
                        g = _strip_tags(m['gesture'])
                        cd = f" → {_strip_tags(m['cognition_direction'])}" if m["cognition_direction"] else ""
                        lines.append(f"- {g}{cd}")
                    parts.append("\n".join(lines))
                    
                    # ── Log T0 passive injection as attention hits ──
                    # These narratives enter model view without a query — pure weight-driven.
                    # sim=0 (no semantic query involved), source='t0_inject'
                    t0_scored = [(0.0, m) for m in deduped]
                    self._log_attention(t0_scored, source="t0_inject")

            # ── T2b: Context bridge (recent raw conversation chunks) ──
            bridge = self._context_bridge(c)
            if bridge:
                parts.append(bridge)

            # ── T3a: Topic map (what themes live in my memory) ──
            topics = c.execute(
                """SELECT cluster_name, noun_freq FROM topic_clusters
                   WHERE noun_freq >= 5
                   ORDER BY noun_freq DESC LIMIT 25"""
            ).fetchall()
            if topics:
                lines = ["## 记忆主题图谱（top 25）\n"]
                topic_strs = [f"{t['cluster_name']}({t['noun_freq']})" for t in topics]
                lines.append(" · ".join(topic_strs))
                parts.append("\n".join(lines))

            # ── T3b: People I know (profiles) ──
            # Strategy: fixed entities (full) + pinned entities (full) + rest (summary)
            import os as _os_pin
            from collections import defaultdict as _dd
            pin_path = _os_pin.path.expanduser("~/.hermes/profile_pins.json")
            fixed_entities = {"self"}
            pinned_entities = []
            try:
                with open(pin_path) as _pf:
                    _pin_data = json.load(_pf)
                # Support both formats: {"fixed":[...], "pinned":[...]} or plain [...]
                if isinstance(_pin_data, dict):
                    fixed_entities = set(_pin_data.get("fixed", ["self"]))
                    _pinned_raw = _pin_data.get("pinned", [])
                else:
                    _pinned_raw = _pin_data
                # Dedup: pinned entries already in fixed don't count
                pinned_entities = [p for p in _pinned_raw if p not in fixed_entities][:2]
            except (FileNotFoundError, json.JSONDecodeError):
                pass

            full_entities = fixed_entities | set(pinned_entities)

            # Fetch all profiles
            profiles = c.execute(
                """SELECT entity, ptype, content FROM profiles ORDER BY entity"""
            ).fetchall()
            if profiles:
                by_entity = _dd(list)
                for p in profiles:
                    by_entity[p['entity']].append(p)

                lines = ["## 人物画像\n"]
                for entity, plist in by_entity.items():
                    if entity in full_entities:
                        # Full injection: all ptypes
                        for p in plist:
                            content = p['content'] or ''
                            if content:
                                lines.append(f"**{entity}** ({p['ptype'] or ''}): {content}")
                    else:
                        # Summary injection: prefer contact ptype, fallback to first available
                        summary_p = next((p for p in plist if p['ptype'] == 'contact'), None)
                        if summary_p is None and plist:
                            summary_p = plist[0]  # fallback: no contact, use first ptype
                        if summary_p:
                            raw = (summary_p['content'] or '').strip()
                            if len(raw) <= 100:
                                summary = raw  # short enough, no truncation
                            elif '。' in raw[:100]:
                                summary = raw[:100].rsplit('。', 1)[0] + '。'
                            else:
                                summary = raw[:100]
                            lines.append(f"**{entity}** ({summary_p['ptype'] or ''}): {summary}")

                if len(lines) > 1:
                    parts.append("\n".join(lines))

            c.close()

            if not parts:
                return ""

            return "\n\n".join(parts)

        except Exception as e:
            logger.warning("Tideline system_prompt_block failed: %s", e)
            return ""

    # ═══ T1: Prefetch (per-turn semantic search) ══════════

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Semantic search narratives relevant to current conversation."""
        if not query or len(query.strip()) < 5:
            return self._prefetch_cache  # return cached from queue_prefetch

        try:
            qvec = _embed(query[:500])
            if not qvec:
                return ""

            c = _db()
            # Get all narratives with embeddings
            rows = c.execute(
                """SELECT id, gesture, context_layer, cognition_direction, weight,
                          embedding, tags, created_at
                   FROM narratives
                   WHERE gesture IS NOT NULL AND gesture != ''
                   AND embedding IS NOT NULL
                   ORDER BY created_at DESC"""
            ).fetchall()

            if not rows:
                c.close()
                return ""

            # ── Adaptive window: cap corpus size for cosine performance ──
            SCAN_LIMIT = 2000
            scan_rows = rows
            scan_truncated = False
            if len(rows) > SCAN_LIMIT:
                scan_rows = rows[:SCAN_LIMIT]
                scan_truncated = True

            scored = []
            for r in scan_rows:
                emb_raw = r["embedding"]
                if not emb_raw:
                    continue
                try:
                    emb = json.loads(emb_raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                sim = _cosine(qvec, emb)
                if sim > 0.25:  # threshold
                    scored.append((sim, r))

            scored.sort(key=lambda x: x[0], reverse=True)

            # ── Cross-turn dedup: skip narratives already injected this session ──
            fresh = [(sim, r) for sim, r in scored if r["id"] not in self._injected_ids]
            top = fresh[:5]

            # If dedup leaves <2 results, relax: allow re-injection of top hits
            if len(top) < 2 and scored:
                top = scored[:5]  # fallback: no dedup when too few fresh hits

            # ── T4: Fallback to full FTS5 search when T1 misses ──
            if len(top) < 2:
                t4_results = self._t4_fts_search(c, query, c_ids=[r["id"] for _, r in top])
                if t4_results:
                    lines = ["## 相关记忆（auto-recalled）\n"]
                    for sim, r in top:
                        cd = f" → {_strip_tags(r['cognition_direction'])}" if r["cognition_direction"] else ""
                        ctx = f" ({_strip_tags(r['context_layer'])})" if r["context_layer"] else ""
                        rt = f" ({_relative_time(r['created_at'])})" if r["created_at"] else ""
                        lines.append(f"- {_strip_tags(r['gesture'])}{cd}{ctx}{rt}")
                    lines.append("\n## 远期记忆（FTS5 fallback）\n")
                    lines.append(t4_results)
                    c.close()
                    result = "\n".join(lines)
                    self._prefetch_cache = result
                    for _, r in top:
                        self._injected_ids.add(r["id"])
                    self._log_attention(top)
                    return result

            if not top:
                c.close()
                return ""

            lines = ["## 相关记忆（auto-recalled）\n"]
            for sim, r in top:
                cd = f" → {_strip_tags(r['cognition_direction'])}" if r["cognition_direction"] else ""
                ctx = f" ({_strip_tags(r['context_layer'])})" if r["context_layer"] else ""
                rt = f" ({_relative_time(r['created_at'])})" if r["created_at"] else ""
                lines.append(f"- {_strip_tags(r['gesture'])}{cd}{ctx}{rt}")

            # Silent degradation: don't expose scan limits in injection

            c.close()
            result = "\n".join(lines)
            self._prefetch_cache = result
            for _, r in top:
                self._injected_ids.add(r["id"])
            self._log_attention(top)
            return result

        except Exception as e:
            logger.warning("Tideline prefetch failed: %s", e)
            return ""

    def _log_attention(self, scored_results, source="t1_prefetch"):
        """Delegate to shared attention tracking module."""
        if not scored_results:
            return
        try:
            from scripts.attention_shared import log_attention
            c = _db()
            log_attention(c, scored_results, source=source)
            c.close()
        except Exception:
            pass  # attention tracking should never break prefetch

    def _t4_fts_search(self, c, query: str, c_ids: list) -> str:
        """T4: Full-corpus FTS5 keyword search fallback when T1 returns <2 results.

        Searches ALL narratives (not just recent 100) by keyword.
        Returns formatted string or empty string.
        """
        import re as _re
        # Extract meaningful keywords from query (>=2 char chunks)
        keywords = _re.findall(r'[\u4e00-\u9fff]{2,}|[a-zA-Z]{3,}', query[:200])
        if not keywords:
            return ""

        # Take top 5 keywords
        keywords = keywords[:5]
        results = []
        seen_ids = set(c_ids)

        for kw in keywords:
            try:
                fts_rows = c.execute(
                    """SELECT n.id, n.gesture, n.context_layer, n.cognition_direction, n.weight,
                              n.created_at
                       FROM narratives_fts f
                       JOIN narratives n ON n.id = f.rowid
                       WHERE narratives_fts MATCH ?
                       AND n.id NOT IN ({})
                       ORDER BY n.weight DESC LIMIT 3""".format(
                        ",".join(["?"] * len(seen_ids)) if seen_ids else "-1"
                    ),
                    [kw] + list(seen_ids)
                ).fetchall()
                for r in fts_rows:
                    if r["id"] not in seen_ids:
                        seen_ids.add(r["id"])
                        results.append(r)
            except Exception:
                continue

        if not results:
            return ""

        results.sort(key=lambda r: r["weight"] or 0, reverse=True)
        lines = []
        for r in results[:5]:
            cd = f" → {_strip_tags(r['cognition_direction'])}" if r["cognition_direction"] else ""
            lines.append(f"- {_strip_tags(r['gesture'])}{cd}")
        return "\n".join(lines)

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Pre-compute for next turn."""
        if not query or len(query.strip()) < 5:
            return
        # Run prefetch in background thread
        def _bg():
            try:
                self._prefetch_cache = self.prefetch(query, session_id=session_id)
            except Exception:
                pass
        threading.Thread(target=_bg, daemon=True).start()

    # ═══ T2b: Context Bridge ═══════════════════════════════

    def _context_bridge(self, c, max_chunk=5, max_hours=72) -> str:
        """Pull recent raw context chunks — the last conversation's texture.

        Strategy:
        1. Expand window 24h → 48h → 72h until we find context entries.
        2. Group by session (from meta). Sort by recency (latest entry).
           Only use narrative weight as a tiebreaker when sessions overlap
           in time (within 30 min of each other).
        3. Return the latest N chunks from the most recent session.
        """
        for hours in range(24, max_hours + 1, 24):
            rows = c.execute(
                """SELECT id, content, meta, created_at FROM context
                   WHERE created_at > datetime('now', ?)
                   ORDER BY created_at DESC LIMIT 30""",
                (f"-{hours} hours",),
            ).fetchall()
            if rows:
                break
        else:
            return ""

        # Group by session, tracking each session's latest timestamp
        sessions: dict[str, list] = {}
        session_latest: dict[str, str] = {}
        for r in rows:
            try:
                meta = json.loads(r["meta"]) if r["meta"] else {}
            except (json.JSONDecodeError, TypeError):
                meta = {}
            sess = meta.get("session", "") or "_legacy"
            sessions.setdefault(sess, []).append(r)
            ts = r["created_at"] or ""
            if ts > session_latest.get(sess, ""):
                session_latest[sess] = ts

        # Sort sessions: primary = recency (latest entry DESC),
        # tiebreaker = narrative weight
        def _sess_weight(cursor, chunk_list):
            chunk_ids = [str(r["id"]) for r in chunk_list]
            placeholders = ",".join("?" * len(chunk_ids))
            linked = cursor.execute(
                f"""SELECT MAX(n.weight) as w FROM narratives n
                   WHERE EXISTS (
                       SELECT 1 FROM json_each(n.source_links) je
                       WHERE je.value IN ({placeholders})
                   )""",
                chunk_ids,
            ).fetchone()
            return (linked["w"] or 0) if linked else 0

        sorted_sessions = sorted(
            sessions.keys(),
            key=lambda s: (session_latest.get(s, ""),
                           _sess_weight(c, sessions[s])),
            reverse=True,
        )

        best_sess = sorted_sessions[0] if sorted_sessions else None

        if not best_sess:
            return ""

        chunks = sessions[best_sess][-max_chunk:]
        lines = ["## 上下文桥接（最近对话质感）\n"]
        for chunk in chunks:
            text = chunk["content"][:400]
            lines.append(text)
        return "\n\n".join(lines)

    # ═══ sync_turn (write context) ════════════════════════

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        """Write turn to context table with embedding (same as MCP server does)."""
        self._turn_count += 1
        try:
            c = _db()
            meta = json.dumps({"session": session_id} if session_id else {})
            for role, text in [("USER", user_content), ("ASSISTANT", assistant_content)]:
                content = f"[{role}] {text[:2000]}"
                emb = _embed(content[:500])
                emb_json = json.dumps(emb) if emb else None
                c.execute(
                    "INSERT INTO context (content, embedding, meta, created_at) VALUES (?, ?, ?, ?)",
                    (content, emb_json, meta, _now()),
                )
            c.commit()
            c.close()
        except Exception as e:
            logger.debug("Tideline sync_turn failed: %s", e)

    # ═══ on_pre_compress (extract + rescue) ═══════════════

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        """Before context compression: extract + persist, then remind.

        Two things happen here:
        1. Extract user messages and key assistant content from the messages
           about to be discarded, and persist them to the context table with
           embeddings so they remain searchable after the buffer is gone.
        2. Return high-weight memory reminders for the compression summary
           prompt so the compressor preserves them.
        """
        # ── Phase 1: Persist messages about to be lost ──
        try:
            extracted = _extract_conversation(messages)
            if extracted:
                c = _db()
                for role, text in extracted:
                    content = f"[{role}] {text[:2000]}"
                    emb = _embed(content[:500])
                    emb_json = json.dumps(emb) if emb else None
                    meta = json.dumps({"session": self._session_id, "rescued": True})
                    c.execute(
                        "INSERT INTO context (content, embedding, meta, created_at) VALUES (?, ?, ?, ?)",
                        (content, emb_json, meta, _now()),
                    )
                c.commit()
                c.close()
                logger.info(
                    "Tideline on_pre_compress: persisted %d messages before compression",
                    len(extracted),
                )
        except Exception as e:
            logger.debug("Tideline on_pre_compress persist failed: %s", e)

        # ── Phase 2: Return high-weight memory reminders ──
        try:
            c = _db()
            rows = c.execute(
                """SELECT gesture, cognition_direction FROM narratives
                   WHERE weight > 0.7
                   ORDER BY created_at DESC LIMIT 3"""
            ).fetchall()
            c.close()

            if not rows:
                return ""

            lines = ["高权重记忆：\n"]
            for r in rows:
                cd = f" → {r['cognition_direction']}" if r["cognition_direction"] else ""
                lines.append(f"- {r['gesture']}{cd}")
            return "\n".join(lines)

        except Exception as e:
            logger.debug("Tideline on_pre_compress reminder failed: %s", e)
            return ""

    # ═══ on_session_end (session boundary checkpoint) ═════

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        """Session boundary checkpoint: persist the full conversation tail.

        Called on /new, /reset, CLI exit, gateway session expiry. Extracts
        all user messages and substantive assistant responses, writes them
        to context table with embeddings. This is the safety net that
        catches anything not already persisted by sync_turn.
        """
        try:
            extracted = _extract_conversation(messages)
            if not extracted:
                return

            c = _db()
            count = 0
            for role, text in extracted:
                content = f"[{role}] {text[:2000]}"
                # Check if already persisted by sync_turn (dedup by content prefix)
                existing = c.execute(
                    "SELECT 1 FROM context WHERE content = ? LIMIT 1",
                    (content,),
                ).fetchone()
                if existing:
                    continue
                emb = _embed(content[:500])
                emb_json = json.dumps(emb) if emb else None
                meta = json.dumps({"session": self._session_id, "session_end": True})
                c.execute(
                    "INSERT INTO context (content, embedding, meta, created_at) VALUES (?, ?, ?, ?)",
                    (content, emb_json, meta, _now()),
                )
                count += 1
            c.commit()
            c.close()

            if count:
                logger.info(
                    "Tideline on_session_end: persisted %d new messages at session boundary",
                    count,
                )
        except Exception as e:
            logger.debug("Tideline on_session_end failed: %s", e)

    # ═══ No tools (MCP server handles interactive) ════════

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """Return empty — MCP server provides all interactive memory tools."""
        return []

    def shutdown(self) -> None:
        logger.info("Tideline memory provider shutdown")
