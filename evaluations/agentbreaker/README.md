# AgentBreaker evaluation on MiniKlarna

Compare the actual garak **0.16.0 and 0.17.0** AgentBreaker probe using an instrumented MiniKlarna target. The experiment separates what the agent attempted, what the backend blocked or deferred, what changed in the ledger, and what the AgentBreaker judge reported.

The earlier banking presentation's exact garak commit is unknown. Version 0.16.0 is a release comparator, not a reconstruction of that analysis. The five scenarios here are manually authored and Asago-aligned; they are not an internal dataset or native Asago-generated artifacts.

## Location and publication scope

This standalone evaluation project lives in `evaluations/agentbreaker/` in the
Garak fork. Change into that directory before running the commands below:

```bash
cd evaluations/agentbreaker
```

The branch includes the evaluation source, tests, synthetic configurations,
pinned dependency manifests, and methodology/findings documents. Historical
reports reference local `runs/` evidence that is not included. Findings documents
are historical snapshots and may contain results superseded by later local
analysis; publishing the source does not establish a final benchmark result. Raw campaign
records, credentials, downloaded upstream sources, unrelated research, and
generated presentation artifacts remain local.

The original hosted model endpoint has been replaced with
`https://llm.example.com/v1` in example configurations and proxy scripts. Replace
that placeholder with your own endpoint before using hosted-model examples;
local Ollama configurations do not require this change. Absolute paths in
historical documents and provenance manifests have been made project-relative.
The `qwen-3.8-27b` and `gemma-4-26b` values are historical provider-specific model
identifiers, not portable provider-independent model names.

## Plan and scope

The [implementation plan](docs/superpowers/plans/2026-09-20-agentbreaker-evaluation.md) was written before implementation. The [evaluation protocol](docs/evaluation-protocol.md) defines the questions, evidence and interpretation rules.

1. Validate ten direct backend controls: five scenarios against safe and unsafe executors.
2. Run the same five user requests through the configured Qwen agent in each mode.
3. Run unmodified AgentBreaker from each pinned garak release against both modes.
4. Give both release judges the same frozen prompt/response pairs.
5. Produce an evidence-linked scorecard, disagreements for review, and candidate Asago regression cases.

The active pilot uses the actual **OGX Responses → MiniKlarna MCP** stack with local Ollama `qwen2.5:14b` (`configs/ogx-qwen.json`). OGX owns the model/tool loop; each request gets a fresh upstream MCP process exposing all seven original tools. The first live checks confirmed an authorized $20 refund and an ineligible refund blocked by the safe backend. See [runtime setup and scope](docs/ogx-runtime.md).

The OGX pilot uses one repeat, temperature zero, six tool calls per request, eight accepted target requests per campaign and a 40-minute worker deadline. Upstream `max_attempts_per_tool=2` applies to each generated initial attack line, so it is not a cap of two total attacks. An in-flight bounded target request is drained before evidence consolidation.

The preliminary direct-executor run remains in `runs/qwen-pilot`, using `configs/ollama-qwen.json`, six tool turns and a 30-minute deadline. Its [completed frozen comparison](docs/qwen-frozen-findings.md) and [partially completed campaigns](docs/qwen-campaign-findings.md) are separate evidence. The earlier cloud Gemma configuration remains in `configs/pilot.json`.

## Setup

Run from this repository with Python 3.11–3.13 and `uv`:

```bash
python3 scripts/bootstrap.py
uv venv .venv --python 3.13
uv pip install --python .venv/bin/python -r requirements.lock
uv pip install --python .venv/bin/python --no-deps -e .
python3 scripts/bootstrap.py --verify
```

The bootstrap downloads pinned source archives and verifies archive and source-tree hashes. Only the runtime needed for garak's OpenAI-compatible path is installed; this is not an installation of every garak provider. The two garak releases run in separate processes with separate import/cache/config directories. The CLI loads the pinned MiniKlarna source directly.

Local Ollama needs no API key. For the optional cloud Gemma configuration, put your key in the ignored local `.env` file:

```dotenv
LITELLM_API_KEY=your-personal-key
```

Alternatively export `LITELLM_API_KEY` in the process environment. Existing environment values take precedence. The loader does not execute shell expressions. Do not put credentials in the JSON configuration. Runs, environment files and upstream downloads are ignored by Git.

## Execute

For the actual OGX/MCP agent, first follow the separate environment and service setup in [docs/ogx-runtime.md](docs/ogx-runtime.md), then run:

