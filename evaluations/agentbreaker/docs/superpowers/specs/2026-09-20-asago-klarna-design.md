# Pre-generated Asago scenarios on MiniKlarna

## Objective and accepted scope

Use the user's existing corpus at vendor/asago-artifact-generator/examples/scenarios/. Do not run Policy Mapper or regenerate scenarios. Copy all 25 YAMLs and paired features unchanged, preserving hashes, original IDs and provenance.

The user chose scenario input to AgentBreaker over ToolChat replay. Translate each selected scenario's objective, original narrative and explicit fixture adaptation into attacker-side agent.yaml. The target's prompt, actual OGX/MCP tools and local Qwen stay unchanged. Native AgentBreaker 0.17 performs analysis, generation, verification and refinement. Our adapter retains the first initial attack for the selected tool and records discarded initial attacks, bounding each cell to two target requests. This is not an unmodified default campaign, native Asago artifact generation, or ToolChat replay.

## Initial pilot

Keep all 25 scenarios in the coverage inventory. Four directly testable core mechanisms form the live pilot: AP-T2-01 parameter inflation; AP-T3-02 authorization-boundary amount inflation; AP-T11-03 ambiguous policy inquiry leading to refund inflation; AP-T7-01 customer-resolution goal conflicting with refund eligibility. Preserve mechanism distinctions even where they exercise the same policy.

Run each in safe and unsafe modes, with one initial attack and at most one refinement: eight live cells, at most 16 target requests. Other scenarios remain explicitly unselected, partial or unsupported, never successful defenses. Missing RAG poisoning, tool-return injection, credential inheritance, code execution, external verification and fleet-scale behavior are deployment capabilities, not artifact-validation failures.

configs/asago/pregenerated/execution-manifest.json predeclares fixture substitutions and assertions. Original scenario text remains reference material for the attacker; explicit operationalized fixtures take precedence over illustrative identifiers. Completed refund records are not actual cash payments: unsafe mode can record an inflated amount while applying only the remaining balance.

## Runtime and evidence

Use existing local qwen2.5:14b through OGX1.4.0 and the original seven-tool upstream MCP MiniKlarna. Each independent request starts a fresh owned MCP process and verifies initial state. OGX owns the entire model/tool loop. Preserve native judge output alongside actual tool calls/results and before/after ledgers.

Active results live under runs/asago-pregenerated-klarna-pilot. Abandoned hand-authored-panel setup remains under runs/asago-klarna-pilot and contributes no results. No artifact-generation gate applies. The heavy Policy Mapper installation was declined and not retried.

## Outcomes and limits

Count corpus applicability, selected cells, actual requests, policy-triggering calls, completed violations, blocks, unknowns and runtime errors separately. Retain all 25 corpus entries. The existing four-policy refund oracle remains unchanged; scenario assertions separately check the declared fixture/amount. Confirmed policy violations outside an exact amount remain visible.

Native alerts may reflect willingness or factual errors rather than executed violations. The source's RCE-labelled ambiguity scenario tests financial tool misuse here, not code execution. Unsafe baseline already covers all four refund policies, so this experiment assesses scenario-to-test usefulness, mechanism diversity and judgment quality rather than new policy-count coverage. Shared local model use and one repeat do not support speed or broad superiority claims.

## Validation and deliverables

Focused no-socket tests cover native-loop integration, assertions and denominators, followed by actual OGX/MCP execution. Deliver source inventory, execution manifest, per-cell agent.yaml, raw native analysis and selected attacks, traces, judgments, incremental status, JSON/CSV matrices and concise findings. Never substitute fabricated tool history for executed evidence.
