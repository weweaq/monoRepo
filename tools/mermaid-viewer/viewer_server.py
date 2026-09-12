"""Local single-user viewer server for the mermaid .mmd viewer.

Serves the repo root over HTTP so the viewer (tools/mermaid-viewer/viewer.html)
can be opened locally without a file:// sandbox, and persists review comments
into SQLite under tools/mermaid-viewer/data/reviews.db -- so comments survive
a reload or a reboot.

Usage (PowerShell):
    python tools/mermaid-viewer/viewer_server.py
    python -m tools.mermaid_viewer.viewer_server --port 8123 --no-browser

Standard library only. Everything runs on 127.0.0.1 (loopback); no internet.

Endpoints:
    GET  /tools/mermaid-viewer/viewer.html   (static files, repo root)
    GET  /api/reviews/<name>                 -> {"reviews": [...]}
    POST /api/reviews/<name>                 body {"reviews": [...]} upserts
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import threading
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

# Repo root = two levels up from this file (tools/mermaid-viewer/).
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(DATA_DIR, "reviews.db")
DEFAULT_PORT = 8123


def _connect() -> sqlite3.Connection:
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS reviews ("
        "  file_key TEXT PRIMARY KEY,"
        "  payload TEXT NOT NULL,"
        "  updated_at TEXT NOT NULL DEFAULT (datetime('now','+8 hours'))"
        ")"
    )
    return conn


class Handler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # -- API ------------------------------------------------------------
    def _parse_reviews_key(self) -> str | None:
        # path: /api/reviews/<encoded-name>
        parts = self.path.lstrip("/").split("/")
        if len(parts) >= 3 and parts[0] == "api" and parts[1] == "reviews":
            return parts[2]
        return None

    def _send_json(self, obj: dict, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        key = self._parse_reviews_key()
        if key is not None:
            conn = _connect()
            try:
                row = conn.execute(
                    "SELECT payload FROM reviews WHERE file_key=?",
                    (key,),
                ).fetchone()
            finally:
                conn.close()
            self._send_json({"reviews": json.loads(row[0]) if row else []})
            return
        super().do_GET()

    def do_POST(self) -> None:
        key = self._parse_reviews_key()
        if key is None:
            self._send_json({"error": "bad path"}, 400)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
            data = json.loads(raw.decode("utf-8"))
            reviews = data.get("reviews", [])
            if not isinstance(reviews, list):
                self._send_json({"error": "reviews must be a list"}, 400)
                return
        except (ValueError, UnicodeDecodeError):
            self._send_json({"error": "invalid json"}, 400)
            return
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO reviews(file_key, payload, updated_at) VALUES(?,?,datetime('now','+8 hours')) "
                "ON CONFLICT(file_key) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at",
                (key, json.dumps(reviews, ensure_ascii=False)),
            )
            conn.commit()
        finally:
            conn.close()
        self._send_json({"ok": True, "count": len(reviews)})

    # -- static ---------------------------------------------------------
    def translate_path(self, path: str) -> str:
        # Force the docroot to the repo root, not CWD.
        path = path.split("?", 1)[0].split("#", 1)[0]
        rel = path.lstrip("/")
        full = os.path.realpath(os.path.join(ROOT, rel))
        root = os.path.realpath(ROOT)
        if full != root and not full.startswith(root + os.sep):
            return os.path.join(root, "nonexistent")
        return full

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[viewer] %s - %s\n" % (self.address_string(), fmt % args))


def _browser_target(port: int) -> str:
    return "http://127.0.0.1:%d/tools/mermaid-viewer/viewer.html" % port


def main() -> None:
    ap = argparse.ArgumentParser(description="Local mermaid .mmd viewer + review store")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    port = httpd.server_address[1]

    print("=" * 60)
    print("  mermaid .mmd viewer  (localhost, offline)")
    print("  打开:  %s" % _browser_target(port))
    print("  仓库根: %s" % ROOT)
    print("  评论库: %s" % DB_PATH)
    print("  按 Ctrl+C 停止")
    print("=" * 60)

    if not args.no_browser:
        threading.Timer(0.6, webbrowser.open, args=(_browser_target(port),)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
