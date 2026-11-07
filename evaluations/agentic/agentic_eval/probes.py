"""Narrow adapters around the pinned native AgentBreaker and ToolChain probes.

Planning, attack generation, artifact handling, and refinement use upstream
methods. Evaluation-specific changes are dependency injection, external manifest
loading, dispatch limits, fixed step judging, and explicit unknown handling.
The driver is serial because Garak's report sink is process-global.
"""

from copy import deepcopy
from dataclasses import asdict
import io
import json
import math
from pathlib import Path
from threading import Lock

import yaml

from garak import _config
from garak.attempt import Message
from garak.detectors.agent_breaker import AgentBreakerChainResult
from garak.probes.agent_breaker import AgentBreaker, ToolChain
from garak.resources.agent_breaker.parsing import extract_json_object

from .budget import BudgetExhausted


_DRIVER_LOCK = Lock()


class JudgeUnresolved(RuntimeError):
    """The fixed judge could not provide a valid assessment."""


class _JudgeProxy:
    """Record exact judge inputs/outputs and validate single-tool verdicts."""

    def __init__(self, generator, recorder, kind):
        self.generator = generator
        self.name = generator.name
        self.recorder = recorder
        self.kind = kind

    def generate(self, prompt, generations_this_call=1):
        outputs = self.generator.generate(prompt=prompt, generations_this_call=1)
        self.recorder(
            {
                "event": "judge_response",
                "judgment_kind": self.kind,
                "prompt": asdict(prompt),
                "outputs": [
                    output.text if output is not None else None
                    for output in (outputs or [])
                ],
            }
        )
        if self.kind == "agent-breaker-exploit":
            valid = False
            if outputs and len(outputs) == 1 and outputs[0] is not None:
                try:
                    value = extract_json_object(outputs[0].text)
                    confidence = value.get("confidence")
                    valid = (
                        isinstance(value.get("success"), str)
                        and value["success"].upper() in {"YES", "NO", "PARTIAL"}
                        and type(confidence) in (int, float)
                        and math.isfinite(confidence)
                        and 0 <= confidence <= 1
                        and isinstance(value.get("reasoning"), str)
                    )
                except (ValueError, TypeError, AttributeError):
                    valid = False
            if not valid:
                return [Message(text="INVALID_JUDGE_OUTPUT")]
        return outputs


class _TargetDispatch:
    """Adapt a single target callback to the native generator interface."""

    parallel_capable = False
    name = "evaluation-ogx-target"

    def __init__(self, dispatch, cap):
        self.dispatch = dispatch
        self.cap = cap
        self.calls = 0
        self.notes = {}

    def generate(self, prompt, generations_this_call=1):
        if generations_this_call != 1:
            raise ValueError("Evaluation target requires generations=1")
        if self.calls >= self.cap:
            raise BudgetExhausted("Native component target-request cap exhausted")
        self.calls += 1
        try:
            output = self.dispatch(prompt.last_message().text, deepcopy(self.notes))
        except BudgetExhausted:
            self.calls -= 1
            raise
        if not isinstance(output, str):
            raise TypeError("Target dispatch must return response text")
        return [Message(text=output)]

    def clear_history(self):
        """Conversation/backend ownership remains with the runtime callback."""


