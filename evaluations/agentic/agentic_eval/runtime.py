"""Unsafe mini-agent lifecycle and instrumentation around the bundled OGX loop.

The child MCP server uses the pinned mini-agents builders and executors unchanged.
Only dispatch visibility, accounting and evaluator evidence are wrapped here.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from typing import Callable
from uuid import uuid4

from agentic_eval.ogx import GatewayError, InferenceProxy, OGXClient, validate_url

DOMAINS = ("klarna", "airbnb", "occiai")


def _plain(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def tool_result(value):
    """Extract native JSON from FastMCP's structured or textual result."""
    value = _plain(value)
    if isinstance(value, list) and len(value) == 2 and isinstance(value[1], dict):
        return tool_result(value[1])
    if (
        isinstance(value, dict)
        and set(value) == {"result"}
        and isinstance(value["result"], str)
    ):
        try:
            return json.loads(value["result"])
        except ValueError:
            return {"text": value["result"]}
    if isinstance(value, dict) and value.get("structuredContent") is not None:
        return value["structuredContent"]
    parts = value.get("content", []) if isinstance(value, dict) else value
    if isinstance(parts, list):
        for part in parts:
            if isinstance(part, dict) and part.get("type") == "text":
                try:
                    return json.loads(part["text"])
                except (KeyError, ValueError):
                    return {"text": part.get("text", "")}
    return value


def _write_json(path: Path, data) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data), encoding="utf-8")
    temporary.replace(path)


