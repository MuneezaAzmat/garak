# OGX/MCP MiniKlarna Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Run the actual OGX/MCP MiniKlarna agent with local Qwen and evaluate it with both pinned AgentBreaker releases.

**Architecture:** OGX owns the model/tool loop and talks to the upstream FastMCP service. A runtime supervisor isolates each independent MCP ledger; the evaluation adapter reads Responses output and state snapshots into the existing trace schema. A protocol bridge, when required, delegates entirely to OGX.

**Tech Stack:** Python 3.13 in `.venv-ogx`, OGX Responses, MCP SSE, Ollama `qwen2.5:14b`, pinned MiniKlarna and garak sources, existing Python evaluator.

**Spec:** `docs/superpowers/specs/2026-09-20-ogx-miniklarna-design.md`

## Global constraints

- No changes to vendored sources, raw preliminary traces, cloud credentials or Ollama model settings.
- Loopback services; workspace-local persistence; new run directory and dependency lock.
- Preserve all seven upstream Klarna tools and record the difference from the preliminary adapter.
- Fresh MCP process state between independent requests; verified snapshots before and after each request.
- Real model calls provide acceptance evidence. No synthetic reply is reported as a live result.

## Task 1: Native runtime readiness

**Files:** `.venv-ogx/` (ignored), `configs/ogx-local.yaml`, `requirements-ogx.lock`, `docs/ogx-runtime.md`.

- [x] Create the separate environment and resolve a pinned OGX/MCP runtime, retaining package versions and upstream configuration compatibility findings.
- [x] Inspect the installed OGX configuration schema, CLI and Responses MCP implementation. Derive the local configuration from the pinned mini-agents configuration; record any required compatibility adjustments.
- [x] Start the upstream Klarna MCP entrypoint with `--host 127.0.0.1`, and OGX with local storage and local Qwen. Verify tool discovery and provider mapping without issuing an evaluation prompt.
- [x] Record readiness commands, actual ports, versions and any upstream streaming patch requirement. Do not blindly apply the repository's patch to a different OGX version.

## Task 2: Real-agent trace and reset integration

**Files:** `src/agentbreaker_eval/ogx_agent.py`, `tests/test_ogx_agent.py`, existing `runner.py`/`server.py` dispatch points, `configs/ogx-qwen.json`.

**Interface:** `run_ogx_agent(prompt, mode, config) -> dict` returns the existing trace schema; it calls OGX Responses and reads MCP snapshots, never the executor directly. The runtime supervisor owns only processes it started and waits for in-flight work before teardown.

- [x] Add non-socket parser tests: a saved Responses output containing a completed MCP call preserves exact tool arguments/result; a text-only JSON-shaped call remains text and is never dispatched; a failed/incomplete Responses result remains an error.
- [x] Run the tests before implementation and confirm the expected missing-interface failure.
- [x] Implement Responses/MCP output parsing and the local runtime/reset lifecycle using the installed runtime's verified interfaces. Preserve raw response documents alongside normalized traces.
- [x] Route baseline and existing garak bridge requests to `run_ogx_agent` when the target backend is `ogx`. Keep the direct-executor backend available for reproduction.
- [x] Verify one legitimate refund and one ineligible-refund request through the full path; inspect the actual MCP ledger. Verify state reset with a second independent request.

## Task 3: Bounded evaluation and findings

**Files:** `runs/ogx-qwen-pilot/` (ignored), `docs/ogx-runtime.md`, `docs/progress.md`, existing reporting artifacts.

- [x] Run the same five baseline prompts in safe and unsafe mode, recording the original seven-tool catalog and real OGX instructions/limits.
- [ ] Run bounded 0.16.0/0.17.0 native campaigns with equal configured budgets; retain timeouts, tool errors and actual completed-request counts.
- [ ] Rejudge common frozen responses where budget permits and distinguish missing judgments from negative verdicts.
- [ ] Review ledger outcomes and Asago candidate provenance. Compare releases within the OGX deployment; describe differences from the preliminary adapter without attributing causality to one component.
- [ ] Publish local report links, exact run status, validation scope and unresolved limitations. No claim of production-service safety.

## Current checkpoint

Design and plan preceded implementation. The preliminary direct-executor frozen comparison completed all 27 pairs for both releases. OGX 1.4/MCP 1.30 are installed in a separately locked environment, and the actual runtime is verified. All ten live baselines completed without runtime errors; 22 stored inference records corroborate their model/tool/result chains. The 105-test non-socket suite and 74 subtests passed. Native OGX campaigns are running; frozen rejudging remains outstanding.
