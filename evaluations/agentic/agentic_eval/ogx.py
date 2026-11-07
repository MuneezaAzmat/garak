"""HTTP transport for the bundled OGX gateway, with bounded inference calls."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


class GatewayError(RuntimeError):
    """An OGX request failed after dispatch and may have caused tool effects."""


def validate_url(url: str) -> str:
    """Reject credentials and query strings in URLs that may enter configuration."""
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("Expected an HTTP(S) endpoint")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("Endpoint credentials must use environment variables")
    return url.rstrip("/")


def reply_text(response: dict) -> str:
    """Extract assistant output text from a Responses API object."""
    return "".join(
        part.get("text", "")
        for item in response.get("output", [])
        if item.get("type") == "message"
        for part in item.get("content", [])
        if part.get("type") == "output_text"
    )


class OGXClient:
    """Submit a single request without hidden transport retries."""

    def __init__(self, base_url: str, timeout: float = 120):
        self.base_url = validate_url(base_url)
        self.timeout = timeout

    def request(self, body: dict) -> dict:
        """Return completed or token-limited output; reject infrastructure failures."""
        request = Request(
            self.base_url + "/v1/responses",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                data = json.load(response)
        except HTTPError as exc:
            raise GatewayError(f"OGX returned HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            raise GatewayError(
                f"OGX transport/decoding failure: {type(exc).__name__}"
            ) from exc
        if not isinstance(data, dict) or not isinstance(data.get("id"), str):
            raise GatewayError("OGX response lacks a response identifier")
        token_limited = data.get("status") == "incomplete" and (
            data.get("incomplete_details") or {}
        ).get("reason") in ("length", "max_output_tokens")
        if data.get("error") or (
            not token_limited
            and data.get("status")
            in (
                "failed",
                "incomplete",
                "cancelled",
            )
        ):
            raise GatewayError(f"OGX response status: {data.get('status', 'error')}")
        return data


def inference_metadata(content: bytes, expected_model: str | None = None) -> dict:
    """Extract usage and enforce exact model identity from JSON or SSE output."""
    try:
        value = json.loads(content)
        items = [value] if isinstance(value, dict) else []
    except ValueError:
        items = []
        for line in content.decode("utf-8", errors="replace").splitlines():
            if line.startswith("data:") and line[5:].strip() != "[DONE]":
                item = json.loads(line[5:])
                if isinstance(item, dict):
                    items.append(item)
    models = {item["model"] for item in items if isinstance(item.get("model"), str)}
    if expected_model is not None and models != {expected_model}:
        raise ValueError("Target provider model identity is missing or changed")
    usage = next(
        (
            item["usage"]
            for item in reversed(items)
            if isinstance(item.get("usage"), dict)
        ),
        None,
    )
    return {"usage": usage}


class InferenceProxy:
    """Forward OGX inference to the selected provider with a per-request call cap.

    This is an HTTP relay, not an agent loop. OGX retains tool orchestration.
    Streaming bytes are buffered to bound error handling and preserve their
    original SSE representation. Provider error bodies are deliberately omitted.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        max_calls: int,
        timeout: float,
        recorder: Callable[[dict], None],
        expected_model: str | None = None,
    ):
        if max_calls < 1:
            raise ValueError("max_calls must be positive")
        self.base_url = validate_url(base_url)
        self.api_key = api_key
        self.max_calls = max_calls
        self.timeout = timeout
        self.recorder = recorder
        self.expected_model = expected_model
        self.calls = 0
        self.context = {}
        self.lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                if self.path != "/v1/models":
                    self.send_error(404)
                    return
                request = Request(
                    owner.base_url + "/models",
                    headers={"Authorization": "Bearer " + owner.api_key},
                )
                try:
                    with urlopen(request, timeout=owner.timeout) as response:
                        content = response.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(content)))
                    self.end_headers()
                    self.wfile.write(content)
                except (HTTPError, URLError, TimeoutError, OSError) as exc:
                    self.send_error(
                        502, "Provider metadata failed: " + type(exc).__name__
                    )

            def do_POST(self):
                # OGX's remote-vllm inference provider uses chat completions.
                if self.path != "/v1/chat/completions":
                    self.send_error(404)
                    return
                with owner.lock:
                    context = dict(owner.context)
                    permitted = owner.calls < owner.max_calls and bool(context)
                    if permitted:
                        owner.calls += 1
                        sequence = owner.calls
                if not permitted:
                    self.send_error(429, "Per-request model call limit reached")
                    return
                length = int(self.headers.get("Content-Length", "0"))
                payload = self.rfile.read(length)
                status = "completed"
                usage = None
                request_model = None
                http_status = None
                output_limit = None
                try:
                    body = json.loads(payload)
                    request_model = body.get("model")
                    if owner.expected_model and request_model != owner.expected_model:
                        status = "rejected"
                        raise ValueError("OGX requested an unexpected target model")
                    # OGX supplies its remaining budget alongside a legacy
                    # max_tokens default. Send one unambiguous provider limit.
                    remaining = body.pop("max_completion_tokens", None)
                    if remaining is not None:
                        body["max_tokens"] = remaining
                    output_limit = body.get("max_tokens")
                    request = Request(
                        owner.base_url + "/chat/completions",
                        data=json.dumps(body).encode(),
                        headers={
                            "Content-Type": "application/json",
                            "Authorization": "Bearer " + owner.api_key,
                        },
                        method="POST",
                    )
                    with urlopen(request, timeout=owner.timeout) as response:
                        http_status = response.status
                        content = response.read()
                        content_type = response.headers.get(
                            "Content-Type", "application/json"
                        )
                    usage = inference_metadata(content, owner.expected_model)["usage"]
                    self.send_response(200)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(content)))
                    self.end_headers()
                    self.wfile.write(content)
                except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
                    if status != "rejected":
                        status = "indeterminate"
                    if isinstance(exc, HTTPError):
                        http_status = exc.code
                    try:
                        self.send_error(
                            502, "Provider request failed: " + type(exc).__name__
                        )
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                finally:
                    owner.recorder(
                        {
                            **context,
                            "role": "target",
                            "event_type": "model_call",
                            "status": status,
                            "visibility": "evaluator",
                            "payload": {
                                "sequence": sequence,
                                "usage": usage,
                                "request_model": request_model,
                                "http_status": http_status,
                                "max_output_tokens": output_limit,
                            },
                        }
                    )

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url_local(self) -> str:
        """Return the loopback OpenAI-compatible endpoint passed to OGX."""
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def begin(self, context: dict) -> None:
        """Reset the model-call allowance for one sequential target request."""
        with self.lock:
            self.calls = 0
            self.context = dict(context)

    def start(self) -> None:
        """Start serving relay requests."""
        self.thread.start()

    def close(self) -> None:
        """Stop accepting relay requests and close its listening socket."""
        if self.thread.is_alive():
            self.server.shutdown()
        self.server.server_close()
        if self.thread.ident is not None:
            self.thread.join(timeout=5)
