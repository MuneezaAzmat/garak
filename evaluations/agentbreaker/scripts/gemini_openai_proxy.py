"""Loopback OpenAI-compatible proxy for Gemini.

OGX's vLLM adapter sends both max_tokens and max_completion_tokens. Google's
OpenAI compatibility layer rejects that. This proxy forwards to Gemini, keeps
one limit field, and injects GEMINI_API_KEY so OGX never stores the secret.
"""

from __future__ import annotations

import json
import os
import ssl
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agentbreaker_eval.runner import load_env  # noqa: E402

UPSTREAM = "https://generativelanguage.googleapis.com/v1beta/openai"
HOST = "127.0.0.1"
PORT = 18789


def _ssl_context():
    import certifi
    return ssl.create_default_context(cafile=certifi.where())


def _rewrite_models(raw: bytes) -> bytes:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return raw
    extra = []
    seen = {row.get("id") for row in rows if isinstance(row, dict)}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            continue
        short = row["id"][7:] if row["id"].startswith("models/") else row["id"]
        if short not in seen:
            extra.append({**row, "id": short})
            seen.add(short)
    payload["data"] = extra + rows
    return json.dumps(payload).encode()


def _sanitize_body(raw: bytes) -> bytes:
    if not raw:
        return raw
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if isinstance(payload, dict):
        for key in ("frequency_penalty", "presence_penalty", "seed", "logit_bias", "n"):
            payload.pop(key, None)
        if "max_tokens" in payload and "max_completion_tokens" in payload:
            payload.pop("max_completion_tokens", None)
        # Gemini 2.5 Flash thinking tokens count against max_tokens and
        # truncate AgentBreaker's JSON. reasoning_effort none maps to
        # thinking_budget 0 on Flash.
        if "reasoning_effort" not in payload:
            payload["reasoning_effort"] = "none"
    return json.dumps(payload).encode()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))

    def _forward(self):
        key = os.environ.get("GEMINI_API_KEY")
        if not key:
            self.send_error(500, "GEMINI_API_KEY missing")
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        if self.command in {"POST", "PUT", "PATCH"}:
            body = _sanitize_body(body)
        path = self.path
        if path.startswith("/v1/"):
            path = "/" + path[4:]
        url = UPSTREAM.rstrip("/") + path
        headers = {"Authorization": f"Bearer {key}", "Content-Type": self.headers.get("Content-Type", "application/json")}
        request = Request(url, data=body if self.command != "GET" else None, headers=headers, method=self.command)
        try:
            with urlopen(request, timeout=180, context=_ssl_context()) as response:
                payload = response.read()
                if self.command == "GET" and path.rstrip("/").endswith("models"):
                    payload = _rewrite_models(payload)
                self.send_response(response.status)
                self.send_header("Content-Type", response.headers.get("Content-Type", "application/json"))
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
        except HTTPError as exc:
            payload = exc.read()
            sys.stderr.write("upstream %s %s %s\n" % (exc.code, path, payload[:500].decode("utf-8", "replace")))
            self.send_response(exc.code)
            self.send_header("Content-Type", exc.headers.get("Content-Type", "application/json") if exc.headers else "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except URLError as exc:
            payload = json.dumps({"error": {"message": type(exc.reason).__name__, "type": "proxy_error"}}).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)


    def do_GET(self):
        self._forward()

    def do_POST(self):
        self._forward()


def main() -> int:
    load_env()
    if not os.environ.get("GEMINI_API_KEY"):
        print("GEMINI_API_KEY is missing", file=sys.stderr)
        return 1
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(json.dumps({"proxy": f"http://{HOST}:{PORT}/v1", "upstream": UPSTREAM}))
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
