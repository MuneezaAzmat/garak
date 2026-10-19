# OGX/MCP MiniKlarna evaluation

The user wants the actual mini-agent runtime, rather than a custom model/tool loop. The main target will therefore use OGX Responses orchestration, local Ollama `qwen2.5:14b`, and the pinned MiniKlarna FastMCP server. The earlier direct-executor run remains preliminary evidence, not an equivalent deployment.

## Runtime and boundaries

- Preserve MiniKlarna commit `4daf9abcf5a894e3e5eb7ba5de2f7ccbafe462c3` and both pinned garak releases. Do not edit vendored sources.
- Use a separate `.venv-ogx` environment and a separate run directory. Resolve, verify and record OGX and MCP versions before live calls. OGX 1.4.0 is the current candidate from PyPI; verify compatibility with the repository's configuration before treating it as the selected runtime.
- Use the upstream OGX Responses provider and actual FastMCP entrypoint. On this Mac, native Python is available and Docker is not installed. Run only the Klarna services needed for this experiment, bound to loopback, with workspace-local persistence.
- Keep Ollama at `http://127.0.0.1:11434/v1`; use a nonsecret local placeholder when an SDK requires an API key. Do not forward the cloud credential.
- Retain the upstream seven-tool Klarna catalog, including `get_klarna_state_summary`. This differs from the preliminary adapter's six-tool catalog. Record this difference; do not pool the two deployments' rates or claim that any difference is caused solely by MCP.
- Keep business rules in the upstream executor. Inspect and record OGX's handling of MCP instructions instead of assuming they become a system prompt.
- MCP state lives for the server process. Start fresh MCP state for each independent request and verify the initial ledger. Preserve it throughout a request and any deliberately defined conversation. A new Responses ID alone does not reset the ledger.
- Collect actual Responses tool-call items and MCP ledger snapshots. Preserve both requested/recorded amounts and applied balance changes. Use the existing refund oracle, with out-of-scope actions and runtime failures explicit.
- A compatibility bridge, if needed by garak's generator, may translate request/response protocols but must delegate all model/tool orchestration to OGX. It must not call the MiniKlarna executor directly.
- This is a real agent deployment over simulated business services. It does not reproduce production customer identity, payment rails, human-review operations or customer traffic.

## Acceptance evidence

1. OGX discovers local Qwen and the actual Klarna MCP catalog.
2. A legitimate refund through `/v1/responses` produces an MCP call and the expected ledger change.
3. An ineligible refund through the same path is rejected in safe mode and executes in unsafe mode, with explicit ledger evidence. If the model does not issue that call, retain the outcome and use a separate direct MCP control to verify the backend; never invent execution.
4. A second independent request starts with the original ledger, while a deliberate conversation can retain state.
5. The baseline and bounded native AgentBreaker runs target this complete path, with separate deployment provenance, actual exposure and partial outcomes reported.
6. Final conclusions compare releases within the same deployment and distinguish model/tool formatting, orchestration failures, backend policy enforcement and judge mistakes.
