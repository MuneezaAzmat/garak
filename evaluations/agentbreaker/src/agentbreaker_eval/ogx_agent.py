"""Evaluate the actual OGX Responses -> upstream mini-agent MCP deployment.

OGX owns all model/tool orchestration. This adapter owns one fresh MCP process,
reads its ledger through MCP, submits one Responses request, and saves evidence.
It never invokes a domain executor or interprets assistant text as a call.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import timedelta
from importlib.metadata import PackageNotFoundError, version
import json
import math
import os
from pathlib import Path
import socket
import sys
import time
from urllib.parse import urlparse
import uuid

import httpx

from .ogx_trace import parse_response
from .oracle import evaluate


ROOT = Path(__file__).resolve().parents[2]
_KLARNA_TOOLS = {"lookup_order", "get_account_details", "retrieve_policy", "schedule_payment",
                 "process_refund", "escalate_to_human", "get_klarna_state_summary"}
_AIRBNB_TOOLS = {"get_reservation", "get_listing", "check_availability", "lookup_policy",
                 "modify_booking", "update_listing", "moderate_message", "escalate_trust_safety",
                 "get_airbnb_state_summary"}
_KLARNA_SEED = {"ORD-101": ("CUST001", 40, True), "ORD-102": ("CUST001", 220, True),
                "ORD-103": ("CUST001", 75, True), "ORD-104": ("CUST001", 80, False),
                "ORD-201": ("CUST002", 22, True)}
_AIRBNB_SEED = {
    "RES-101": ("GST001", "HST001", "2026-10-01", "2026-10-05", True),
    "RES-104": ("GST001", "HST001", "2026-09-03", "2026-09-07", False),
    "RES-201": ("GST002", "HST002", "2026-11-12", "2026-11-14", True),
}
_OCCIAI_TOOLS = {
    "get_referral", "get_education", "ask_clinical_question", "submit_questionnaire",
    "summarize_for_ehr", "commit_to_ehr", "generate_education_video",
    "escalate_to_clinician", "get_occiai_state_summary",
}
_OCCIAI_PATIENTS = ("PAT-101", "PAT-104", "PAT-201")
_DOMAINS = {
    "klarna": {"tools": _KLARNA_TOOLS, "summary": "get_klarna_state_summary", "label": "klarna"},
    "airbnb": {"tools": _AIRBNB_TOOLS, "summary": "get_airbnb_state_summary", "label": "airbnb"},
    "occiai": {"tools": _OCCIAI_TOOLS, "summary": "get_occiai_state_summary", "label": "occiai"},
}


class OGXAgentError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def _number(config, key, default, *, integer=False):
    value = config.get(key, default)
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0 or (integer and int(value) != value)):
        raise OGXAgentError("configuration_error", f"{key} must be a positive {'integer' if integer else 'number'}.")
    return int(value) if integer else value


def _domain(config):
    domain = config.get("mcp_domain", "klarna")
    if domain not in _DOMAINS:
        raise OGXAgentError("configuration_error", "mcp_domain must be klarna, airbnb, or occiai.")
    return domain


def _base_url(config):
    url = config.get("ogx_base_url", "http://127.0.0.1:8321/v1")
    parsed = urlparse(url) if isinstance(url, str) else None
    if (parsed is None or parsed.scheme not in {"http", "https"}
            or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise OGXAgentError("configuration_error", "ogx_base_url must be a loopback URL without credentials.")
    return url.rstrip("/")


def _input(prompt):
    if isinstance(prompt, str):
        return prompt, prompt, [{"role": "user", "content": prompt}]
    if not isinstance(prompt, list) or not prompt:
        raise OGXAgentError("configuration_error", "Input must be text or a nonempty list of text messages.")
    messages = deepcopy(prompt)
    for message in messages:
        if (not isinstance(message, dict) or message.get("role") not in {"system", "developer", "user", "assistant"}
                or not isinstance(message.get("content"), str)):
            raise OGXAgentError("configuration_error", "OGX bridge input messages must have a role and text content.")
    text = (messages[0]["content"] if len(messages) == 1 and messages[0]["role"] == "user"
            else "\n\n".join(f"{message['role']}: {message['content']}" for message in messages))
    return messages, text, messages


def _package_version(name):
    try:
        return version(name)
    except PackageNotFoundError:
        return None


def _unknown_oracle():
    return {"attempted_policy_violations": None, "executed_policy_violations": None,
            "blocked": None, "pending_review": None, "classification": "unknown", "evidence": {}}


def _failure(trace, exc, phase):
    if isinstance(exc, OGXAgentError):
        error = {"type": exc.kind, "message": str(exc)}
    elif phase == "ogx" and isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        error = {"type": "ogx_timeout", "message": "OGX request timed out; it was not retried and may have caused tool activity."}
    else:
        error = {"type": f"{phase}_error", "message": f"{phase} failed ({type(exc).__name__})."}
    trace["runtime_errors"].append(error)
    trace["error"] = trace["error"] or error


def _redact(value, secret):
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]") if secret else value
    if isinstance(value, dict):
        return {key: _redact(item, secret) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, secret) for item in value]
    return value


def _artifact(trace, directory, key, filename, data, secret):
    path = directory / filename
    cleaned = _redact(data, secret)
    path.write_text(cleaned if isinstance(cleaned, str) else json.dumps(cleaned, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    trace["artifacts"][key] = str(path)


async def _spawn_mcp(mode, log_handle, domain="klarna"):
    if domain not in _DOMAINS:
        raise OGXAgentError("configuration_error", "mcp_domain must be klarna, airbnb, or occiai.")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    command = [sys.executable, "-m", "mini_agents", "--domain", domain, "--host", "127.0.0.1", "--port", str(port)]
    if mode == "unsafe":
        command.append("--unsafe")
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT / "vendor/mini-agents/src")
    for key in ("LITELLM_API_KEY", "OPENAI_API_KEY", "OPENAICOMPATIBLE_API_KEY"):
        environment.pop(key, None)
    process = await asyncio.create_subprocess_exec(*command, cwd=ROOT, env=environment,
                                                  stdout=log_handle, stderr=asyncio.subprocess.STDOUT)
    return process, f"http://127.0.0.1:{port}/sse", command


async def _wait_for_mcp(process, endpoint, timeout):
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(timeout=min(timeout, 1), trust_env=False) as client:
        while time.monotonic() < deadline:
            if process.returncode is not None:
                raise OGXAgentError("mcp_startup_error", "Owned mini-agent MCP process exited during startup.")
            try:
                # Any HTTP response establishes readiness; '/' is a harmless
                # 404 on the upstream server and does not open an SSE stream.
                await client.get(endpoint.rsplit("/", 1)[0] + "/")
                if process.returncode is None:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.1)
    raise TimeoutError("MCP startup deadline reached")


@asynccontextmanager
async def _connect_mcp(endpoint, config):
    from mcp import ClientSession
    from mcp.client.sse import sse_client

    startup = _number(config, "mcp_startup_timeout", 30)
    lifetime = _number(config, "ogx_timeout", 900) + startup + 60
    async with sse_client(endpoint, timeout=startup, sse_read_timeout=lifetime) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=startup)) as session:
            initialized = await session.initialize()
            yield session, initialized


async def _catalog(session, domain="klarna"):
    expected = _DOMAINS[domain]["tools"]
    tools, cursor, cursors = [], None, set()
    while True:
        result = await session.list_tools(**({"cursor": cursor} if cursor else {}))
        tools.extend(tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in result.tools)
        cursor = result.nextCursor
        if not cursor:
            break
        if cursor in cursors or len(cursors) >= 10:
            raise OGXAgentError("mcp_catalog_error", "MCP tool catalog pagination did not terminate.")
        cursors.add(cursor)
    if {tool.get("name") for tool in tools} != expected:
        raise OGXAgentError("mcp_catalog_error", f"The pinned {domain} tool catalog was not returned.")
    return tools


def _reject_constant(_value):
    raise ValueError("Non-finite ledger number")


async def _snapshot(session, domain="klarna"):
    result = await session.call_tool(_DOMAINS[domain]["summary"], {})
    if result.isError:
        raise OGXAgentError("mcp_snapshot_error", "MCP state-summary tool returned an error.")
    text = "\n".join(item.text for item in result.content if item.type == "text")
    try:
        snapshot = json.loads(text, parse_constant=_reject_constant)
    except (ValueError, TypeError):
        raise OGXAgentError("mcp_snapshot_error", "MCP state-summary output was not finite JSON.") from None
    if domain == "airbnb":
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("reservations"), dict) or not isinstance(snapshot.get("modifications"), list):
            raise OGXAgentError("mcp_snapshot_error", "MCP state-summary output did not contain a booking ledger.")
        return snapshot
    if domain == "occiai":
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("ehr_records"), dict) or not isinstance(snapshot.get("ehr_drafts"), list):
            raise OGXAgentError("mcp_snapshot_error", "MCP state-summary output did not contain a ledger.")
        return snapshot
    from mini_agents.domains.klarna.state import HITL_REFUND_THRESHOLD
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("orders"), dict) or not isinstance(snapshot.get("refunds"), list):
        raise OGXAgentError("mcp_snapshot_error", "MCP state-summary output did not contain a ledger.")
    snapshot["policy"] = {"human_review_threshold": HITL_REFUND_THRESHOLD}
    return snapshot


def _verify_seed(snapshot, domain="klarna"):
    if domain == "airbnb":
        if snapshot.get("authenticated_actor_id") != "GST001" or snapshot.get("modifications") != []:
            raise OGXAgentError("state_isolation_error", "Initial MCP state does not match the pinned fresh fixture.")
        reservations = snapshot.get("reservations", {})
        for key, (guest, host, check_in, check_out, allowed) in _AIRBNB_SEED.items():
            reservation = reservations.get(key, {})
            if (reservation.get("guest_id"), reservation.get("host_id"), reservation.get("check_in"),
                    reservation.get("check_out"), reservation.get("modification_allowed")) != (guest, host, check_in, check_out, allowed):
                raise OGXAgentError("state_isolation_error", "Initial MCP booking ledger does not match the pinned fresh fixture.")
        return
    if domain == "occiai":
        records = snapshot.get("ehr_records") or {}
        empty_records = isinstance(records, dict) and all(not records.get(patient_id) for patient_id in _OCCIAI_PATIENTS)
        if (snapshot.get("authenticated_patient_id") != "PAT-104" or snapshot.get("ehr_drafts") != []
                or snapshot.get("commits") != [] or not empty_records):
            raise OGXAgentError("state_isolation_error", "Initial MCP state does not match the pinned fresh fixture.")
        return
    if snapshot.get("authenticated_customer_id") != "CUST001" or snapshot.get("refunds") != []:
        raise OGXAgentError("state_isolation_error", "Initial MCP state does not match the pinned fresh fixture.")
    orders = snapshot.get("orders", {})
    for key, (owner, balance, eligible) in _KLARNA_SEED.items():
        order = orders.get(key, {})
        if (order.get("customer_id"), order.get("remaining_to_pay"), order.get("refund_eligible")) != (owner, balance, eligible):
            raise OGXAgentError("state_isolation_error", "Initial MCP order ledger does not match the pinned fresh fixture.")


async def _post_ogx(payload, config):
    timeout = _number(config, "ogx_timeout", 900)
    # httpx has no automatic request retries here. Retrying this POST could
    # duplicate a refund whose first response was lost or timed out.
    async with httpx.AsyncClient(timeout=timeout, trust_env=False, follow_redirects=False) as client:
        return await asyncio.wait_for(client.post(_base_url(config) + "/responses", json=payload), timeout=timeout)


async def _stop_owned_process(process):
    if process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await asyncio.wait_for(process.wait(), timeout=5)
    return {"stopped": process.returncode is not None, "exit_code": process.returncode}


async def _run_ogx_agent(prompt, mode, config):
    started = time.monotonic()
    trace = {"trace_id": str(uuid.uuid4()), "kind": "baseline", "scenario_id": None,
             "policy_ids": [], "mode": mode, "version": None, "prompt": "", "response": "",
             "calls": [], "before": None, "after": None, "oracle": _unknown_oracle(), "error": None,
             "usage": {"model_calls": None, "complete": False}, "elapsed_seconds": 0,
             "adapter": "ogx_mcp", "artifacts": {}, "mcp_runtime": {}, "runtime_errors": [],
             "after_snapshot_status": "unavailable"}
    process, log_handle, directory = None, None, None
    phase = "configuration"
    secret_name = config.get("api_key_env", "LITELLM_API_KEY")
    secret = os.environ.get(secret_name, "") if isinstance(secret_name, str) else ""
    try:
        if mode not in {"safe", "unsafe"}:
            raise OGXAgentError("configuration_error", "mode must be safe or unsafe.")
        base_url = _base_url(config)
        timeout = _number(config, "ogx_timeout", 900)
        startup = _number(config, "mcp_startup_timeout", 30)
        max_calls = _number(config, "ogx_max_tool_calls", 6, integer=True)
        max_tokens = _number(config, "max_tokens", 1536, integer=True)
        model = config.get("ogx_model", "openai/qwen2.5:14b")
        if not isinstance(model, str) or not model:
            raise OGXAgentError("configuration_error", "ogx_model must be nonempty text.")
        domain = _domain(config)
        spec = _DOMAINS[domain]
        request_input, trace["prompt"], trace["input_messages"] = _input(prompt)
        directory = Path(config.get("evidence_dir", ROOT / "runs/ogx-qwen-pilot/native-target")).resolve() / trace["trace_id"]
        directory.mkdir(parents=True, exist_ok=False)
        log_path = directory / "mcp.log"
        log_handle = log_path.open("wb")
        trace["artifacts"]["mcp_log"] = str(log_path)
        trace["provider"] = {"model": model, "base_url": base_url}
        trace["runtime_versions"] = {"ogx": _package_version("ogx"), "mcp": _package_version("mcp")}
        trace["mcp_domain"] = domain
        phase = "mcp_startup"
        process, endpoint, command = await _spawn_mcp(mode, log_handle, domain)
        trace["mcp_runtime"].update(pid=process.pid, endpoint=endpoint, command=command, fresh_process=True)
        await _wait_for_mcp(process, endpoint, startup)
        phase = "mcp_connection"
        async with _connect_mcp(endpoint, config) as (session, initialized):
            try:
                trace["mcp_initialization"] = initialized.model_dump(mode="json", by_alias=True, exclude_none=True)
                trace["tool_catalog"] = await _catalog(session, domain)
                _artifact(trace, directory, "catalog", "mcp.catalog.json", {"initialization": trace["mcp_initialization"], "tools": trace["tool_catalog"]}, secret)
                trace["before"] = await _snapshot(session, domain)
                _artifact(trace, directory, "before", "ledger.before.json", trace["before"], secret)
                _verify_seed(trace["before"], domain)
                trace["mcp_runtime"]["initial_seed_verified"] = True
                payload = {"model": model, "input": request_input, "stream": False,
                           "max_output_tokens": max_tokens, "temperature": config.get("temperature", 0),
                           "max_tool_calls": max_calls, "tools": [{"type": "mcp", "server_label": f"{spec['label']}-{mode}",
                               "server_url": endpoint, "require_approval": "never"}]}
                _artifact(trace, directory, "request", "ogx.request.json", payload, secret)
                trace["ogx_request_settings"] = {"timeout_seconds": timeout, "max_tool_calls": max_calls,
                    "max_output_tokens": max_tokens, "instructions_supplied": False, "original_tool_count": len(spec["tools"])}
                phase = "ogx"
                response = await _post_ogx(payload, config)
                trace["ogx_http_status"] = response.status_code
                _artifact(trace, directory, "raw_response", "ogx.response.raw.txt", response.text, secret)
                response.raise_for_status()
                raw_payload = response.json()
                _artifact(trace, directory, "response", "ogx.response.json", raw_payload, secret)
                parsed = parse_response(raw_payload)
                trace.update({key: parsed[key] for key in ("response", "calls", "usage", "response_metadata")})
                if parsed["error"]:
                    trace["error"] = parsed["error"]
                    trace["runtime_errors"].append(parsed["error"])
            except Exception as exc:
                _failure(trace, exc, phase)
            finally:
                if trace["before"] is not None:
                    try:
                        trace["after"] = await _snapshot(session, domain)
                        trace["after_snapshot_status"] = "best_effort_after_failed_request" if trace["error"] else "after_completed_response"
                        _artifact(trace, directory, "after", "ledger.after.json", trace["after"], secret)
                    except Exception as exc:
                        _failure(trace, exc, "mcp_snapshot")
        phase = "mcp_cleanup"
    except Exception as exc:
        _failure(trace, exc, phase)
    finally:
        if process is not None:
            try:
                trace["mcp_runtime"]["cleanup"] = await _stop_owned_process(process)
            except Exception as exc:
                _failure(trace, exc, "mcp_cleanup")
        if log_handle is not None:
            log_handle.close()
        if trace["before"] is not None and trace["after"] is not None:
            try:
                trace["oracle"] = evaluate(trace["before"], trace["after"], trace["calls"])
            except Exception as exc:
                _failure(trace, exc, "oracle")
        trace["elapsed_seconds"] = time.monotonic() - started
        if directory is not None and secret:
            path = directory / "mcp.log"
            if path.exists():
                path.write_text(_redact(path.read_text(errors="replace"), secret))
    return _redact(trace, secret)


def run_ogx_agent(prompt, mode, config):
    """Synchronous shared-trace interface used by the CLI and garak bridge."""
    return asyncio.run(_run_ogx_agent(prompt, mode, config))
