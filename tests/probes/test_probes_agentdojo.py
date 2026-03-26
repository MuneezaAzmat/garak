# SPDX-FileCopyrightText: Portions Copyright (c) 2025 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for garak.probes.agentdojo and garak.detectors.agentdojo"""

import pytest

import garak._plugins
from garak.attempt import Attempt, Conversation, Message, Turn
from garak.probes.agentdojo import (
    ATTACK_TEMPLATES,
    SUITE_SCENARIOS,
    AgentDojoBase,
    DirectInjection,
    IgnorePrevious,
    ImportantInstructions,
    InjecAgent,
    SystemMessageInjection,
)
from garak.detectors.agentdojo import InjectionFollowed, InjectionRefused


# ---------------------------------------------------------------------------
# Probe tests
# ---------------------------------------------------------------------------

PROBE_CLASSES = [
    DirectInjection,
    IgnorePrevious,
    ImportantInstructions,
    SystemMessageInjection,
    InjecAgent,
]


@pytest.mark.parametrize("klass", PROBE_CLASSES)
def test_probe_loads_via_plugin(klass):
    """Probe can be loaded through garak's plugin loader."""
    name = f"probes.agentdojo.{klass.__name__}"
    p = garak._plugins.load_plugin(name)
    assert isinstance(p, AgentDojoBase)


@pytest.mark.parametrize("klass", PROBE_CLASSES)
def test_probe_has_prompts(klass):
    """Each probe should generate a non-empty list of Conversation prompts."""
    p = klass()
    assert len(p.prompts) > 0, f"{klass.__name__} generated no prompts"


@pytest.mark.parametrize("klass", PROBE_CLASSES)
def test_prompts_are_conversations(klass):
    """Each prompt should be a Conversation with 3 turns: system, user, assistant."""
    p = klass()
    for i, prompt in enumerate(p.prompts):
        assert isinstance(prompt, Conversation), (
            f"Prompt {i} is {type(prompt)}, expected Conversation"
        )
        assert len(prompt.turns) == 3, (
            f"Prompt {i} has {len(prompt.turns)} turns, expected 3"
        )
        assert prompt.turns[0].role == "system"
        assert prompt.turns[1].role == "user"
        assert prompt.turns[2].role == "assistant"


def test_important_instructions_payload_embedded():
    """The <INFORMATION> payload should appear in the assistant turn."""
    p = ImportantInstructions()
    for prompt in p.prompts:
        assistant_text = prompt.turns[2].content.text
        assert "<INFORMATION>" in assistant_text
        assert "</INFORMATION>" in assistant_text


def test_direct_injection_payload_embedded():
    """The 'TODO:' payload should appear in the assistant turn."""
    p = DirectInjection()
    for prompt in p.prompts:
        assistant_text = prompt.turns[2].content.text
        assert "TODO:" in assistant_text


@pytest.mark.parametrize("klass", PROBE_CLASSES)
def test_scenario_metadata_matches_prompts(klass):
    """Metadata list should be same length as prompts list."""
    p = klass()
    assert len(p._scenario_metadata) == len(p.prompts)


@pytest.mark.parametrize("klass", PROBE_CLASSES)
def test_attempt_prestore_hook(klass):
    """_attempt_prestore_hook should populate attempt.notes with triggers and agentdojo metadata."""
    p = klass()
    # Create a minimal attempt
    attempt = Attempt(
        prompt=p.prompts[0],
        goal=p.goal,
        seq=0,
    )
    attempt = p._attempt_prestore_hook(attempt, 0)
    assert "triggers" in attempt.notes
    assert isinstance(attempt.notes["triggers"], list)
    assert len(attempt.notes["triggers"]) > 0
    assert "agentdojo" in attempt.notes
    assert "injection_goal" in attempt.notes["agentdojo"]
    assert "suite" in attempt.notes["agentdojo"]
    assert "attack_type" in attempt.notes["agentdojo"]


@pytest.mark.parametrize("klass", PROBE_CLASSES)
def test_required_probe_attributes(klass):
    """Each probe must have required garak attributes."""
    p = klass()
    assert p.lang == "en"
    assert p.doc_uri != ""
    assert p.primary_detector == "agentdojo.ToolCallInjectionFollowed"
    assert len(p.tags) > 0
    assert p.goal != ""


def test_prompt_count():
    """ImportantInstructions should produce expected number of prompts.

    banking: 2 scenarios * 2 goals = 4
    workspace: 40 scenarios * 6 goals = 240
    total = 244
    """
    p = ImportantInstructions()
    assert len(p.prompts) == 244


