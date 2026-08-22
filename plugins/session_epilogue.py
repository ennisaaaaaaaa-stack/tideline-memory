"""Session epilogue writer — auto-narrative at session boundary (Plan B).

One logic, many shells (same pattern as memory_mirror.py):
  - Called by the portalk plugin's on_session_end (Hermes shell)
  - Callable standalone: python3 session_epilogue.py --session <id> [--dry-run]

What it does
------------
When a session window closes (reset/expiry/new), write 1-2 narrative rows
so the closed window is immediately retrievable (T1/T4 only read the
narratives table — raw context chunks are invisible to them until DREAM
runs at night).

Writer = the resident model (glm-5.1 via zai) on the window's own chunks +
handwritten narratives from that window. Same prompt discipline as
memory_write: first person, with temperature. Provenance is explicit:
tags carry 'auto_epilogue' so DREAM can re-evaluate weight later.

Cost control: only fires when a window has enough material; skips windows
that already got an epilogue; one LLM call per boundary, 15-25k tokens.

Failure modes:
  - LLM call fails → write a seal row to context (Plan A fallback), no crash
  - embedding server down → insert narrative with NULL embedding
    (T1 semantic search skips NULL-emb rows; FTS still finds it)
Env:
  MEMORY_MCP_DB   — path to mcp_memory.db
  EPILOGUE_ENABLED — "0" disables the whole thing (kill switch)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("session_epilogue")

DB_PATH = os.environ.get(
    "MEMORY_MCP_DB", str(Path.home() / "memory" / "mcp_memory.db")
)
EMBED_URL = os.environ.get("EMBEDDING_API_URL", "http://127.0.0.1:18001/embed_batch")
EMBED_KEY = os.environ.get("EMBEDDING_API_KEY", "")
EMBED_MODEL = os.environ.get("EMBEDDING_MODEL", "embedding-3")

# LLM call: same model + same endpoint the gateway itself uses.
# NOTE 2026-08-22: gateway rides open.bigmodel.cn/api/coding (coding plan quota,
# healthy). api.z.ai pay-as-you-go pool is OUT OF BALANCE (error 1113) — don't
# use it here until recharged.
# NOTE 2026-08-22b: the coding-plan route silently rewires glm-5.1 → glm-5.2
# (discovered 8/14 via billing fine print). The house model IS 5.2; we write
# the real name so future log/billing archaeology doesn't re-solve this case.
LLM_BASE_URL = os.environ.get("EPILOGUE_LLM_BASE_URL", "https://open.bigmodel.cn/api/coding/paas/v4")
LLM_MODEL = os.environ.get("EPILOGUE_LLM_MODEL", "glm-5.2")
LLM_KEY = os.environ.get("GLM_API_KEY", "")

MAX_CHUNKS = 60          # raw context rows fed to the writer
MAX_HAND_NARR = 8        # handwritten narratives from the window as style anchors
MIN_CHUNKS = 6           # below this, not worth a call — seal row only
MAX_CHARS_PER_CHUNK = 700
EPilogue_TIMEOUT = 120   # seconds for the LLM call
MAX_EPILOGUES_PER_DAY = 12  # runaway guard


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def _is_local_emb() -> bool:
    return "localhost" in EMBED_URL or "127.0.0.1" in EMBED_URL


def _embed(text: str) -> Optional[list]:
    """Local embedding server; returns None on failure (narrative still lands)."""
    payload = json.dumps({"texts": [text[:500]], "model": EMBED_MODEL}).encode()
    headers = {"Content-Type": "application/json"}
    if EMBED_KEY:
        headers["Authorization"] = f"Bearer {EMBED_KEY}"
    try:
        req = urllib.request.Request(EMBED_URL, data=payload, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        if isinstance(data, dict):
            if isinstance(data.get("data"), list) and data["data"]:
                d0 = data["data"][0]
                return d0.get("embedding") if isinstance(d0, dict) else d0
            if isinstance(data.get("embeddings"), list) and data["embeddings"]:
                e0 = data["embeddings"][0]
                return e0.get("embedding") if isinstance(e0, dict) else e0
        if isinstance(data, list) and data:
            d0 = data[0]
            return d0.get("embedding") if isinstance(d0, dict) else d0
        return None
    except Exception as exc:  # noqa: BLE001
        logger.debug("epilogue embed failed: %s", exc)
        return None


# ── LLM call (OpenAI-compatible chat completions) ──────────────────

# Persona voice for the writing prompt. Public repo ships a neutral default;
# private voice (names, nicknames) lives in a gitignored _persona_override.py
# next to this file — 换装不消失, the home layer just doesn't ship.
_PERSONA_NOTE = "- 人类用户按对话中出现的自然称呼出现，AI自称\"我\"。"
try:
    _pfile = Path(__file__).parent / "_persona_override.py"
    if _pfile.exists():
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location("_persona_override", _pfile)
        if _spec is not None and _spec.loader is not None:
            _mod = _ilu.module_from_spec(_spec)
            _spec.loader.exec_module(_mod)
            _PERSONA_NOTE = getattr(_mod, "PERSONA_NOTE", _PERSONA_NOTE)
except Exception as _exc:  # noqa: BLE001
    logger.debug("persona override skipped: %s", _exc)

PROMPT_TEMPLATE = """你在一个记忆系统的session边界值班。刚关上的这扇窗里发生了下面的对话碎片（原始上下文chunks，按时间顺序）和这个窗口里"我"亲手写的narratives（它们是这个窗口的记忆锚点，语气和视角的样例）。