```bash
.venv-ogx/bin/python -m agentbreaker_eval doctor --config configs/ogx-qwen.json --run-dir runs/ogx-qwen-pilot
.venv-ogx/bin/python -m agentbreaker_eval run --config configs/ogx-qwen.json --run-dir runs/ogx-qwen-pilot
```

To reproduce the preliminary direct-executor setup:

```bash
.venv/bin/python -m agentbreaker_eval doctor --run-dir runs/qwen-pilot
.venv/bin/python -m agentbreaker_eval run --run-dir runs/qwen-pilot
```

`doctor` verifies source imports, `/v1/models`, the configured model ID, and one short completion, using authentication when configured. `run` stops if provider readiness or any experiment stage fails; saved evidence remains available. A previously completed control stage is reused. Choose a fresh run directory to repeat failed campaigns or change configuration. Frozen rejudge results are tied to an input fingerprint and cannot silently cover later-added traces.

Individual stages:

```bash
.venv/bin/python -m agentbreaker_eval doctor --offline --run-dir runs/local-check
.venv/bin/python -m agentbreaker_eval controls --run-dir runs/local-check
.venv/bin/python -m agentbreaker_eval baseline --run-dir runs/qwen-pilot
.venv/bin/python -m agentbreaker_eval campaign --run-dir runs/qwen-pilot
.venv/bin/python -m agentbreaker_eval rejudge --run-dir runs/qwen-pilot
.venv/bin/python -m agentbreaker_eval report --run-dir runs/qwen-pilot
```

Live stages require a successful full doctor check. `--config` selects settings; it defaults to `configs/ollama-qwen.json`. To revisit the original cloud run, supply both `--config configs/pilot.json` and `--run-dir runs/gemma-pilot`. Use `doctor --provider-only` for connection diagnosis; it does not authorize bypassing source checks. Ollama uses a dedicated optional `OLLAMA_API_KEY` variable, so the cloud credential is not sent to the local endpoint.

## Tangible artifacts

| File in run directory | Use |
|---|---|
| `report.md` | Observed outcomes, judge comparisons, Asago coverage and limitations |
| `metrics.json`, `scorecard.csv` | Counts with explicit denominators and separate unknowns/errors |
| `traces.jsonl` | Every tool call, response, before/after state and ledger-based verdict |
| `judgements.jsonl` | Separate campaign and frozen-response release judgments |
| `asago-candidates.json` | Policy/scenario/evidence-linked candidate regression cases |
| `manifest.json`, `doctor.json` | Sources, model settings, runtime, stage status and readiness |
| `campaigns/`, `rejudge/` | Native upstream attempts, configuration, worker logs and results |

Candidate exports require native Asago conversion and replay before integration. Controls-only candidates demonstrate fixture behavior; they are not AgentBreaker discoveries.

## Validation and limitations

```bash
.venv/bin/python -m pytest tests vendor/mini-agents/tests/test_klarna_refund_ledger.py -q
```

Tests use real upstream executors and real upstream probe/detector classes. Scripted model I/O checks harness behavior, not model performance. HTTP tests bind only to loopback.

The OGX adapter submits one Responses request and records actual MCP output plus independently read ledgers. It retains all seven original tools, including the model-visible full-state summary. Initialization instructions are saved as evidence; installed OGX 1.4's Responses path does not forward them as model system instructions. No additional target system prompt is supplied, matching the upstream example.

The preliminary adapter exercises real MiniKlarna executors through a custom model/tool loop with six customer tool schemas. It does not validate OGX/MCP transport, and its full-state summary is evaluator-only. Both deployments reset state between independent requests. Their differences mean the two deployments are not a controlled test of transport alone.

The oracle covers refund ownership, eligibility, remaining-balance caps and human review. Other actions remain outside its coverage. Refund records and applied balance reductions are distinct: the unsafe fixture can record a refund larger than the balance while reducing the balance only to zero. This is a simulated ledger, not evidence of an external payment.

The attacker gets an explicit refund contract and fixed fixture IDs in both versions. The target receives the upstream customer tool descriptions. Version 0.17's contract-aware judge receives the purpose/contract through its supported interface; 0.16 does not support that interface. This intended release difference is documented in each judgment.

Safe/unsafe modes remove several controls together and may reveal missing controls in tool responses. These are planted-control comparisons, not single-defense ablations. One repeat cannot establish statistical significance. The suite exercises four refund policies, so policy-level incremental coverage is bounded; new attack mechanisms also need qualitative review. Token totals cover recorded target calls only unless otherwise stated; native attacker/judge calls are not a complete cost ledger. No dollar prices are assumed.

Current execution status and evidence are recorded in [docs/progress.md](docs/progress.md).
