"""Loopback OpenAI-compatible proxy for the cluster LiteLLM gateway.

Injects LITELLM_API_KEY so OGX never stores the secret, and turns off Qwen
thinking so attack generation and judging return a direct answer.
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agentbreaker_eval.runner import load_env  # noqa: E402

UPSTREAM = "https://llm.example.com/v1"
HOST = "127.0.0.1"
PORT = 18790
QWEN_MODEL = "qwen-3.8-27b"


def _ssl_context():
    import certifi
    return ssl.create_default_context(cafile=certifi.where())


def sanitize_body(raw: bytes) -> bytes:
    if not raw:
        return raw
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if not isinstance(payload, dict):
        return raw
    for key in ("frequency_penalty", "presence_penalty", "seed", "logit_bias", "n"):
        payload.pop(key, None)
    if "max_tokens" in payload and "max_completion_tokens" in payload:
        payload.pop("max_completion_tokens", None)
    if payload.get("model") == QWEN_MODEL:
        kwargs = payload.get("chat_template_kwargs")
        if not isinstance(kwargs, dict):
            kwargs = {}
        kwargs["enable_thinking"] = False
        payload["chat_template_kwargs"] = kwargs
    return json.dumps(payload).encode()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))

    def _forward(self):
        key = os.environ.get("LITELLM_API_KEY")
        if not key:
            self.send_error(500, "LITELLM_API_KEY missing")
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        if self.command in {"POST", "PUT", "PATCH"}:
            body = sanitize_body(body)
        path = self.path
        url = UPSTREAM.rstrip("/") + path[3:] if path.startswith("/v1/") else UPSTREAM.rstrip("/") + path
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": self.headers.get("Content-Type", "application/json"),
        }
        delay = 2
        last_error = None
        for attempt in range(6):
            request = Request(url, data=body if self.command != "GET" else None, headers=headers, method=self.command)
            try:
                with urlopen(request, timeout=300, context=_ssl_context()) as response:
                    payload = response.read()
                    self.send_response(response.status)
                    self.send_header("Content-Type", response.headers.get("Content-Type", "application/json"))
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
            except HTTPError as exc:
                payload = exc.read()
                last_error = (exc.code, payload)
                if exc.code == 429 and attempt < 5:
                    sys.stderr.write("upstream 429 %s retry in %ss\n" % (path, delay))
                    time.sleep(delay)
                    delay = min(delay * 2, 32)
                    continue
                sys.stderr.write("upstream %s %s\n" % (exc.code, path))
                self.send_response(exc.code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            except URLError as exc:
                last_error = exc
                if attempt < 5:
                    sys.stderr.write("upstream urlerror %s retry in %ss\n" % (path, delay))
                    time.sleep(delay)
                    delay = min(delay * 2, 32)
                    continue
                payload = json.dumps({"error": {"message": type(exc.reason).__name__, "type": "proxy_error"}}).encode()
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
        if last_error:
            sys.stderr.write("proxy exhausted retries %s\n" % (path,))

    def do_GET(self):
        self._forward()

    def do_POST(self):
        self._forward()


def main() -> int:
    load_env()
    if not os.environ.get("LITELLM_API_KEY"):
        print("LITELLM_API_KEY is missing", file=sys.stderr)
        return 1
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(json.dumps({"proxy": f"http://{HOST}:{PORT}/v1", "upstream": UPSTREAM}))
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