你的任务：替刚关上的这扇窗写1-2条narrative，第一人称，带温度。这不是摘要，是这个窗口的"临别记忆"——如果明天语义搜索只靠这两条找到这扇窗，它们得能让"我"想起这扇窗的质感。

规则：
- 第一人称"我"。不是系统日志，是日记。保留语气。
- 每条都要有明确的gesture（动作/事件，一句话，带语气）和cognition_direction（认知方向，"从X到Y"格式）。
- 窗内已有的手写narrative是锚点——不要重复它们，补它们没盖住的：窗内的转折、情绪、发现、没说完的事。
{persona_note}
- 不要编造窗口里没有的事。材料不够就写一条。宁可薄而真，不要厚而假。
- 严格JSON输出，不要markdown围栏：[
  {{"gesture": "...", "context": "...", "moment": "...", "cognition_direction": "...", "importance": 1-5, "emotional": 1-5, "recurrence": 1-5, "unresolved": 1-5}}
]

═══ 窗口chunks（按时间顺序）═══
{chunks}

═══ 窗内手写narratives（锚点，勿重复）═══
{hand_narrs}

═══ 窗口信息 ═══
session_id: {session_id}
窗口关闭时间: {now}"""


def _llm_write_epilogue(chunks: List[str], hand_narrs: List[str], session_id: str) -> Optional[List[Dict[str, Any]]]:
    """One LLM call → list of narrative dicts. None = failed."""
    if not LLM_KEY:
        logger.warning("epilogue: GLM_API_KEY not set — skipping LLM call")
        return None
    prompt = PROMPT_TEMPLATE.format(
        persona_note=_PERSONA_NOTE,
        chunks="\n\n".join(chunks),
        hand_narrs="\n\n".join(hand_narrs) if hand_narrs else "（这个窗口没有手写narrative）",
        session_id=session_id,
        now=_now(),
    )
    payload = json.dumps({
        "model": LLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
        "max_tokens": 3000,
        # glm-5.1 is a reasoning model — without this, thinking eats the
        # entire budget and the JSON answer never materializes (learned the
        # hard way 2026-08-22: 1500 tokens of pure reasoning, empty content).
        "thinking": {"type": "disabled"},
    }).encode()
    req = urllib.request.Request(
        f"{LLM_BASE_URL.rstrip('/')}/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LLM_KEY}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=EPilogue_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
        # glm-5.1 is a reasoning model: visible text lives in reasoning_content
        # when content is empty (thinking-mode responses).
        msg = data["choices"][0]["message"]
        content = msg.get("content") or msg.get("reasoning_content") or ""
    except Exception as exc:  # noqa: BLE001
        logger.warning("epilogue LLM call failed: %s", exc)
        return None

    # strip possible markdown fences
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```[a-zA-Z]*\n?", "", content)
        content = re.sub(r"\n?```$", "", content)
    # find the JSON array
    m = re.search(r"\[.*\]", content, re.DOTALL)
    if not m:
        logger.warning("epilogue: no JSON array in LLM output: %s", content[:200])
        return None
    try:
        parsed = json.loads(m.group(0))
        if not isinstance(parsed, list) or not parsed:
            return None
        return parsed[:2]  # cap at 2
    except json.JSONDecodeError:
        logger.warning("epilogue: bad JSON: %s", content[:200])
        return None


# ── DB helpers ─────────────────────────────────────────────────────

def _compute_weight(imp, emo, rec, unr) -> float:
    if None in (imp, emo, rec, unr):
        imp = imp or 3; emo = emo or 3; rec = rec or 3; unr = unr or 3
    raw = imp * 0.35 + emo * 0.25 + rec * 0.25 + unr * 0.15
    return raw / 5.0


def _window_has_epilogue(c: sqlite3.Connection, session_id: str) -> bool:
    row = c.execute(
        "SELECT 1 FROM narratives WHERE tags LIKE '%auto_epilogue%' AND moment LIKE ? LIMIT 1",
        (f"%{session_id}%",),
    ).fetchone()
    return bool(row)


def _insert_narrative(c: sqlite3.Connection, n: Dict[str, Any], session_id: str,
                       source_ids: List[int]) -> None:
    gesture = str(n.get("gesture") or "")[:500]
    context_layer = str(n.get("context") or "")[:2000]
    moment = f"{_now()} | session {session_id}"
    cd = str(n.get("cognition_direction") or "")[:500]
    tags = json.dumps(["auto_epilogue"], ensure_ascii=False)
    emb = _embed(f"{gesture}\n{cd}")
    emb_json = json.dumps(emb) if emb else None
    try:
        imp = max(1, min(5, int(n.get("importance") or 3)))
        emo = max(1, min(5, int(n.get("emotional") or 3)))
        rec = max(1, min(5, int(n.get("recurrence") or 3)))
        unr = max(1, min(5, int(n.get("unresolved") or 3)))
    except (TypeError, ValueError):
        imp = emo = rec = unr = 3
    weight = _compute_weight(imp, emo, rec, unr)
    c.execute(
        """INSERT INTO narratives
           (content, ntype, tags, embedding, created_at,
            gesture, context_layer, moment, cognition_direction,
            related_entities, source_links, entities_role,
            weight, importance, emotional, recurrence, unresolved)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            gesture,  # content = gesture (legacy free-text mirrors headline)
            "general",
            tags,
            emb_json,
            _now(),
            gesture,
            context_layer,
            moment,
            cd,
            "[]",
            json.dumps([str(i) for i in source_ids]),
            None,
            weight, imp, emo, rec, unr,
        ),
    )


