#!/usr/bin/env python3
"""Tiny local server for the audiobook player.

- Serves the player and the MP3 files (with HTTP Range support, so seeking works everywhere).
- GET/POST /api/state  -> reads/writes progress.json next to the book (position, speed, bookmarks...).
- Listens on 127.0.0.1 only. Shuts itself down after IDLE_MINUTES without any request
  (an open player tab pings every minute, so it stays up while the tab is open).

Usage: python3 server.py [port]
"""
import json
import os
import re
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # book folder
STATE_FILE = os.path.join(ROOT, "progress.json")
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
IDLE_MINUTES = 15
last_hit = time.time()
state_lock = threading.Lock()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def log_message(self, fmt, *args):
        pass  # keep the terminal quiet

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache" if not self.path.startswith("/audio/") else "max-age=86400")
        super().end_headers()

    # ---- API ----
    def do_POST(self):
        global last_hit
        last_hit = time.time()
        if self.path != "/api/state":
            self.send_error(404)
            return
        n = int(self.headers.get("Content-Length") or 0)
        if n > 2_000_000:
            self.send_error(413)
            return
        body = self.rfile.read(n)
        try:
            data = json.loads(body)
            assert isinstance(data, dict)
        except Exception:
            self.send_error(400)
            return
        # Optimistic concurrency: a write must be based on the latest revision, so a stale
        # player tab (e.g. one left open in another browser) cannot overwrite newer progress.
        # The tab that is actually playing sends force=true and wins.
        with state_lock:
            try:
                cur = json.load(open(STATE_FILE, encoding="utf-8"))
            except (FileNotFoundError, ValueError):
                cur = {}
            cur_rev = cur.get("rev", 0)
            base = data.pop("baseRev", None)
            force = data.pop("force", False)
            if "rev" in cur and base != cur_rev and not force:
                raw = json.dumps(cur, ensure_ascii=False).encode()
                self.send_response(409)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            data["rev"] = cur_rev + 1
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, STATE_FILE)
        raw = json.dumps({"rev": data["rev"]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        global last_hit
        last_hit = time.time()
        if self.path == "/":
            self.send_response(302)
            self.send_header("Location", "/player/index.html")
            self.end_headers()
            return
        if self.path.startswith("/api/state"):
            with state_lock:
                try:
                    raw = open(STATE_FILE, "rb").read()
                except FileNotFoundError:
                    raw = b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if self.path.startswith("/api/ping"):
            self.send_response(204)
            self.end_headers()
            return
        rng = self.headers.get("Range")
        path = self.translate_path(self.path)
        if rng and os.path.isfile(path):
            return self.send_range(path, rng)
        return super().do_GET()

    def send_range(self, path, rng):
        size = os.path.getsize(path)
        m = re.match(r"bytes=(\d*)-(\d*)", rng)
        if not m:
            self.send_error(416)
            return
        a, b = m.groups()
        if a == "":
            start, end = max(0, size - int(b)), size - 1
        else:
            start = int(a)
            end = int(b) if b else size - 1
        end = min(end, size - 1)
        if start > end:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    chunk = f.read(min(256 * 1024, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass


def idle_watchdog(server):
    while True:
        time.sleep(30)
        if time.time() - last_hit > IDLE_MINUTES * 60:
            server.shutdown()
            return


if __name__ == "__main__":
    Handler.extensions_map.update({".mp3": "audio/mpeg", ".m4b": "audio/mp4", ".json": "application/json"})
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=idle_watchdog, args=(srv,), daemon=True).start()
    print(f"Audiobook player: http://127.0.0.1:{PORT}/  (progress -> {STATE_FILE})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
