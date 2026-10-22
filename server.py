#!/usr/bin/env python3
"""Local server for the semantic-view converter.

Binds to 127.0.0.1 only. It does not read environment variables, does not call
Sigma, and does not accept a client id or client secret.

    python3 server.py
"""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from converter.build import convert

ROOT = Path(__file__).resolve().parent
HOST = "127.0.0.1"
PORT = 8765
MAX_BODY = 2_000_000


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Status line only. Never log a request body; it is someone else's model.
        if args and str(args[1]).startswith("4") or (args and str(args[1]).startswith("5")):
            super().log_message("%s %s", args[0], args[1])

    def _send(self, code, body, content_type):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _static(self, url_path):
        rel = url_path.split("?", 1)[0].lstrip("/")
        if rel in ("", "index.html"):
            file = ROOT / "index.html"
        else:
            if ".." in rel.split("/") or rel.startswith("."):
                return None
            file = (ROOT / rel).resolve()
            root = ROOT.resolve()
            if root not in file.parents:
                return None
            if any(part.startswith(".") or part == "__pycache__" for part in file.relative_to(root).parts):
                return None
        return file if file.is_file() else None

    def do_GET(self):
        file = self._static(self.path)
        if file is None:
            self._send(404, "Not found", "text/plain")
            return
        kind = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                ".js": "text/javascript; charset=utf-8", ".sql": "text/plain; charset=utf-8",
                ".py": "text/plain; charset=utf-8"}.get(file.suffix, "application/octet-stream")
        self._send(200, file.read_bytes(), kind)

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/api/convert":
            self._send(404, "Not found", "text/plain")
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self._send(413, json.dumps({"ok": False, "error": "The semantic view is too large."}), "application/json")
            return
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send(400, json.dumps({"ok": False, "error": "The request was not JSON."}), "application/json")
            return
        # Ignore anything that looks like an API credential if a client sends it.
        for banned in ("clientSecret", "clientId", "client_" + "secret", "client_" + "id", "token", "apiKey"):
            payload.pop(banned, None)
        try:
            def flag(key, default):
                if key not in payload:
                    return default
                return bool(payload.get(key))

            result = convert(
                payload.get("source") or "",
                connection_id=payload.get("connectionId"),
                catalog=payload.get("catalog"),
                facts_as_metrics=flag("factsAsMetrics", True),
                names=payload.get("names"),
                skip_facts=payload.get("skipFacts"),
                synonyms_in_description=flag("synonymsInDescription", True),
                infer_number_formats=flag("inferNumberFormats", True),
                database=payload.get("database"),
                schema=payload.get("schema"),
            )
        except Exception as exc:  # noqa: BLE001 — return the message, never a traceback with locals
            self._send(400, json.dumps({"ok": False, "error": str(exc)}), "application/json")
            return
        self._send(200, json.dumps(result), "application/json")


def main():
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Semantic view converter at http://{HOST}:{PORT}/")
    server.serve_forever()


if __name__ == "__main__":
    main()