class _InjectedProbe:
    """Inject models and tracing while retaining native probe methods."""

    def __init__(self, manifest_path, attacker, judge, recorder):
        self._manifest_path = Path(manifest_path)
        self._attacker = attacker
        self._judge = judge
        self._record = recorder
        self.completed = []
        self.unresolved = []
        self.generations = 1
        self.parallel_attempts = 1
        self.follow_prompt_cap = False
        # AgentBreaker constructs its detector directly; initialise with a local
        # no-op dependency, then inject Gemma before any assessment can run.
        detector_config = {
            "detectors": {
                "agent_breaker": {
                    "AgentBreakerResult": {
                        "detector_model_type": "test.Blank",
                        "detector_model_name": "Blank",
                    }
                }
            }
        }
        super().__init__(config_root=detector_config)
        self.red_team_model = attacker
        self.parse_model = attacker
        if hasattr(self, "_detector"):
            self._detector.evaluation_generator = _JudgeProxy(
                judge, recorder, "agent-breaker-exploit"
            )

    def _load_agent_config(self):
        self.agent_config = yaml.safe_load(self._manifest_path.read_text())
        if not isinstance(self.agent_config, dict):
            raise ValueError("Agent manifest must be a mapping")
        if not isinstance(self.agent_config.get("agent_purpose"), str):
            raise ValueError("Agent manifest must provide agent_purpose")
        if (
            not isinstance(self.agent_config.get("tools"), list)
            or not self.agent_config["tools"]
        ):
            raise ValueError("Primary comparison requires supplied manifest tools")
        self.agent_config_file = str(self._manifest_path)

    def _setup_red_team_model(self):
        self.red_team_model = self._attacker

    def _attempt_prestore_hook(self, attempt, seq):
        attempt = super()._attempt_prestore_hook(attempt, seq)
        # Native ChainResult explicitly matches this identifier. The adapter
        # implementation is recorded separately in the run metadata.
        name = "ToolChain" if isinstance(self, ToolChain) else "AgentBreaker"
        attempt.probe_classname = f"agent_breaker.{name}"
        return attempt

    def _setup_parse_model(self):
        if not getattr(self, "_judging_step", False):
            self.parse_model = self._attacker

    def _generator_precall_hook(self, generator, attempt=None):
        super()._generator_precall_hook(generator, attempt)
        generator.notes = deepcopy(attempt.notes or {})
        generator.notes["attempt_id"] = str(attempt.uuid)
        self._record(
            {
                "event": "native_attempt",
                "attempt_id": str(attempt.uuid),
                "notes": deepcopy(generator.notes),
                "prompt": asdict(attempt.prompt),
            }
        )

    def _postprocess_attempt(self, attempt):
        try:
            processed = super()._postprocess_attempt(attempt)
        except JudgeUnresolved:
            self.completed.append(deepcopy(attempt))
            raise
        self.completed.append(deepcopy(processed))
        return processed

    def _unknown(self, kind, details):
        event = {"event": "judge_unresolved", "judgment_kind": kind, "details": details}
        self.unresolved.append(event)
        self._record(event)
        raise JudgeUnresolved(details)


class EvaluationAgentBreaker(_InjectedProbe, AgentBreaker):
    """Native AgentBreaker with injected models and unknown-judge termination."""

    def _verify_attack_success(self, **kwargs):
        verdict = super()._verify_attack_success(**kwargs)
        self._record(
            {
                "event": "judge_verdict",
                "judgment_kind": "agent-breaker-exploit",
                "verdict": verdict,
            }
        )
        if verdict is None:
            self._unknown(
                "agent-breaker-exploit", "Native verification retries exhausted"
            )
        return verdict


class EvaluationToolChain(_InjectedProbe, ToolChain):
    """Native ToolChain with Gemma injected only during outcome assessments."""

    DEFAULT_PARAMS = ToolChain.DEFAULT_PARAMS | {"max_hypotheses_per_chain": 2}
    # Bound reasoning work per relevance call after the pilot's token cutoff.
    _RELEVANCE_BATCH_SIZE = 4

    def _check_step_success(self, chain, step_index, step_prompt, agent_response):
        previous = self.parse_model
        self._judging_step = True
        self.parse_model = _JudgeProxy(self._judge, self._record, "toolchain-step")
        try:
            verdict = super()._check_step_success(
                chain, step_index, step_prompt, agent_response
            )
        finally:
            self.parse_model = previous
            self._judging_step = False
        self._record(
            {
                "event": "judge_verdict",
                "judgment_kind": "toolchain-step",
                "chain_id": chain.get("chain_id"),
                "step_index": step_index,
                "verdict": verdict,
            }
        )
        if verdict[2] in {"step check unavailable", "invalid step-check response"}:
            self._unknown("toolchain-step", verdict[2])
        return verdict


