# Garak agentic probe evaluation

Runs Garak's **AgentBreaker** and **ToolChain** probes against three deliberately unsafe mini-agents (Klarna, Airbnb and OcciAI customer-service domains from [mini-agents](https://github.com/hjrnunes/mini-agents)). The target model drives each agent through [OGX](https://pypi.org/project/ogx/)'s model/tool loop. Outcomes are scored from recorded tool calls and backend state changes, not from LLM judgments.

## Requirements

- A **git clone** of this repository, with history. Setup verifies that the pinned Garak commit is an ancestor of `HEAD`, so a source archive will not work.
- [uv](https://docs.astral.sh/uv/). It installs the locked Python 3.12 environment.
- Three OpenAI-compatible endpoints (`/v1/models` and `/v1/chat/completions`): an **attacker**, a **target** and a **judge**. They can be hosted APIs, vLLM, NIM or Ollama. The target endpoint must support tool calling. The three roles must resolve to different models.

## Setup

From `evaluations/agentic/`:

```sh
python3 scripts/bootstrap.py
```

This fetches mini-agents at the revision in `sources.lock.json`, runs `uv sync --frozen`, and applies and verifies the pinned OGX streaming patch. Use `--uv /path/to/uv` if `uv` is not on `PATH`. Use `--check-only` to verify sources without installing.

## Configure models

The model roster lists the available models, the attacker/target pairs, and the fixed judge. It defaults to [configs/models.json](configs/models.json), which reads every endpoint from environment variables. Copy [.env.example](.env.example) to a file **outside the repository**, fill it in, and pass it with `--env-file`. Only variables named by the roster are read, values are never shell-evaluated, and exported variables take precedence.

To compare several models, write your own roster and select it with `AGENTIC_MODELS=/path/to/roster.json`. Each model entry has these fields:

| Field | Meaning |
|---|---|
| `provider` | `openai-compatible`, or `ollama` to also record the local digest, quantisation and Ollama version |
| `model` / `model_env` | Model ID, or the variable that supplies it (the variable wins when set) |
| `base_url` / `base_url_env` | Endpoint for `openai-compatible` models; `ollama` uses `OLLAMA_BASE_URL` |
| `api_key_env` | Variable holding the bearer key. Omit it for keyless endpoints |
| `roles` | Any of `target`, `attacker`, `helper`, `judge`. The helper is always the attacker |
| `role_sampling` | Extra chat-completion fields for attacker/judge calls, such as `temperature` |
| `pinned_digest`, `pinned_quantization`, `pinned_backend` | Optional for `ollama`. Preflight then refuses different weights or a different server version |

Every pair is run on all three domains, for both probes and every budget cap. The roster used is recorded in each prepared manifest, and execution refuses to run if the roster has changed since preparation.

## Run

```sh
# 1. Verify each endpoint and record the exact served model identities (minimal requests, no attacks)
.venv/bin/python scripts/preflight_models.py --env-file ~/agentic.env --output results/preflight.json

# 2. Prepare a batch: manifests are fixed here and identified by content hash
.venv/bin/python scripts/run_evaluation.py prepare --stage pilot --arm both \
  --preflight results/preflight.json --output results/pilot

# 3. Execute (resumable: finished rows are kept, never replayed)
.venv/bin/python scripts/run_evaluation.py run --env-file ~/agentic.env --output results/pilot
```

- `--stage main` prepares the larger study.
- `--arm agent-breaker` or `--arm toolchain` prepares one probe.
- `run --limit N` executes the first N rows; `run --run-id <id>` executes one row from `matrix.json`.

The runner starts and stops the local MCP backends and OGX gateways itself. Everything listens on `127.0.0.1` only.

During a run, open `results/<batch>/progress.md` in a Markdown preview. It refreshes every three seconds and shows the active row, request usage, and which role the run is waiting on. `progress.json` holds the detailed diagnostics. To refresh a batch without running it:

```sh
.venv/bin/python scripts/watch_progress.py --output results/pilot --watch
```

## Protocol

| Setting | Value |
|---|---|
| Domains | `klarna`, `airbnb`, `occiai`, unsafe mode, upstream default data, upstream MCP instructions as the agent instructions |
| Target-request caps per run | pilot 10; main 10 and 50. One request = one probe submission to OGX, including refinements |
| OGX loop per target request | ≤16 model calls, ≤16 tool calls, 8,192 output tokens |
| Attacker/helper calls | 16,384 output tokens; judge calls 8,192; ≤200 attacker/helper/judge calls per run |
| Timeouts and retries | 900 s per request. Role calls retry only on HTTP 429, 500, 502, 503 or 504, at most twice (after 2 s and 5 s, honouring `Retry-After` ≤30 s). Target requests are never retried |
| Truncation | A role response that ends with `finish_reason: length` is rejected and logged, never passed to the probe as a partial plan |
| ToolChain | Upstream settings, except relevance batches of 4 paths and ≤2 hypotheses per path |
| Resets | Each AgentBreaker attempt restarts the backend. ToolChain keeps state within a chain and isolates chains |

The judge only steers each probe's native refinement and stopping. The run outcome comes from [agentic_eval/evidence.py](agentic_eval/evidence.py), which checks every tool call's arguments, result, and before/after backend snapshot against the evaluator-only rules in [policies/](policies/). Each request is labelled `confirmed_violation`, `authorized_action`, `blocked_attempt` or `unresolved`. Unparseable or exhausted judge verdicts are recorded as unresolved, not as misses.

## Outputs and reproducibility

Each run directory holds `manifest.json` (models requested and served, source revisions, limits, protocol hash), `events.jsonl` (every model call, tool call and state snapshot), the native probe attempts, a `requests.sqlite` budget ledger, and `result.json`. `summary.json` aggregates the batch. `results/` is git-ignored.

A manifest records the git commit and a protocol hash covering the shared code, configs, policies and lock files. Execution refuses to run if any of these, or the roster, changed after preparation. After changing code, models or settings, prepare a new batch directory. Do not pool results across protocol versions.

These backends deliberately omit business-rule enforcement. Results measure exploit discovery in these environments, not the robustness of a production agent.