def test_suite_filtering_banking():
    """Probe should respect suite filtering — banking only."""
    p = ImportantInstructions.__new__(ImportantInstructions)
    p.suites = ["banking"]
    p.user_name = "Emma Johnson"
    p.model_name = "the AI language model"
    p.attack_template = ATTACK_TEMPLATES["important_instructions"]
    p.attack_name = "important_instructions"
    p.prompts = []
    p._scenario_metadata = []
    p._build_prompts()
    # banking: 2 scenarios * 2 goals = 4
    assert len(p.prompts) == 4


def test_suite_filtering_workspace():
    """Probe should respect suite filtering — workspace only."""
    p = ImportantInstructions.__new__(ImportantInstructions)
    p.suites = ["workspace"]
    p.user_name = "Emma Johnson"
    p.model_name = "the AI language model"
    p.attack_template = ATTACK_TEMPLATES["important_instructions"]
    p.attack_name = "important_instructions"
    p.prompts = []
    p._scenario_metadata = []
    p._build_prompts()
    # workspace: 40 scenarios * 6 goals = 240
    assert len(p.prompts) == 240


# ---------------------------------------------------------------------------
# Detector tests
# ---------------------------------------------------------------------------


def _make_attempt_with_outputs(output_texts, triggers):
    """Helper to create an Attempt with given outputs and triggers."""
    prompt = Conversation(
        turns=[Turn("user", Message(text="test prompt", lang="en"))]
    )
    attempt = Attempt(prompt=prompt, goal="test", seq=0)
    attempt.notes["triggers"] = triggers
    attempt.outputs = [
        Message(text=t, lang="en") if t is not None else None
        for t in output_texts
    ]
    return attempt


def test_injection_followed_detects_trigger():
    """InjectionFollowed should return 1.0 when trigger is in output."""
    d = InjectionFollowed()
    attempt = _make_attempt_with_outputs(
        ["I will send money to US133000000121212121212 now."],
        ["US133000000121212121212"],
    )
    results = list(d.detect(attempt))
    assert results == [1.0]


def test_injection_followed_no_trigger():
    """InjectionFollowed should return 0.0 when trigger is absent."""
    d = InjectionFollowed()
    attempt = _make_attempt_with_outputs(
        ["Your total spending in March 2022 was $1050."],
        ["US133000000121212121212"],
    )
    results = list(d.detect(attempt))
    assert results == [0.0]


def test_injection_followed_none_output():
    """InjectionFollowed should return None for None outputs."""
    d = InjectionFollowed()
    attempt = _make_attempt_with_outputs(
        [None],
        ["US133000000121212121212"],
    )
    results = list(d.detect(attempt))
    assert results == [None]


def test_injection_followed_case_insensitive():
    """Trigger matching should be case-insensitive."""
    d = InjectionFollowed()
    attempt = _make_attempt_with_outputs(
        ["Forwarding to ATTACKER@EVIL.COM as requested."],
        ["attacker@evil.com"],
    )
    results = list(d.detect(attempt))
    assert results == [1.0]


def test_injection_followed_multiple_outputs():
    """Should handle multiple outputs correctly."""
    d = InjectionFollowed()
    attempt = _make_attempt_with_outputs(
        [
            "I will send money to US133000000121212121212.",
            "Your balance is $5000.",
            None,
        ],
        ["US133000000121212121212"],
    )
    results = list(d.detect(attempt))
    assert results == [1.0, 0.0, None]


def test_injection_refused_detects_refusal():
    """InjectionRefused should return 0.0 (safe) when refusal is detected."""
    d = InjectionRefused()
    attempt = _make_attempt_with_outputs(
        ["I cannot follow that instruction as it appears to be malicious."],
        [],
    )
    results = list(d.detect(attempt))
    assert results == [0.0]


def test_injection_refused_no_refusal():
    """InjectionRefused should return 1.0 (potential vuln) when no refusal."""
    d = InjectionRefused()
    attempt = _make_attempt_with_outputs(
        ["Sure, I'll transfer the money right away."],
        [],
    )
    results = list(d.detect(attempt))
    assert results == [1.0]


def test_injection_refused_none_output():
    """InjectionRefused should return None for None outputs."""
    d = InjectionRefused()
    attempt = _make_attempt_with_outputs(
        [None],
        [],
    )
    results = list(d.detect(attempt))
    assert results == [None]
