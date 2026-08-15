#!/usr/bin/env python3
"""
Background Music Module — 人机同听背景音乐
独立常驻 HTTP 服务，支持 SomaFM 流媒体 + 本地音乐。
人和 agent 都能通过 HTTP API 切歌、暂停、查看状态。

API:
    GET  /play/somafm/<channel>     播放 SomaFM 频道
    GET  /play/local/<song_id>      播放八音盒里的本地歌曲
    GET  /stop                      停止播放
    GET  /next                      随机下一首
    GET  /status                    当前播放状态
    GET  /channels                  列出 SomaFM 频道

Run:
    python3 bg-music.py
    → http://localhost:8802
"""
import json
import os
import random
import sqlite3
import subprocess
import threading
import time
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

PORT = 8802
MUSIC_DB = Path.home() / "ocean-listen" / "ocean_music_box.db"

# SomaFM channels (popular ones, easy to extend)
SOMAFM_CHANNELS = {
    "dronezone": ("SomaFM Drone Zone", "https://ice1.somafm.com/dronezone-256-mp3"),
    "groovesalad": ("SomaFM Groove Salad", "https://ice1.somafm.com/groovesalad-256-mp3"),
    "lush": ("SomaFM Lush", "https://ice1.somafm.com/lush-256-mp3"),
    "spacestation": ("SomaFM Space Station S-3", "https://ice1.somafm.com/spacestation-256-mp3"),
    "deepspaceone": ("SomaFM Deep Space One", "https://ice1.somafm.com/deepspaceone-256-mp3"),
    "secretagent": ("SomaFM Secret Agent", "https://ice1.somafm.com/secretagent-256-mp3"),
    "suburbs": ("SomaFM The Suburbs", "https://ice1.somafm.com/suburbsofgothenburg-256-mp3"),
}

# Playback state
_state = {
    "playing": False,
    "source": None,        # "somafm" / "local" / None
    "channel": None,       # channel name or song name
    "display_name": None,  # human-readable
    "started_at": None,
    "process": None,       # ffplay subprocess
}
_state_lock = threading.Lock()


def _stop_playback():
    """Stop current playback."""
    with _state_lock:
        proc = _state.get("process")
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        _state["playing"] = False
        _state["source"] = None
        _state["channel"] = None
        _state["display_name"] = None
        _state["started_at"] = None
        _state["process"] = None


