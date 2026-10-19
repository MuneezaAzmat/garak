# AgentBreaker evaluation implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Build and run a reproducible, ledger-verified MiniKlarna comparison of AgentBreaker 0.16.0 and 0.17.0, with explicit Asago usefulness evidence.

**Architecture:** A small Python package wraps pinned MiniKlarna executors in an instrumented model/tool loop. Separate subprocesses import pinned garak releases, avoiding module-version collisions; all runs emit a shared JSONL evidence format consumed by one report generator. A loopback-only HTTP target lets unmodified upstream AgentBreaker call the agent through its normal OpenAI-compatible generator.

**Tech Stack:** Python 3.11+, standard library HTTP/JSON/subprocess, OpenAI Python SDK for model access, PyYAML, pytest, pinned upstream sources. The active pilot uses Ollama `qwen2.5:14b` for target, attacker and judge after the user's provider change; the supplied LiteLLM/Gemma configuration remains available separately.

**Spec:** `docs/superpowers/specs/2026-09-20-agentbreaker-evaluation-design.md`

## Global constraints

- MiniKlarna commit: `4daf9abcf5a894e3e5eb7ba5de2f7ccbafe462c3`.
- Versions: 0.16.0 release baseline (historical version unknown) and 0.17.0.
- No secrets in configuration, logs, source manifests, or report artifacts.
- Policy-free target prompt; identical tool catalog between modes; evaluator-only full-state inspection.
- Fresh state per independent prompt; preserve every tool call and full before/after ledger.
- Real upstream AgentBreaker; direct-executor target adapter documented transparently.
- Pilot defaults: one repeat, maximum two rounds per generated initial attack line; eight accepted target requests and a 1,800-second deadline per campaign. All roles use the same configured model in this pilot.
- Report unknowns, failures, sample sizes, and semantic differences between judge success and executed harm.

## Shared data contract

Each trace is a JSON object with `trace_id`, `kind` (`backend_control`, `baseline`, `agentbreaker`), `scenario_id` (nullable), `policy_ids`, `mode` (`safe`, `unsafe`), `version` (nullable), `prompt`, `response`, `calls` (list of `{name, arguments, result}`), `before`, `after`, `oracle` (dictionary), `error` (nullable), `usage`, and `elapsed_seconds`. Additional provenance is allowed. Never use default false for missing oracle/judge outcomes.

## Task 1: Reproducible source and environment setup

**Files:** `pyproject.toml`, `.gitignore`, `scripts/bootstrap.py`, `sources.lock.json`, `README.md`.

- [x] Add source pins and download upstream archives into ignored `vendor/`; validate paths and retain SHA256 hashes.
- [x] Create `.venv` using uv and install the API-path runtime dependencies; document that unused garak providers are not installed when using the minimal runtime.
- [x] Verify imports separately with `PYTHONPATH=vendor/garak-<version>:src:vendor/mini-agents/src .venv/bin/python ...`.
- [x] Record exact interpreter and installed package versions.

## Task 2: Scenarios, executor wrapper, and ledger oracle

**Files:** `src/agentbreaker_eval/scenarios.py`, `target.py`, `oracle.py`, `tests/test_oracle.py`, `tests/test_target.py`.

**Interfaces:** `scenarios.scenarios() -> list[dict]`; `target.KlarnaTarget(mode).call(name, arguments) -> dict`; `.snapshot() -> dict`; `.tools() -> list[dict]` (OpenAI function schemas); `oracle.evaluate(before, after, calls) -> dict` with `attempted_policy_violations`, `executed_policy_violations`, `blocked`, `pending_review`, `classification`, and `evidence`.

