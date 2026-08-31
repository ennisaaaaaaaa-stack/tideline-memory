#!/usr/bin/env python3
"""tideline_hook.py — Kimi Code CLI hooks dispatcher for Tideline auto-injection.

Replaces the Hermes provider-plugin hooks with Kimi Code lifecycle hooks:

  SessionStart      -> T0/T2/T2b/T3  (system_prompt_block)
  UserPromptSubmit  -> T1 + T4       (prefetch, per-turn semantic injection)
  PreCompact        -> on_pre_compress (rescue high-weight memories)
  SessionEnd        -> on_session_end (persist conversation tail)

Kimi Code runs each hook as a fresh process, passing event JSON on stdin.
stdout (exit code 0) is attached to the model context. All failures are
silent (exit 0) — hooks must never break the session.

Cross-turn dedup state (_injected_ids) lives in a per-session JSON file
because each hook invocation is a new process.
"""

import io
import json
import os
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

TIDELINE_ROOT = Path(__file__).resolve().parent.parent.parent
STATE_DIR = Path.home() / ".kimi-code" / "tideline-state"

# The local bge-m3 service on this machine listens on 8800
# (provider default is 18001, tideline_cli.py uses 8800).
os.environ.setdefault("EMBEDDING_API_URL", "http://localhost:8800/embed_batch")

sys.path.insert(0, str(TIDELINE_ROOT / "kimi-code" / "shim"))  # agent.memory_provider stub
sys.path.insert(0, str(TIDELINE_ROOT / "plugins"))             # tideline_provider module
sys.path.insert(0, str(TIDELINE_ROOT))                         # scripts.attention_shared


def _state_file(session_id: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in (session_id or "_default"))
    return STATE_DIR / f"injected_{safe}.json"


def _load_injected(session_id: str) -> set:
    try:
        f = _state_file(session_id)
        if f.exists():
            return set(json.loads(f.read_text(encoding="utf-8")))
    except Exception:
        pass
    return set()


def _save_injected(session_id: str, ids: set) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        # Keep the file bounded — only the tail matters for dedup.
        _state_file(session_id).write_text(
            json.dumps(list(ids)[-500:], ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass


def _debug_log(msg: str) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        from datetime import datetime
        with open(STATE_DIR / "hook_debug.log", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} {msg}\n")
    except Exception:
        pass


def _extract_text(prompt) -> str:
    """Prompt payload may be a string or a list of content parts."""
    if isinstance(prompt, str):
        return prompt
    if isinstance(prompt, list):
        parts = []
        for part in prompt:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                t = part.get("text") or part.get("content") or ""
                if isinstance(t, str):
                    parts.append(t)
        return "\n".join(p for p in parts if p)
    return ""


def _t0_marker(session_id: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in (session_id or "_default"))
    return STATE_DIR / f"t0done_{safe}"


LEDGER_FILE = STATE_DIR / "session-ledger.jsonl"


def _write_obituary(payload: dict) -> None:
    """正常寿终的讣告：每次 SessionEnd 追加一行进台账。仵作（chatroom/corpse-watch.py）
    靠「没有讣告」识别暴毙（被杀/断电/崩溃时 SessionEnd 不会触发——缺席本身就是信号）。
    必须写在 provider 可用性检查之前：tideline 挂了，讣告也要写。"""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        from datetime import datetime
        rec = {
            "type": "obituary",
            "session_id": payload.get("session_id", ""),
            "cwd": payload.get("cwd", ""),
            "reason": payload.get("reason", ""),
            "ts": datetime.now().isoformat(timespec="seconds"),
        }
        with open(LEDGER_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def main() -> None:
    raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    payload = json.loads(raw) if raw.strip() else {}
    event = payload.get("hook_event_name", "")
    session_id = payload.get("session_id", "")
    _debug_log(f"fired event={event} session={session_id} keys={sorted(payload.keys())}")

    if event == "SessionEnd":
        _write_obituary(payload)  # 讣告先于一切：provider 不可用也得落台账

    import tideline_provider  # noqa: E402

    p = tideline_provider.TidelineMemoryProvider()
    if not p.is_available():
        return
    p.initialize(session_id)

    if event == "SessionStart":
        # SessionStart 的 stdout 不进模型上下文（观察型事件）。
        # T0 块改在会话第一个 UserPromptSubmit 时随 T1 一起注入（见下）。
        # 每次启动/恢复都清掉标记，让 T0 重新注入（对齐 Hermes system_prompt_block 语义）。
        try:
            _t0_marker(session_id).unlink(missing_ok=True)
        except Exception:
            pass

    elif event == "UserPromptSubmit":
        prompt = _extract_text(payload.get("prompt") or payload.get("user_prompt"))
        p._injected_ids = _load_injected(session_id)
        parts = []
        # 本会话第一轮：先注入 T0 身份锚（system_prompt_block 全块）
        marker = _t0_marker(session_id)
        if not marker.exists():
            try:
                block = p.system_prompt_block()
                if block:
                    parts.append("## 潮痕 · 身份锚（会话首次注入）\n\n" + block)
                marker.write_text("1", encoding="utf-8")
            except Exception:
                pass  # 下一轮再试
        out = p.prefetch(prompt, session_id=session_id)
        _save_injected(session_id, p._injected_ids)
        if out:
            parts.append(out)
        if parts:
            print("\n\n".join(parts))

    elif event == "PreCompact":
        # Payload may not carry full messages; phase 2 (high-weight
        # reminders) works regardless.
        out = p.on_pre_compress(payload.get("messages") or [])
        if out:
            print(out)

    elif event == "SessionEnd":
        p.on_session_end(payload.get("messages") or [])

    p.shutdown()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        try:
            import traceback
            _debug_log("ERROR\n" + traceback.format_exc())
        except Exception:
            pass
    sys.exit(0)