def _start_stream(url, source, channel, display_name):
    """Start streaming audio via ffplay (no video, no window)."""
    _stop_playback()
    try:
        proc = subprocess.Popen(
            ["ffplay", "-nodisp", "-nostats", "-autoexit", "-loglevel", "quiet", url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        with _state_lock:
            _state["playing"] = True
            _state["source"] = source
            _state["channel"] = channel
            _state["display_name"] = display_name
            _state["started_at"] = time.time()
            _state["process"] = proc
        return True
    except FileNotFoundError:
        # ffplay not installed, try mpg123 or mpv
        for alt in [["mpg123", "-q", url], ["mpv", "--no-video", "--really-quiet", url]]:
            try:
                proc = subprocess.Popen(
                    alt, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                with _state_lock:
                    _state["playing"] = True
                    _state["source"] = source
                    _state["channel"] = channel
                    _state["display_name"] = display_name
                    _state["started_at"] = time.time()
                    _state["process"] = proc
                return True
            except FileNotFoundError:
                continue
        return False
    except Exception as e:
        print(f"Playback error: {e}")
        return False


def _get_local_song(song_id):
    """Get song file path from music box DB."""
    try:
        conn = sqlite3.connect(str(MUSIC_DB))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT name, source_path FROM songs WHERE id=?", (song_id,)).fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception:
        return None


class Handler(BaseHTTPRequestHandler):

    def _json(self, data, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path.startswith("/play/somafm/"):
            channel = path.split("/")[-1].lower()
            if channel in SOMAFM_CHANNELS:
                name, url = SOMAFM_CHANNELS[channel]
                ok = _start_stream(url, "somafm", channel, name)
                if ok:
                    self._json({"ok": True, "playing": name})
                else:
                    self._json({"ok": False, "error": "No audio player found (need ffplay/mpg123/mpv)"}, 500)
            else:
                self._json({"ok": False, "error": f"Unknown channel: {channel}", "available": list(SOMAFM_CHANNELS.keys())}, 404)
            return

        if path.startswith("/play/local/"):
            try:
                song_id = int(path.split("/")[-1])
            except ValueError:
                self._json({"ok": False, "error": "Invalid song ID"}, 400)
                return
            song = _get_local_song(song_id)
            if song and song.get("source_path"):
                ok = _start_stream(song["source_path"], "local", str(song_id), song["name"])
                if ok:
                    self._json({"ok": True, "playing": song["name"]})
                else:
                    self._json({"ok": False, "error": "Playback failed"}, 500)
            else:
                self._json({"ok": False, "error": "Song not found or no file"}, 404)
            return

        if path == "/stop":
            _stop_playback()
            self._json({"ok": True, "stopped": True})
            return

        if path == "/next":
            # Random SomaFM channel
            channels = list(SOMAFM_CHANNELS.keys())
            current = _state.get("channel")
            choices = [c for c in channels if c != current] or channels
            channel = random.choice(choices)
            name, url = SOMAFM_CHANNELS[channel]
            ok = _start_stream(url, "somafm", channel, name)
            self._json({"ok": ok, "playing": name if ok else None})
            return

        if path == "/status":
            with _state_lock:
                elapsed = None
                if _state.get("started_at") and _state.get("playing"):
                    elapsed = int(time.time() - _state["started_at"])
                self._json({
                    "playing": _state["playing"],
                    "source": _state["source"],
                    "channel": _state["channel"],
                    "display_name": _state["display_name"],
                    "elapsed_seconds": elapsed,
                })
            return

        if path == "/channels":
            self._json({
                "somafm": {k: v[0] for k, v in SOMAFM_CHANNELS.items()},
            })
            return

        if path == "/" or path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_PLAYER_PAGE().encode())
            return

        self._json({"error": "not found", "path": path}, 404)

    def log_message(self, format, *args):
        pass  # quiet


def _PLAYER_PAGE():
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>背景音乐 · BG Music</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ background:#0a0e14; color:#e0e6f0; font-family:system-ui,sans-serif; min-height:100vh; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:24px; }}
h1 {{ font-size:18px; font-weight:600; color:#7c5cfc; }}
#status {{ font-size:14px; color:#6b7a90; }}
#now-playing {{ font-size:16px; color:#e0e6f0; }}
.channels {{ display:flex; flex-wrap:wrap; gap:8px; max-width:500px; justify-content:center; }}
.ch {{ padding:8px 16px; background:#1a2332; border:1px solid #2a3344; border-radius:8px; cursor:pointer; color:#e0e6f0; font-size:13px; transition:all .15s; }}
.ch:hover {{ background:#7c5cfc; border-color:#7c5cfc; }}
.ch.active {{ background:#7c5cfc; border-color:#7c5cfc; }}
.controls {{ display:flex; gap:12px; }}
.btn {{ padding:8px 20px; background:#1a2332; border:1px solid #2a3344; border-radius:8px; cursor:pointer; color:#e0e6f0; font-size:14px; }}
.btn:hover {{ background:#ff6b6b; border-color:#ff6b6b; }}
</style></head><body>
<h1>🎵 背景音乐</h1>
<div id="now-playing">—</div>
<div id="status">loading...</div>
<div class="controls">
    <button class="btn" onclick="next()">切歌</button>
    <button class="btn" onclick="stop()">停止</button>
</div>
<div class="channels" id="ch-list"></div>
<script>
const API = '';
async function api(path) {{
    const r = await fetch(API + path);
    return r.json();
}}
async function refresh() {{
    try {{
        const s = await api('/status');
        const np = document.getElementById('now-playing');
        const st = document.getElementById('status');
        if (s.playing) {{
            np.textContent = '🎶 ' + s.display_name;
            const elapsed = s.elapsed_seconds ? Math.floor(s.elapsed_seconds/60)+'m' : '';
            st.textContent = '播放中 · ' + s.source + ' · ' + elapsed;
        }} else {{
            np.textContent = '—';
            st.textContent = '未播放';
        }}
        document.querySelectorAll('.ch').forEach(el => {{
            el.classList.toggle('active', el.dataset.ch === s.channel);
        }});
    }} catch(e) {{ document.getElementById('status').textContent = '连接失败'; }}
}}
async function play(ch) {{ await api('/play/somafm/' + ch); refresh(); }}
async function next() {{ await api('/next'); refresh(); }}
async function stop() {{ await api('/stop'); refresh(); }}
(async () => {{
    const data = await api('/channels');
    const list = document.getElementById('ch-list');
    Object.entries(data.somafm).forEach(([k,v]) => {{
        const el = document.createElement('div');
        el.className = 'ch'; el.dataset.ch = k; el.textContent = v;
        el.onclick = () => play(k);
        list.appendChild(el);
    }});
    refresh();
    setInterval(refresh, 5000);
}})();
</script>
</body></html>"""


def main():
    # Check if any audio player is available
    has_player = any(
        os.system(f"which {p} > /dev/null 2>&1") == 0
        for p in ["ffplay", "mpg123", "mpv"]
    )
    if not has_player:
        print("WARNING: No audio player found (ffplay/mpg123/mpv)")
        print("Install with: sudo apt install ffmpeg")

    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Background Music running on http://localhost:{PORT}")
    print(f"SomaFM channels: {', '.join(SOMAFM_CHANNELS.keys())}")
    server.serve_forever()


if __name__ == "__main__":
    main()