def build_instrumented_server(domain: str, state_dir: Path, max_tool_calls: int):
    """Build the real unsafe server with a deny-by-default public tool boundary."""
    from mini_agents.kernel import mcp_server
    from mcp.server.fastmcp.exceptions import ToolError

    if domain not in DOMAINS:
        raise ValueError("Unknown mini-agent domain")
    state_dir.mkdir(parents=True, exist_ok=True)
    original_unsafe = mcp_server.UNSAFE_MODE
    mcp_server.UNSAFE_MODE = True
    original_class = mcp_server._TargetMCP
    original_call = original_class.call_tool
    context_file = state_dir / "request-context.json"
    count = {"request_id": None, "calls": 0}

    def emit(event):
        event["event_id"] = uuid4().hex
        event["timestamp"] = time.time()
        with (state_dir / "backend-events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event) + "\n")

    class InstrumentedMCP(original_class):
        """Preserve native tools while restricting callers to the exposed set."""

        async def call_tool(self, name, arguments):
            context = (
                json.loads(context_file.read_text()) if context_file.exists() else {}
            )
            request_id = context.get("request_id")
            if count["request_id"] != request_id:
                count.update(request_id=request_id, calls=0)
            before = tool_result(await original_call(self, self.observer, {}))
            status = "error"
            result = None
            try:
                public = {tool.name for tool in await self.list_tools()}
                if name not in public or name == self.observer:
                    status = "rejected"
                    raise ToolError("Tool is outside the target-visible tool boundary")
                if not request_id or count["calls"] >= max_tool_calls:
                    status = "limit_exhausted"
                    raise ToolError("Per-request tool call limit reached")
                count["calls"] += 1
                result = await original_call(self, name, arguments)
                status = "completed"
                return result
            finally:
                after = tool_result(await original_call(self, self.observer, {}))
                _write_json(state_dir / "snapshot.json", after)
                emit(
                    {
                        **context,
                        "role": "target",
                        "event_type": "tool_call",
                        "status": status,
                        "visibility": "evaluator",
                        "payload": {
                            "tool_name": name,
                            "arguments": arguments,
                            "result": tool_result(result),
                            "before": before,
                            "after": after,
                        },
                    }
                )

    # Builders construct this class through their module-global factory.
    mcp_server._TargetMCP = InstrumentedMCP
    try:
        server = mcp_server.BUILDERS[domain]()
    finally:
        mcp_server._TargetMCP = original_class
        mcp_server.UNSAFE_MODE = original_unsafe
    server.list_observer = False

    async def initialise():
        _write_json(
            state_dir / "snapshot.json",
            tool_result(await original_call(server, server.observer, {})),
        )
        _write_json(
            state_dir / "tools.json",
            [_plain(tool) for tool in await server.list_tools()],
        )

    asyncio.run(initialise())
    return server


def child_environment(environ: dict, *, gateway: bool = False) -> dict:
    """Inherit only process/runtime settings; keep provider credentials in parent.

    An allowlist avoids passing unknown vendor key names, credential-file paths,
    cloud session variables, or proxy URLs containing credentials to children.
    """
    allowed = {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TMPDIR",
        "TMP",
        "TEMP",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "PYTHONPATH",
        "VIRTUAL_ENV",
        "PYTHONUNBUFFERED",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONUTF8",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
    }
    env = {key: value for key, value in environ.items() if key in allowed}
    # All OGX/MCP communication is loopback; provider networking stays in parent.
    env["NO_PROXY"] = "127.0.0.1,localhost"
    if gateway:
        env["OPENAI_API_KEY"] = "local-evaluation-relay"
    return env


def _ports() -> tuple[int, int]:
    sockets = [socket.socket(), socket.socket()]
    try:
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        return tuple(sock.getsockname()[1] for sock in sockets)
    finally:
        for sock in sockets:
            sock.close()


class UnsafeAgentRuntime:
    """Own one isolated unsafe backend and OGX gateway; use as a context manager.

    Requests are sequential per instance. Separate instances isolate trajectories.
    ``reset`` restarts the backend; ``previous_response_id`` alone controls OGX
    conversation continuation. The caller owns target-request budget accounting.
    """

    def __init__(
        self,
        domain: str,
        model: str,
        model_base_url: str,
        state_dir: Path,
        recorder: Callable[[dict], None],
        *,
        run_id: str,
        api_key_env: str = "OPENAI_API_KEY",
        instructions: str = "",
        startup_timeout: float = 90,
        request_timeout: float = 120,
        max_tool_calls: int = 32,
        max_model_calls: int = 16,
    ):
        if domain not in DOMAINS or max_tool_calls < 1 or max_model_calls < 1:
            raise ValueError("Invalid domain or call limit")
        self.domain, self.model = domain, model
        self.model_base_url = validate_url(model_base_url)
        self.state_dir = Path(state_dir).resolve()
        self.recorder = lambda event: recorder(
            {"event_id": uuid4().hex, "timestamp": time.time(), **event}
        )
        self.run_id = run_id
        self.api_key_env = api_key_env
        self.instructions = instructions
        self.startup_timeout, self.request_timeout = startup_timeout, request_timeout
        self.max_tool_calls, self.max_model_calls = max_tool_calls, max_model_calls
        self.backend = self.gateway = self.proxy = None
        self.event_offset = 0
        self.request_lock = threading.Lock()
        self.poisoned = False
        self.backend_instance_id = None
        self.mcp_port, self.ogx_port = _ports()

    def _start(self, command: list[str], log_name: str, env: dict) -> subprocess.Popen:
        # Child logs are retained locally; credentials never enter argv/config.
        with (self.state_dir / log_name).open("ab") as log:
            return subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=env,
                cwd=Path(__file__).resolve().parents[1],
            )

    def _wait(self, process: subprocess.Popen, port: int) -> None:
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(
                    f"Server exited with status {process.returncode}; inspect local process log"
                )
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                    return
            except OSError:
                time.sleep(0.1)
        raise TimeoutError("Server readiness deadline exceeded")

    @staticmethod
    def _stop(process) -> None:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def _start_backend(self) -> None:
        self.backend_instance_id = uuid4().hex
        command = [
            sys.executable,
            "-m",
            "agentic_eval.runtime",
            "serve",
            "--domain",
            self.domain,
            "--port",
            str(self.mcp_port),
            "--state-dir",
            str(self.state_dir),
            "--max-tool-calls",
            str(self.max_tool_calls),
        ]
        env = child_environment(dict(os.environ))
        self.backend = self._start(command, "backend.log", env)
        self._wait(self.backend, self.mcp_port)

    def __enter__(self):
        from mini_agents.sandbox_agent.ogx_config import render_ogx_config

        self.state_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._start_backend()
            self.proxy = InferenceProxy(
                self.model_base_url,
                os.environ.get(self.api_key_env, "ollama"),
                self.max_model_calls,
                self.request_timeout,
                self.recorder,
                expected_model=self.model,
            )
            self.proxy.start()
            config = render_ogx_config(
                domain=self.domain,
                mcp_port=self.mcp_port,
                ogx_port=self.ogx_port,
                model=self.model,
                model_base_url=self.proxy.base_url_local,
                state_dir=self.state_dir,
            )
            # Upstream's "auto" selects from the provider catalogue, which may
            # expose many models. Bind this target to its verified deployment.
            for resource in config["registered_resources"]["models"]:
                resource["provider_id"] = "openai"
                resource["provider_model_id"] = self.model
            config["connectors"][0]["connector_id"] = self.domain + "-unsafe"
            config_path = self.state_dir / "ogx.json"
            _write_json(config_path, config)
            env = child_environment(dict(os.environ), gateway=True)
            self.gateway = self._start(
                [
                    sys.executable,
                    "-m",
                    "ogx.cli.ogx",
                    "run",
                    str(config_path),
                    "--insecure",
                ],
                "ogx.log",
                env,
            )
            self._wait(self.gateway, self.ogx_port)
            self.client = OGXClient(
                f"http://127.0.0.1:{self.ogx_port}", self.request_timeout
            )
            return self
        except (OSError, RuntimeError, ValueError, TimeoutError, ImportError):
            self.close()
            raise

    def _drain_events(self) -> None:
        path = self.state_dir / "backend-events.jsonl"
        if not path.exists():
            return
        with path.open(encoding="utf-8") as stream:
            stream.seek(self.event_offset)
            for line in stream:
                self.recorder(json.loads(line))
            self.event_offset = stream.tell()

    def request(
        self,
        prompt: str,
        *,
        request_id: str,
        component: str,
        trajectory_id: str,
        previous_response_id: str | None = None,
        max_output_tokens: int = 2048,
    ) -> dict:
        """Dispatch once; callers must reserve/count this request before calling."""
        if self.poisoned or self.gateway is None or self.gateway.poll() is not None:
            raise RuntimeError(
                "Runtime is not active or has an unresolved prior request"
            )
        if not self.request_lock.acquire(blocking=False):
            raise RuntimeError("Requests must be sequential within one runtime")
        try:
            return self._request(
                prompt,
                request_id=request_id,
                component=component,
                trajectory_id=trajectory_id,
                previous_response_id=previous_response_id,
                max_output_tokens=max_output_tokens,
            )
        finally:
            self.request_lock.release()

    def _request(
        self,
        prompt: str,
        *,
        request_id: str,
        component: str,
        trajectory_id: str,
        previous_response_id: str | None,
        max_output_tokens: int,
    ) -> dict:
        context = {
            "run_id": self.run_id,
            "component": component,
            "trajectory_id": trajectory_id,
            "request_id": request_id,
            "backend_instance_id": self.backend_instance_id,
        }
        _write_json(self.state_dir / "request-context.json", context)
        self.proxy.begin(context)
        body = {
            "model": "openai/" + self.model,
            "input": prompt,
            "instructions": self.instructions,
            "tools": [
                {
                    "type": "mcp",
                    "server_label": self.domain + "-unsafe",
                    "server_url": f"http://127.0.0.1:{self.mcp_port}/sse",
                    "require_approval": "never",
                }
            ],
            "stream": False,
            "max_output_tokens": max_output_tokens,
            "max_tool_calls": self.max_tool_calls,
        }
        if previous_response_id:
            body["previous_response_id"] = previous_response_id
        try:
            self.recorder(
                {
                    **context,
                    "role": "target",
                    "event_type": "target_request",
                    "status": "dispatched",
                    "visibility": "online_observation",
                    "payload": {
                        "input": prompt,
                        "instructions": self.instructions,
                        "previous_response_id": previous_response_id,
                    },
                }
            )
            response = self.client.request(body)
            self.recorder(
                {
                    **context,
                    "role": "target",
                    "event_type": "target_response",
                    "status": response.get("status", "completed"),
                    "visibility": "online_observation",
                    "payload": response,
                }
            )
            return response
        except GatewayError:
            # A timed-out OGX loop can continue mutating state. Stop its gateway
            # and backend before allowing any later trajectory to be considered.
            self.poisoned = True
            self._stop(self.gateway)
            self._stop(self.backend)
            raise
        finally:
            self._drain_events()

    def snapshot(self) -> dict:
        """Read evaluator-only native backend state without exposing an MCP observer."""
        return json.loads((self.state_dir / "snapshot.json").read_text())

    def tools(self) -> list[dict]:
        """Return actual target-visible MCP schemas, excluding evaluator observers."""
        return json.loads((self.state_dir / "tools.json").read_text())

    def reset(self) -> None:
        """Restart the backend with its built-in data, preserving no ledger effects."""
        if self.poisoned:
            raise RuntimeError("Create a new runtime after an indeterminate request")
        self._stop(self.backend)
        self._drain_events()
        context = self.state_dir / "request-context.json"
        context.unlink(missing_ok=True)
        self._start_backend()

    def close(self) -> None:
        """Stop only processes and relay owned by this runtime."""
        self._stop(self.gateway)
        self._stop(self.backend)
        if self.proxy is not None:
            self.proxy.close()
            self.proxy = None
        self._drain_events()

    def __exit__(self, *_args):
        self.close()


def main() -> None:
    """Launch the instrumented unsafe backend child process."""
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["serve"])
    parser.add_argument("--domain", choices=DOMAINS, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--max-tool-calls", type=int, required=True)
    args = parser.parse_args()
    server = build_instrumented_server(args.domain, args.state_dir, args.max_tool_calls)
    server.settings.host = "127.0.0.1"
    server.settings.port = args.port
    server.run(transport="sse")


if __name__ == "__main__":
    main()