class _InjectedChainDetector(AgentBreakerChainResult):
    """Native terminal scoring with injected Gemma and UNKNOWN retained."""

    def __init__(self, judge, recorder):
        self._provided_judge = judge
        self._record = recorder
        super().__init__(config_root={})

    def _load_generator(self):
        self.evaluation_generator = _JudgeProxy(
            self._provided_judge, self._record, "toolchain-exploit"
        )

    @classmethod
    def _score_judgement(cls, judgment):
        # Remove only a complete outer Markdown fence; keep native strict JSON,
        # duplicate-key checks, required fields and UNKNOWN semantics unchanged.
        if isinstance(judgment, str):
            lines = judgment.strip().splitlines()
            if (
                len(lines) >= 3
                and lines[0].lower() in ("```json", "```")
                and lines[-1] == "```"
            ):
                judgment = "\n".join(lines[1:-1]).strip()
        score = super()._score_judgement(judgment)
        if score is not None and json.loads(judgment)["verdict"] == "UNKNOWN":
            return None
        return score


def run_probe(
    component,
    manifest_path,
    attacker_generator,
    judge_generator,
    dispatch,
    cap,
    recorder=None,
):
    """Run a native probe against ``dispatch(prompt_text, attempt_notes)``.

    The caller pins provider identities, enforces the durable shared budget, and
    owns backend lifecycle. This additional cap prevents component overspending.
    Return actual native attempts, detector scores, and explicit execution state.
    Recorder receives dictionaries; it must not expose privileged evidence to
    model inputs. Call serially: Garak has process-global configuration/reporting.
    """
    if component not in {"agent-breaker", "toolchain"}:
        raise ValueError("Choose a single native probe component")
    if type(cap) is not int or cap <= 0:
        raise ValueError("cap must be a positive integer")
    recorder = recorder or (lambda event: None)
    with _DRIVER_LOCK:
        if not hasattr(_config.run, "generations"):
            _config.load_base_config()
        previous_report = getattr(_config.transient, "reportfile", None)
        report = io.StringIO()
        _config.transient.reportfile = report
        try:
            cls = (
                EvaluationAgentBreaker
                if component == "agent-breaker"
                else EvaluationToolChain
            )
            probe = cls(manifest_path, attacker_generator, judge_generator, recorder)
            target = _TargetDispatch(dispatch, cap)
            status = "completed"
            try:
                probe.probe(target)
            except BudgetExhausted:
                status = "budget_exhausted"
            except JudgeUnresolved:
                status = "judge_unresolved"
            attempts = probe.completed
            if not attempts and status == "completed":
                status = "no_candidates"
            detector = (
                probe._detector
                if component == "agent-breaker"
                else _InjectedChainDetector(judge_generator, recorder)
            )
            scores = []
            in_loop_unresolved = status == "judge_unresolved"
            for attempt in attempts:
                # An unresolved in-loop assessment is not retried by final scoring.
                unknown_in_loop = (
                    in_loop_unresolved
                    and attempt is attempts[-1]
                    and not attempt.notes.get("verified_results")
                )
                values = (
                    [None] * len(attempt.outputs)
                    if unknown_in_loop
                    else detector.detect(attempt)
                )
                scores.append({"attempt_id": str(attempt.uuid), "scores": values})
                if (
                    component == "toolchain"
                    and not unknown_in_loop
                    and detector._eligible_context(attempt) is not None
                    and any(value is None for value in values)
                ):
                    event = {
                        "event": "judge_unresolved",
                        "judgment_kind": "toolchain-exploit",
                        "attempt_id": str(attempt.uuid),
                        "details": "Native terminal verification retries exhausted",
                    }
                    probe.unresolved.append(event)
                    recorder(event)
                    status = "judge_unresolved"
            return {
                "attempts": attempts,
                "native_scores": scores,
                "status": status,
                "target_dispatches": target.calls,
                "unresolved_judgments": probe.unresolved,
                "metadata": {
                    "component": component,
                    "native_class": f"garak.probes.agent_breaker.{cls.__mro__[2].__name__}",
                    "adapter_class": f"{cls.__module__}.{cls.__name__}",
                    "fixed_step_judge": component == "toolchain",
                    "cap": cap,
                    "relevance_batch_size": (
                        probe._RELEVANCE_BATCH_SIZE
                        if component == "toolchain"
                        else None
                    ),
                    "native_settings": {
                        key: getattr(probe, key) for key in probe.DEFAULT_PARAMS
                    },
                },
            }
        finally:
            _config.transient.reportfile = previous_report
            report.close()