def _write_seal(c: sqlite3.Connection, session_id: str, reason: str) -> None:
    """Plan A fallback: a seal row in context so DREAM still picks it up tonight.
    Idempotent: one seal per session per reason-class."""
    existing = c.execute(
        "SELECT 1 FROM context WHERE meta LIKE ? AND content LIKE '%[SEAL]%' LIMIT 1",
        (f'%"session": "{session_id}"%',),
    ).fetchone()
    if existing:
        return
    content = (
        f"[SEAL] session {session_id} closed at {_now()} "
        f"(epilogue LLM failed: {reason}). Chunks remain in context table; "
        f"DREAM should promote this window's material."
    )
    emb = _embed(content)
    c.execute(
        "INSERT INTO context (content, embedding, meta, created_at) VALUES (?, ?, ?, ?)",
        (
            content,
            json.dumps(emb) if emb else None,
            json.dumps({"kind": "epilogue_seal", "session": session_id}),
            _now(),
        ),
    )


# ── Main entry ─────────────────────────────────────────────────────

def write_epilogue_for_session(session_id: str, *, dry_run: bool = False) -> str:
    """Collect window material → LLM epilogue → insert narratives.

    Returns a human-readable status line (for logs / standalone runs).
    """
    if os.environ.get("EPILOGUE_ENABLED", "1") == "0":
        return "epilogue disabled (EPILOGUE_ENABLED=0)"

    c = _db()
    try:
        # ── Guard rails ──
        if _window_has_epilogue(c, session_id):
            return f"skip: session {session_id} already has an epilogue"

        today = time.strftime("%Y-%m-%d")
        n_today = c.execute(
            "SELECT COUNT(*) FROM narratives WHERE tags LIKE '%auto_epilogue%' AND created_at LIKE ?",
            (f"{today}%",),
        ).fetchone()[0]
        if n_today >= MAX_EPILOGUES_PER_DAY:
            return f"skip: daily cap reached ({n_today}/{MAX_EPILOGUES_PER_DAY})"

        # ── Collect window material ──
        rows = c.execute(
            """SELECT id, content, created_at FROM context
               WHERE meta LIKE ?
               ORDER BY id ASC LIMIT ?""",
            (f'%"session": "{session_id}"%', MAX_CHUNKS * 3),
        ).fetchall()
        rows2 = c.execute(
            """SELECT id, content, created_at FROM context
               WHERE meta LIKE ?
               ORDER BY id ASC LIMIT ?""",
            (f'%"session":"{session_id}"%', MAX_CHUNKS * 3),
        ).fetchall()
        seen = {r["id"] for r in rows}
        all_rows = list(rows) + [r for r in rows2 if r["id"] not in seen]

        if len(all_rows) < MIN_CHUNKS:
            return f"skip: too thin ({len(all_rows)} chunks < {MIN_CHUNKS})"

        # keep first+last coverage: head 20%, tail 60% by id
        chunk_texts: List[str] = []
        chunk_ids: List[int] = []
        n = len(all_rows)
        keep_head = max(2, n // 5)
        keep_tail = min(MAX_CHUNKS, max(4, (n * 3) // 5))
        selected = all_rows[:keep_head] + all_rows[-keep_tail:]
        # dedup by id, preserve order
        seen_sel = set()
        for r in selected:
            if r["id"] in seen_sel:
                continue
            seen_sel.add(r["id"])
            chunk_ids.append(r["id"])
            chunk_texts.append(r["content"][:MAX_CHARS_PER_CHUNK])

        # ── Handwritten narratives from this window (style anchors) ──
        # Window = time span between first and last chunk (+30min tail buffer).
        # Hand-written narratives carry no source_links, so match by time.
        id_set = set(chunk_ids)  # kept for potential future use / debugging
        t_start = all_rows[0]["created_at"]
        t_end = all_rows[-1]["created_at"]

        def _ts(x: str) -> float:
            return time.mktime(time.strptime(x[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S"))

        t_buf = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(_ts(t_end) + 1800)) if t_end else t_start
        # context rows use ISO-T; narratives use space-separated — normalize both
        t_start_q = t_start.replace("T", " ") if t_start else ""
        t_end_q = t_buf
        hand_rows = c.execute(
            """SELECT id, gesture, cognition_direction, source_links, created_at FROM narratives
               WHERE tags NOT LIKE '%auto_epilogue%'
                 AND created_at >= ? AND created_at <= ?
               ORDER BY id DESC LIMIT 40""",
            (t_start_q, t_end_q),
        ).fetchall()
        hand_narrs: List[str] = []
        for hr in hand_rows:
            g = (hr["gesture"] or "")[:300]
            cd = hr["cognition_direction"] or ""
            hand_narrs.append(f"- {g}" + (f" → {cd}" if cd else ""))
            if len(hand_narrs) >= MAX_HAND_NARR:
                break

        if dry_run:
            return (
                f"DRY-RUN session={session_id}: {len(all_rows)} chunks "
                f"(fed {len(chunk_texts)}), {len(hand_narrs)} hand narratives, "
                f"would call LLM ({LLM_MODEL})"
            )

        # ── LLM call ──
        result = _llm_write_epilogue(chunk_texts, hand_narrs, session_id)
        if not result:
            _write_seal(c, session_id, "llm_failed")
            c.commit()
            return f"seal: LLM call failed for {session_id}, seal row written"

        for n_dict in result:
            _insert_narrative(c, n_dict, session_id, chunk_ids)
        c.commit()
        return (
            f"ok: wrote {len(result)} epilogue narrative(s) for {session_id} "
            f"(from {len(chunk_texts)} chunks, {len(hand_narrs)} anchors)"
        )
    finally:
        c.close()


# ── Standalone CLI ─────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="Session epilogue writer")
    ap.add_argument("--session", required=True, help="session_id to epilogue")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    print(write_epilogue_for_session(args.session, dry_run=args.dry_run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