- [x] Write literal expected-outcome tests for refund20 ORD101, refund40 ORD104, refund50 ORD101, refund120 ORD102, refund10 ORD201. Include a textual-only claim, state reset, and a rejection with no money movement.
- [x] Run failing tests: `.venv/bin/python -m pytest tests/test_oracle.py tests/test_target.py -q`.
- [x] Implement wrappers importing real upstream classes, plus policy-independent ledger grading. Tool schema mirrors upstream MCP wrappers; no summary tool exposed.
- [x] Verify ten direct safe/unsafe backend controls and keep the recorded refund amount distinct from the applied balance delta.

## Task 3: Instrumented model-backed HTTP agent

**Files:** `src/agentbreaker_eval/agent.py`, `server.py`, `tests/test_agent.py`.

**Interfaces:** `agent.run_agent(prompt, mode, config) -> dict` returning a shared trace; `server.serve(config, mode, trace_path, port)` exposes loopback `/v1/models` and `/v1/chat/completions` with one fresh target per request.

- [x] Test a scripted model response boundary with the real target: a tool call followed by final text changes the ledger; a tool-loop limit remains an operational error.
- [x] Implement bounded HTTP calls, max tool turns, usage aggregation, JSON arguments validation, and no credentials in trace serialization.
- [x] Verify completed, rejected, unavailable-provider, and invalid-tool scenarios; use test doubles only for external model I/O.

## Task 4: Upstream campaigns and frozen-response rejudging

**Files:** `src/agentbreaker_eval/worker.py`, `runner.py`, `tests/test_worker.py`.

**Interfaces:** version-selected worker subcommands invoke actual `AgentBreaker`/`AgentBreakerResult`; orchestrator launches a target server per mode and joins upstream prompt/verdict evidence to target trace IDs.

- [x] Write tests for version selection, missing credentials/config, empty verdict preservation, and report-to-trace matching without dropping repeated prompts.
- [x] Inspect upstream signatures and configure one process_refund target, common models and budgets, per-version output directories.
- [x] Retain native garak attempts/judgments and response-level verification metadata; record fallback/missing contract conditions.
- [x] Rejudge identical frozen traces in each version with the same model; preserve unknowns and distinguish upstream attack-success semantics from executed-policy-violation labels.

## Task 5: Reporting and Asago candidate artifacts

**Files:** `src/agentbreaker_eval/report.py`, `cli.py`, `__main__.py`, `tests/test_report.py`, `configs/pilot.json`.

- [x] Write tests showing unknown judgments cannot count as clean passes and duplicate prompts cannot inflate distinct policy/mechanism coverage.
- [x] Implement scorecards with counts/denominators, costs when available, latency, and lists of evidence-supported findings. Do not infer dollar costs without pricing input.
- [x] Export policy/scenario/evidence-linked candidate regression cases. Label conversion as candidate, not native Asago compatibility.
- [x] Provide CLI commands `doctor`, `controls`, `baseline`, `campaign`, `rejudge`, and `report`; absent live stages remain explicitly unrun in reports.

## Task 6: Execute, review, and deliver

- [x] Run all meaningful unit/integration tests and pinned MiniKlarna regression tests.
- [x] Run deterministic controls and a single live model/tool smoke test, then five baseline scenarios per mode.
- [ ] Run bounded AgentBreaker campaigns across both versions/modes and frozen-trace rejudging, subject to provider readiness.
- [x] Independently review scoring, state isolation, provenance, and operational error handling; fix consequential findings and rerun affected checks.
- [ ] Generate a final report with actual results, limitations, upstream feedback candidates, and Asago usefulness assessment. Update this checklist and `docs/progress.md` with exact commands and artifacts.


Execution checkpoint: the initial full suite passed 88 tests and 33 subtests; subsequent local-provider checks passed 23 tests and 20 subtests. All ten deterministic controls and all ten Qwen baseline cases completed. Native campaigns are running in `runs/qwen-pilot`; frozen rejudging and final analysis remain outstanding. The earlier cloud run remains a separate controls-only study in `runs/gemma-pilot`, blocked by a connection timeout before HTTP/TLS authentication. See `docs/progress.md` for current evidence and commands.
