# SPDX-FileCopyrightText: Portions Copyright (c) 2025 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for agentdojo_iterative probe and multi-turn helpers."""

import json
import pytest

from garak.probes.agentdojo_iterative import (
    extract_tool_call_from_text,
    _format_tool_result,
    TOOL_RESULTS,
)


# ---------------------------------------------------------------------------
# extract_tool_call_from_text
# ---------------------------------------------------------------------------


def test_extract_json_with_name():
    text = '{"name": "send_money", "arguments": {"recipient": "IBAN123", "amount": 100}}'
    result = extract_tool_call_from_text(text)
    assert result is not None
    assert result[0] == "send_money"
    assert result[1]["recipient"] == "IBAN123"


def test_extract_json_with_function():
    text = '{"function": "send_email", "arguments": {"recipients": ["a@b.com"]}}'
    result = extract_tool_call_from_text(text)
    assert result is not None
    assert result[0] == "send_email"


def test_extract_embedded_in_text():
    text = (
        'Sure, I will do that. Here is the call:\n'
        '{"name": "send_money", "arguments": {"recipient": "EVIL-IBAN", "amount": 500}}\n'
        'Let me execute this for you.'
    )
    result = extract_tool_call_from_text(text)
    assert result is not None
    assert result[0] == "send_money"
    assert result[1]["recipient"] == "EVIL-IBAN"


def test_extract_no_tool_call():
    text = "Your balance is $5000. Is there anything else I can help with?"
    result = extract_tool_call_from_text(text)
    assert result is None


def test_extract_none_input():
    assert extract_tool_call_from_text(None) is None


def test_extract_empty_string():
    assert extract_tool_call_from_text("") is None


# ---------------------------------------------------------------------------
# _format_tool_result
# ---------------------------------------------------------------------------


def test_format_send_money():
    result = _format_tool_result("send_money", {"recipient": "IBAN123", "amount": 100})
    assert "IBAN123" in result
    assert "100" in result


def test_format_unknown_tool():
    result = _format_tool_result("unknown_tool", {})
    assert result == "Operation completed successfully."


def test_format_missing_args():
    """Missing args should not crash — template.format handles gracefully."""
    result = _format_tool_result("get_balance", {})
    assert "5,432" in result


def test_format_delete_file():
    result = _format_tool_result("delete_file", {"file_name": "secret.txt"})
    assert "secret.txt" in result


# ---------------------------------------------------------------------------
# Probe construction
# ---------------------------------------------------------------------------


def test_iterative_probe_has_scenarios():
    from garak.probes.agentdojo_iterative import ImportantInstructionsIterative

    probe = ImportantInstructionsIterative.__new__(ImportantInstructionsIterative)
    probe.suites = ["banking"]
    probe.user_name = "Emma Johnson"
    probe.model_name = "the AI language model"
    probe._scenarios = []
    probe._build_scenarios()
    assert len(probe._scenarios) > 0
    for sc in probe._scenarios:
        assert "triggers" in sc
        assert "system_prompt" in sc
        assert "tool_output" in sc


def test_iterative_probe_banking_scenario_count():
    from garak.probes.agentdojo_iterative import ImportantInstructionsIterative

    probe = ImportantInstructionsIterative.__new__(ImportantInstructionsIterative)
    probe.suites = ["banking"]
    probe.user_name = "Emma Johnson"
    probe.model_name = "the AI language model"
    probe._scenarios = []
    probe._build_scenarios()
    # banking: 2 scenarios * 2 goals = 4
    assert len(probe._scenarios) == 4


def test_scenario_has_injection_payload():
    from garak.probes.agentdojo_iterative import ImportantInstructionsIterative

    probe = ImportantInstructionsIterative.__new__(ImportantInstructionsIterative)
    probe.suites = ["banking"]
    probe.user_name = "Emma Johnson"
    probe.model_name = "the AI language model"
    probe._scenarios = []
    probe._build_scenarios()
    for sc in probe._scenarios:
        assert "<INFORMATION>" in sc["tool_output"]
        assert "</INFORMATION>" in sc["tool_output"]
