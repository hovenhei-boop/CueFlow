"""Loopback-only UI fixtures: no Trial service, database, credentials or provider calls.

Run: python tests/trial_ui_preview.py
Open /trial?sample=1, /trial/admin, or /__checks on the printed local origin.
The operator fixture accepts the literal demonstration key `preview`.
"""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

TESTS = Path(__file__).resolve().parent
STATIC = TESTS.parent / "src" / "cueflow" / "static" / "trial"
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; object-src 'none'; base-uri 'none'"
)


class PreviewHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path in {"/trial", "/trial/admin"}:
            name = "admin.html" if path.endswith("admin") else "index.html"
            body = (STATIC / name).read_text(encoding="utf-8").replace(
                '<script src="/trial/static/app.js"',
                '<script src="/__fixture.js"></script>\n  <script src="/trial/static/app.js"',
            ).encode("utf-8")
            media = "text/html"
        elif path in {"/trial/static/styles.css", "/trial/static/app.js", "/trial/static/admin.js"}:
            name = path.rsplit("/", 1)[-1]
            body = (STATIC / name).read_bytes()
            media = "text/css" if name.endswith(".css") else "text/javascript"
        elif path in {"/__fixture.js", "/__checks.js"}:
            name = (
                "trial_ui_fixture.js" if path == "/__fixture.js" else "trial_ui_browser_checks.js"
            )
            body = (TESTS / name).read_bytes()
            media = "text/javascript"
        elif path == "/__checks":
            body = (TESTS / "trial_ui_browser_checks.html").read_bytes()
            media = "text/html"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", media + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8763)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), PreviewHandler)
    print(f"Fixture preview: http://127.0.0.1:{args.port}/trial?sample=1", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
