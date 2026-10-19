"""Normalize OGX 1.4 Responses evidence without running or inferring tool calls.

MiniKlarna MCP tools return JSON text. OGX flattens that text into an MCP
output item's ``output`` string; protocol failures instead populate ``error``.
An executed flag describes returned MCP evidence, never a successful refund.
Only the independent ledger oracle can establish an applied business action.
"""

from __future__ import annotations

import copy
import json
import math
from typing import Any


def _error(kind: str, message: str) -> dict:
    return {"type": kind, "message": message}


def _reject_constant(_value: str):
    raise ValueError("Non-finite JSON number")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Ambiguous duplicate JSON key")
        result[key] = value
    return result


def _finite(value: Any) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(_finite(item) for item in value.values())
    if isinstance(value, list):
        return all(_finite(item) for item in value)
    return True


def _json_object(raw: Any) -> dict | None:
    if not isinstance(raw, str):
        return None
    try:
        value = json.loads(raw, parse_constant=_reject_constant, object_pairs_hook=_unique_object)
        return value if isinstance(value, dict) and _finite(value) else None
    except (ValueError, TypeError, RecursionError):
        return None


def _parse_call(item: dict) -> tuple[dict, list[dict]]:
    arguments = _json_object(item.get("arguments"))
    result = _json_object(item.get("output"))
    errors = []
    record = {"name": item.get("name"), "arguments": arguments,
              "raw_arguments": copy.deepcopy(item.get("arguments")),
              "result": result, "raw_output": copy.deepcopy(item.get("output")),
              "executed": None, "mcp": {key: copy.deepcopy(item.get(key)) for key in
                  ("id", "type", "server_label", "error")}}
    for key in ("status", "call_id", "approval_request_id"):
        if key in item:
            record["mcp"][key] = copy.deepcopy(item[key])
    if arguments is None:
        errors.append(_error("invalid_tool_arguments", "Tool arguments are not an unambiguous finite JSON object."))
    if not isinstance(item.get("name"), str) or not item["name"]:
        errors.append(_error("malformed_mcp_call", "Tool call has no valid name."))

    if item["type"] != "mcp_call":
        record["executed"] = False
        errors.append(_error("unexecuted_tool_call", "OGX returned a client-side call or an approval request without execution."))
    else:
        if any(not isinstance(item.get(key), str) or not item[key] for key in ("id", "server_label")):
            errors.append(_error("malformed_mcp_call", "MCP call lacks its ID or server label."))
        if item.get("error") is not None:
            message = item["error"] if isinstance(item["error"], str) else "OGX reported an MCP protocol failure."
            errors.append(_error("mcp_tool_error", message))
        if result is None:
            errors.append(_error("invalid_tool_result", "MCP output is missing or is not an unambiguous finite JSON object."))
        # OGX 1.4 has no MCPCall.status. Preserve/reject unfinished status if a
        # compatible server adds it, but never require an invented status field.
        if item.get("status") not in (None, "completed"):
            errors.append(_error("incomplete_tool_call", "MCP call did not finish successfully."))
        if not errors:
            record["executed"] = True
    if errors:
        record["error"] = errors[0]
    return record, errors


def _usage(raw: Any) -> dict:
    usage = copy.deepcopy(raw) if isinstance(raw, dict) else {}
    # A Responses document can aggregate several inference rounds. Neither
    # the number of MCP calls nor one HTTP response gives their exact count.
    usage["model_calls"] = None
    valid = {}
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        value = usage.get(key)
        valid[key] = isinstance(value, int) and not isinstance(value, bool) and value >= 0
    for original, normalized in (("input_tokens", "prompt_tokens"), ("output_tokens", "completion_tokens")):
        usage.pop(normalized, None)
        if valid[original]:
            usage[normalized] = usage[original]
    usage["complete"] = all(valid.values())
    if usage["complete"]:
        usage["complete"] = usage["input_tokens"] + usage["output_tokens"] == usage["total_tokens"]
    return usage


def parse_response(payload: dict) -> dict:
    """Return text, MCP calls, failure status, usage, and original metadata.

    ``response`` joins assistant output_text parts in wire order. Refusals,
    reasoning, tool discovery, and all original output items remain in
    ``response_metadata``. Plain text is never interpreted as tool syntax.
    Malformed or unfinished records remain evidence with an explicit error;
    uncertain execution is ``None``, not a claim that no action happened.
    """
    parsed = {"response": "", "calls": [], "error": None,
              "usage": _usage(None), "response_metadata": {}}
    if not isinstance(payload, dict):
        parsed["error"] = _error("malformed_response", "OGX response is not an object.")
        return parsed
    metadata = copy.deepcopy({key: value for key, value in payload.items() if key not in {"output", "usage"}})
    metadata.update(output_items=copy.deepcopy(payload.get("output")), refusals=[], parse_errors=[])
    parsed["response_metadata"] = metadata
    parsed["usage"] = _usage(payload.get("usage"))

    error = payload.get("error")
    status = payload.get("status")
    if error is not None or status == "failed":
        message = error.get("message") if isinstance(error, dict) else None
        parsed["error"] = _error("ogx_response_error", message if isinstance(message, str) else "OGX response failed.")
    elif status in ("incomplete", "cancelled", "queued", "in_progress"):
        details = payload.get("incomplete_details")
        reason = details.get("reason") if isinstance(details, dict) else None
        parsed["error"] = _error("incomplete_response", f"OGX response status is {status}" +
                                 (f": {reason}." if isinstance(reason, str) else "."))
    elif status != "completed":
        parsed["error"] = _error("malformed_response", "OGX response has no recognized completion status.")

    output = payload.get("output")
    if not isinstance(output, list):
        parsed["error"] = parsed["error"] or _error("malformed_response", "OGX output is not a list.")
        return parsed
    texts = []
    for index, item in enumerate(output):
        errors = []
        if not isinstance(item, dict):
            errors.append(_error("malformed_response", "OGX output item is not an object."))
        elif item.get("type") in ("mcp_call", "function_call", "mcp_approval_request"):
            record, errors = _parse_call(item)
            parsed["calls"].append(record)
        elif item.get("type") == "message":
            if item.get("role") == "assistant":
                if item.get("status") not in (None, "completed"):
                    errors.append(_error("incomplete_response", "An assistant output message is unfinished."))
                content = item.get("content")
                if isinstance(content, str):
                    texts.append(content)
                elif isinstance(content, list):
                    for part in content:
                        kind = part.get("type") if isinstance(part, dict) else None
                        key = {"output_text": "text", "refusal": "refusal"}.get(kind) if isinstance(kind, str) else None
                        if key is None or not isinstance(part.get(key), str):
                            errors.append(_error("malformed_response", "Assistant output contains an invalid text part."))
                        elif kind == "output_text":
                            texts.append(part[key])
                        else:
                            metadata["refusals"].append(part[key])
                else:
                    errors.append(_error("malformed_response", "Assistant output has no text content."))
            elif item.get("role") not in ("system", "developer", "user"):
                errors.append(_error("malformed_response", "Output message has no recognized role."))
        elif item.get("type") not in ("reasoning", "mcp_list_tools"):
            errors.append(_error("unsupported_output", "OGX returned an unsupported output item."))
        for issue in errors:
            metadata["parse_errors"].append({"output_index": index, **issue})
            parsed["error"] = parsed["error"] or issue
    parsed["response"] = "".join(texts)
    return parsed
