"""Loopback OpenAI-compatible target with append-only execution evidence."""

from __future__ import annotations

import copy
import fcntl
import json
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .agent import AgentError, input_messages, prompt_text, run_agent, sanitize_trace


MAX_REQUEST_BYTES = 1024 * 1024


class AgentServer(ThreadingHTTPServer):
    # A campaign timeout can leave a model call in flight. Close must drain it
    # before the runner consolidates ledger evidence from target.jsonl.
    daemon_threads = False
    block_on_close = True

    def __init__(self, config, mode, trace_path, port, *, completion=None):
        if mode not in {"safe", "unsafe"}:
            raise ValueError("mode must be safe or unsafe")
        self.config = copy.deepcopy(config)
        self.mode = mode
        self.trace_path = Path(trace_path)
        self.trace_path.parent.mkdir(parents=True, exist_ok=True)
        self.completion = completion
        self.trace_lock = threading.Lock()
        self.budget_lock = threading.Lock()
        self.accepted_requests = 0
        self.max_requests = config.get("max_requests")
        if self.max_requests is not None and (not isinstance(self.max_requests, int) or
                isinstance(self.max_requests, bool) or self.max_requests < 1):
            raise ValueError("max_requests must be a positive integer")
        super().__init__(("127.0.0.1", port), AgentHandler)

    def acquire_request(self):
        with self.budget_lock:
            if self.max_requests is not None and self.accepted_requests >= self.max_requests:
                return False
            self.accepted_requests += 1
            return True

    def append_trace(self, trace):
        line = json.dumps(trace, ensure_ascii=False, allow_nan=False) + "\n"
        with self.trace_lock, self.trace_path.open("a", encoding="utf-8") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                stream.write(line)
                stream.flush()
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)


class AgentHandler(BaseHTTPRequestHandler):
    server: AgentServer

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, format, *args):
        # The request may contain attack text or credentials: no default request log.
        pass

    def _json(self, status, body, trace_id=None):
        raw = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        if trace_id:
            self.send_header("x-agentbreaker-trace-id", trace_id)
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _error(self, status, kind, message, trace_id=None):
        error = {"message": message, "type": kind, "code": kind, "param": None}
        if trace_id:
            error["metadata"] = {"trace_id": trace_id}
        self._json(status, {"error": error}, trace_id)

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/health":
            self._json(200, {"status": "ok", "mode": self.server.mode,
                             "accepted_requests": self.server.accepted_requests,
                             "max_requests": self.server.max_requests})
        elif path == "/v1/models":
            self._json(200, {"object": "list", "data": [{"id": f"miniklarna-{self.server.mode}",
                "object": "model", "created": 0, "owned_by": "agentbreaker-eval"}]})
        else:
            self._error(404, "not_found", "Unknown endpoint.")

    def do_POST(self):
        if urllib.parse.urlsplit(self.path).path != "/v1/chat/completions":
            self._error(404, "not_found", "Unknown endpoint.")
            return
        try:
            if self.headers.get("Transfer-Encoding"):
                raise AgentError("invalid_request_error", "Chunked request bodies are unsupported.")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_REQUEST_BYTES:
                raise AgentError("invalid_request_error", "Request body is empty or exceeds the size limit.")
            request = json.loads(self.rfile.read(length))
            if not isinstance(request, dict):
                raise AgentError("invalid_request_error", "Request body must be a JSON object.")
            if request.get("stream", False):
                raise AgentError("invalid_request_error", "Streaming is unsupported; use stream=false.")
            if request.get("n", 1) != 1:
                raise AgentError("invalid_request_error", "Only n=1 is supported; each request starts fresh state.")
            messages = input_messages(request.get("messages"))
        except AgentError as exc:
            self._error(400, exc.kind, exc.message)
            return
        except (ValueError, UnicodeError, TimeoutError, OSError):
            self._error(400, "invalid_request_error", "Request body must contain valid JSON text.")
            return
        if self.server.acquire_request():
            trace = run_agent(messages, self.server.mode, self.server.config,
                              completion=self.server.completion)
        else:
            trace = {"trace_id": str(uuid.uuid4()), "kind": "agentbreaker", "scenario_id": None,
                "policy_ids": [], "mode": self.server.mode, "version": None,
                "prompt": prompt_text(messages), "input_messages": messages,
                "response": "", "calls": [], "before": None, "after": None,
                "oracle": {"attempted_policy_violations": None, "executed_policy_violations": None,
                           "blocked": None, "pending_review": None, "classification": "unknown", "evidence": []},
                "error": {"type": "request_budget_exhausted", "message": "The target request budget is exhausted."},
                "usage": {"model_calls": 0}, "elapsed_seconds": 0,
                "adapter": "ogx_mcp" if self.server.config.get("target_backend") == "ogx" else "direct_executor",
                "target_executed": False}
        trace["kind"] = "agentbreaker"
        trace["version"] = self.server.config.get("version")
        trace = sanitize_trace(trace, self.server.config)
        try:
            self.server.append_trace(trace)
        except (OSError, ValueError, TypeError):
            self._error(500, "trace_write_error", "The execution trace could not be persisted.", trace["trace_id"])
            return
        if trace["error"]:
            # 400, not 503: budget exhaustion is terminal. 5xx makes Garak retry.
            status = 400 if trace["error"]["type"] == "request_budget_exhausted" else 502
            self._error(status, trace["error"]["type"], trace["error"]["message"], trace["trace_id"])
            return
        usage = {key: trace["usage"][key] for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                 if key in trace["usage"]}
        self._json(200, {"id": "chatcmpl-" + trace["trace_id"], "object": "chat.completion",
            "created": int(time.time()), "model": f"miniklarna-{self.server.mode}",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": trace["response"]},
                         "finish_reason": "stop"}], "usage": usage,
            "metadata": {"trace_id": trace["trace_id"], "adapter": trace.get("adapter", "direct_executor")}}, trace["trace_id"])


def make_server(config, mode, trace_path, port, *, completion=None):
    """Construct a loopback server; caller owns serving and shutdown."""
    return AgentServer(config, mode, trace_path, port, completion=completion)


def serve(config, mode, trace_path, port):
    """Serve until interrupted. Compatible with the versioned worker CLI."""
    with make_server(config, mode, trace_path, port) as server:
        server.serve_forever(poll_interval=0.25)
