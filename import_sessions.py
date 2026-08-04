#!/usr/bin/env python3
"""
Bulk import Hermes sessions into Tideline Memory MCP.

Scans all session files, extracts user/assistant dialogue,
groups by ISO week + platform, writes to MCP-B context table with embeddings.
"""

import json, os, sqlite3, time, re, httpx
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict

DB_PATH = os.environ.get("MEMORY_MCP_DB", str(Path.home() / "memory" / "mcp_memory.db"))
SESSIONS_DIR = os.path.expanduser(os.environ.get("HERMES_SESSIONS_DIR", "~/.hermes/sessions"))
EMB_URL = os.environ.get("EMBEDDING_API_URL", "http://localhost:18001/embed_batch")

def now_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def embed(text: str):
    if not text or len(text) < 5:
        return None
    try:
        with httpx.Client(trust_env=False, timeout=30) as cli:
            r = cli.post(EMB_URL, json={"texts": [text[:8000]]})
            r.raise_for_status()
            return r.json()["embeddings"][0]
    except Exception as e:
        return None

def batch_embed(texts: list):
    """Batch embed via local bge-m3 service."""
    results = []
    valid = [(i, t) for i, t in enumerate(texts) if t and len(t) >= 5]
    for i in range(0, len(valid), 32):
        batch = valid[i:i+32]
        try:
            inputs = [t[:8000] for _, t in batch]
            with httpx.Client(trust_env=False, timeout=60) as cli:
                r = cli.post(EMB_URL, json={"texts": inputs})
                r.raise_for_status()
                data = r.json()["embeddings"]
                for j, item in enumerate(batch):
                    results.append((item[0], data[j]))
            time.sleep(0.2)
        except Exception as e:
            for item in batch:
                results.append((item[0], None))
    
    # Sort back to original order
    results.sort(key=lambda x: x[0])
    return [emb for _, emb in results]


