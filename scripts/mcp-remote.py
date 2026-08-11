#!/usr/bin/env python3
"""
MCP Remote Controller — 自然语言遥控器
常驻进程，监听 inbox 文件，匹配规则后直接执行命令。
不经过 LLM，不消耗 token。

Usage:
    python3 mcp-remote.py  (前台)
    或通过 systemd / nohup 后台运行

Inbox: ~/.hermes/mcp_remote/inbox.txt
    每行一条指令，执行完清空
    格式: 自然语言，如 "♪ SomaFM" 或 "播放 辰星遗响"

Rules: ~/.hermes/mcp_remote/rules.json
    [
      {
        "match": "somafm|drone.?zone",
        "action": "http",
        "url": "http://localhost:8802/play/somafm/dronezone"
      },
      {
        "match": "停止音乐|stop.?music|安静",
        "action": "http",
        "url": "http://localhost:8802/stop"
      },
      {
        "match": "切歌|next|skip",
        "action": "http",
        "url": "http://localhost:8802/next"
      }
    ]
"""
import json
import os
import re
import time
import urllib.request
import urllib.error
import logging
from pathlib import Path

BASE_DIR = Path.home() / ".hermes" / "mcp_remote"
INBOX = BASE_DIR / "inbox.txt"
RULES_FILE = BASE_DIR / "rules.json"
LOG_FILE = BASE_DIR / "remote.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(str(LOG_FILE)),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("mcp-remote")

POLL_INTERVAL = 2  # seconds


def load_rules():
    """Load rules from JSON file."""
    try:
        with open(RULES_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        log.warning("Could not load rules: %s", e)
        return []


def execute_http(url, method="GET"):
    """Fire an HTTP request to trigger an action."""
    try:
        req = urllib.request.Request(url, method=method)
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", errors="replace")[:500]
            log.info("HTTP %s %s → %d %s", method, url, resp.status, body[:100])
            return True
    except urllib.error.URLError as e:
        log.error("HTTP failed: %s — %s", url, e)
        return False
    except Exception as e:
        log.error("Unexpected error for %s: %s", url, e)
        return False


def execute_shell(command):
    """Run a shell command."""
    import subprocess
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=30
        )
        log.info("SHELL '%s' → exit %d", command, result.returncode)
        if result.stderr:
            log.warning("stderr: %s", result.stderr[:200])
        return result.returncode == 0
    except Exception as e:
        log.error("Shell command failed: %s", e)
        return False


def match_and_execute(line):
    """Match a line against rules and execute the first match."""
    rules = load_rules()
    line_lower = line.lower().strip()

    for rule in rules:
        pattern = rule.get("match", "")
        if not pattern:
            continue
        try:
            if re.search(pattern, line_lower, re.IGNORECASE):
                action = rule.get("action", "")
                log.info("MATCH '%s' → rule '%s' (action: %s)", line[:80], pattern, action)

                if action == "http":
                    url = rule.get("url", "")
                    method = rule.get("method", "GET")
                    return execute_http(url, method)
                elif action == "shell":
                    cmd = rule.get("command", "")
                    return execute_shell(cmd)
                elif action == "mcp":
                    # Future: direct MCP server call
                    log.info("MCP action (not yet implemented): %s", rule.get("tool", ""))
                    return False
                else:
                    log.warning("Unknown action type: %s", action)
                    return False
        except re.error as e:
            log.error("Invalid regex '%s': %s", pattern, e)

    log.info("NO MATCH for: %s", line[:80])
    return False


def process_inbox():
    """Read inbox, process each line, clear it."""
    if not INBOX.exists():
        return

    content = INBOX.read_text(encoding="utf-8").strip()
    if not content:
        return

    lines = [l.strip() for l in content.splitlines() if l.strip()]
    log.info("Processing %d command(s) from inbox", len(lines))

    for line in lines:
        match_and_execute(line)

    # Clear inbox after processing
    INBOX.write_text("", encoding="utf-8")


def main():
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    if not INBOX.exists():
        INBOX.write_text("", encoding="utf-8")
    if not RULES_FILE.exists():
        RULES_FILE.write_text("[]", encoding="utf-8")

    log.info("MCP Remote Controller started")
    log.info("Inbox: %s", INBOX)
    log.info("Rules: %s", RULES_FILE)
    log.info("Polling every %ds", POLL_INTERVAL)

    try:
        while True:
            process_inbox()
            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        log.info("Shutting down")


if __name__ == "__main__":
    main()
