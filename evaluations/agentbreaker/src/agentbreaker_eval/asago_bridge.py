"""Explicitly limited Asago ToolChat -> AgentBreaker seed projection.

This is not ToolChat replay. No generated system prompt or fabricated tool
history is installed on the real OGX/MCP target.
"""

from __future__ import annotations

import hashlib
import json
import re

TOOLS = {"lookup_order", "get_account_details", "retrieve_policy", "schedule_payment",
         "process_refund", "escalate_to_human", "get_klarna_state_summary"}
FIXTURE_IDS = {"ORD-101", "ORD-102", "ORD-103", "ORD-104", "ORD-201",
               "CUST001", "CUST002", "PLAN-12", "PLAN-13", "PLAN-21"}


def project_user_seed(raw: bytes, validation: dict, scenario: dict) -> dict:
    """Apply independent admission gates; human/source grounding review follows.

    A positive result is a candidate for review, not comprehensive semantic
    validation. Native validation remains an independently saved upstream result.
    """
    result = {"scenario_id": scenario["id"], "artifact_sha256": hashlib.sha256(raw).hexdigest(),
              "integration": "asago_seed_projection", "supported": False,
              "grounding_review_required": True, "target_system_prompt_supplied": False,
              "reasons": [], "projection": "only the final user text from a system+user artifact"}

    def reject(reason):
        result["reasons"].append(reason)
        return result

    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return reject("malformed_artifact_json")
    if not isinstance(data, dict):
        return reject("artifact_not_object")
    result["platform_coverage"] = data.get("platform_coverage")
    result["injection_surface"] = data.get("injection_surface")
    if validation.get("ok") is not True or validation.get("errors"):
        return reject("native_structural_validation_failed_or_missing")
    if data.get("scenario_id") != scenario["id"]:
        return reject("scenario_id_mismatch")
    if data.get("platform_coverage") not in {"full", "partial"}:
        return reject("native_no_coverage")
    if data.get("injection_surface") != "user_turn":
        return reject("unsupported_injection_surface")
    turns = data.get("turns")
    if (not isinstance(turns, list) or len(turns) != 2 or
            any(not isinstance(turn, dict) for turn in turns) or
            [turn.get("role") for turn in turns] != ["system", "user"] or
            any(turn.get("tool_calls") or turn.get("tool_call_id") for turn in turns)):
        return reject("contextual_or_tool_history_requires_different_replay_adapter")
    if data.get("attack_turn_index") != 1 or turns[-1].get("adversarial") is not True:
        return reject("invalid_attack_turn")
    if any(not isinstance(turn.get("content"), str) or not turn["content"].strip() for turn in turns):
        return reject("empty_or_invalid_content")
    declared = set(re.findall(r"^- (\w+)\(", turns[0]["content"], re.M))
    if scenario["tool"] not in declared or not declared <= TOOLS:
        return reject("tool_catalog_drift")
    fixture_tokens = set(re.findall(r"\b(?:ORD-\d+|CUST-?\d+|PLAN-\d+)\b", "\n".join(t["content"] for t in turns)))
    unknown = sorted(fixture_tokens - FIXTURE_IDS)
    if unknown:
        result["unknown_fixture_ids"] = unknown
        return reject("fixture_id_drift")
    rubric = data.get("detector_rubric")
    if not isinstance(rubric, dict) or not isinstance(rubric.get("judge_prompt"), str) or not rubric["judge_prompt"].strip():
        return reject("missing_native_rubric")
    rubrics = rubric.get("rubrics")
    if not isinstance(rubrics, dict) or any(not isinstance(rubrics.get(key), list) or not rubrics[key]
                                          for key in ("attack_success", "attack_blocked")):
        return reject("missing_native_rubric")
    result.update(supported=True, seed=turns[-1]["content"], declared_tools=sorted(declared),
                  native_rubric_preserved_but_not_executed=True)
    return result


def coverage_rows(scenarios: list[dict], decisions: dict, traces: list[dict]) -> list[dict]:
    """Keep every requested scenario/mode, regardless of admission or execution."""
    def any_known(assessments, field):
        values = [a.get(field) for a in assessments]
        return True if True in values else False if values and all(v is False for v in values) else None

    rows = []
    for scenario in scenarios:
        decision = decisions.get(scenario["id"], {})
        for mode in ("safe", "unsafe"):
            matching = [t for t in traces if t.get("scenario_id") == scenario["id"] and t.get("mode") == mode]
            assessments = [t.get("scenario_assertion", {}) for t in matching]
            rows.append({"scenario_id": scenario["id"], "category": scenario.get("category"),
                         "mode": mode, "seed_supported": decision.get("supported", False),
                         "admission_reasons": decision.get("reasons", []),
                         "target_requests": len(matching), "runtime_errors": sum(bool(t.get("error")) for t in matching),
                         "trace_ids": [t["trace_id"] for t in matching],
                         "attempted": any_known(assessments, "attempted"),
                         "executed": any_known(assessments, "executed"),
                         "achieved": any_known(assessments, "achieved"),
                         "classifications": [a.get("classification", "unassessed") for a in assessments],
                         "unknown_assertions": sum(a.get("executed") is None for a in assessments)})
    return rows