def extract_session(filepath):
    """Extract dialogue from a session file."""
    try:
        with open(filepath) as f:
            if filepath.endswith('.jsonl'):
                # JSONL format - each line is a message
                messages = []
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                        messages.append(msg)
                    except:
                        continue
                # First line might be session_meta
                meta = {}
                if messages and messages[0].get("role") == "session_meta":
                    meta = messages.pop(0)
                
                platform = "unknown"
                model = "unknown"
                session_start = ""
            else:
                data = json.load(f)
                messages = data.get("messages", [])
                platform = data.get("platform", "unknown")
                model = data.get("model", "unknown")
                session_start = data.get("session_start", data.get("last_updated", ""))
                meta = data
    except Exception as e:
        return None
    
    if not messages:
        return None
    
    # Extract user and assistant messages
    user_msgs = []
    assistant_msgs = []
    
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        
        if not content or not isinstance(content, str):
            # Handle list content (multimodal)
            if isinstance(content, list):
                content = " ".join([c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"])
            else:
                continue
        
        if not content.strip():
            continue
        
        # Skip tool outputs and system messages
        if role in ("tool", "system", "session_meta"):
            continue
        
        # Clean content
        content = content.strip()
        
        # Skip very short tool-like messages
        if role == "user":
            # Skip pure tool result references
            if content.startswith("[引用:") or content.startswith("[工具结果"):
                # But keep the actual user text after
                pass
            user_msgs.append(content)
        elif role == "assistant":
            # Skip pure tool calls
            if content.startswith("[工具调用") or content.startswith("[tool_call"):
                continue
            assistant_msgs.append(content)
    
    if not user_msgs and not assistant_msgs:
        return None
    
    return {
        "platform": platform,
        "model": model,
        "session_start": session_start,
        "user_msgs": user_msgs,
        "assistant_msgs": assistant_msgs,
        "msg_count": len(user_msgs) + len(assistant_msgs),
    }


def create_weekly_summary(sessions_in_week):
    """Create a summary text for a week's worth of sessions."""
    lines = []
    
    # Sort by session_start
    sessions_in_week.sort(key=lambda s: s.get("session_start", ""))
    
    platforms = set(s.get("platform") or "unknown" for s in sessions_in_week)
    total_msgs = sum(s["msg_count"] for s in sessions_in_week)
    
    lines.append(f"本周共 {len(sessions_in_week)} 个session，{total_msgs} 条对话消息。")
    lines.append(f"平台: {', '.join(platforms)}")
    lines.append("")
    
    for s in sessions_in_week:
        platform = s["platform"]
        date = s.get("session_start", "")[:10]
        
        # Extract key user messages (first 3, each max 200 chars)
        user_text = []
        for msg in s["user_msgs"][:5]:
            # Clean up references
            clean = re.sub(r'\[引用:.*?\]', '', msg).strip()
            if len(clean) > 10:
                user_text.append(clean[:300])
        
        # Extract key assistant messages (first 2, each max 200 chars)
        asst_text = []
        for msg in s["assistant_msgs"][:3]:
            clean = re.sub(r'\[工具.*?\]', '', msg).strip()
            if len(clean) > 10:
                asst_text.append(clean[:300])
        
        if user_text or asst_text:
            lines.append(f"[{date}|{platform}]")
            for ut in user_text[:3]:
                lines.append(f"  用户: {ut}")
            for at in asst_text[:2]:
                lines.append(f"  洄: {at}")
            lines.append("")
    
    return "\n".join(lines)


def main():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=5000")
    
    # Scan all sessions
    print("Scanning sessions...")
    
    # Skip patterns
    skip_patterns = ['cron_', 'eph_', 'request_dump', 'sessions.json']
    
    all_sessions = []
    skipped = 0
    
    for fname in sorted(os.listdir(SESSIONS_DIR)):
        if any(p in fname for p in skip_patterns):
            skipped += 1
            continue
        
        fpath = os.path.join(SESSIONS_DIR, fname)
        if not os.path.isfile(fpath):
            continue
        
        result = extract_session(fpath)
        if result:
            result["filename"] = fname
            # Parse date from filename or session_start
            date_str = result.get("session_start", "")
            if not date_str:
                # Try to parse from filename
                m = re.search(r'(\d{4})(\d{2})(\d{2})', fname)
                if m:
                    date_str = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
            
            try:
                if "T" in date_str:
                    dt = datetime.fromisoformat(date_str.replace("Z", ""))
                elif date_str:
                    dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
                else:
                    continue
            except:
                continue
            
            iso_year, iso_week, _ = dt.isocalendar()
            result["iso_year"] = iso_year
            result["iso_week"] = iso_week
            result["date"] = dt
            all_sessions.append(result)
    
    print(f"  Total sessions extracted: {len(all_sessions)}")
    print(f"  Skipped (cron/eph/dump): {skipped}")
    
    # Group by ISO year + week + platform
    week_groups = defaultdict(list)
    for s in all_sessions:
        key = (s["iso_year"], s["iso_week"])
        week_groups[key].append(s)
    
    print(f"  Unique weeks: {len(week_groups)}")
    
    # Process each week
    total_written = 0
    for (iso_year, iso_week), sessions in sorted(week_groups.items()):
        summary = create_weekly_summary(sessions)
        
        if len(summary) < 100:
            continue
        
        # Split if too long (>8000 chars)
        if len(summary) > 7500:
            # Split at session boundaries
            parts = []
            current = ""
            for line in summary.split("\n"):
                if len(current) + len(line) > 7500 and current:
                    parts.append(current)
                    current = line
                else:
                    current += "\n" + line
            if current:
                parts.append(current)
        else:
            parts = [summary]
        
        for i, part in enumerate(parts):
            # Use the Monday of the ISO week as the date
            week_date = datetime.fromisocalendar(iso_year, iso_week, 1)
            created = week_date.strftime("%Y-%m-%d 23:59:00")
            
            meta = {
                "source": "session-scan",
                "iso_year": iso_year,
                "iso_week": iso_week,
                "session_count": len(sessions),
                "part": i,
                "total_parts": len(parts),
            }
            
            emb = embed(part)
            
            c.execute(
                "INSERT INTO context(content,embedding,meta,created_at) VALUES(?,?,?,?)",
                (part, json.dumps(emb) if emb else None, json.dumps(meta), created)
            )
            total_written += 1
            
            if total_written % 5 == 0:
                print(f"  Written {total_written} weekly summaries... (W{iso_year}-{iso_week:02d})")
                c.commit()
            
            time.sleep(0.3)
    
    c.commit()
    c.close()
    print(f"\n=== Session scan complete: {total_written} weekly context records written ===")


if __name__ == "__main__":
    main()
