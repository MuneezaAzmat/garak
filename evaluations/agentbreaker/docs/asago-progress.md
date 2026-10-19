# Asago scenario / MiniKlarna progress

**Stopped by user request; work has returned to the original AgentBreaker comparison.**

This side experiment used the user's **25 pre-generated Asago scenarios**. Policy Mapper and Scenario Generator are not used. Five cells completed before cancellation; the sixth was interrupted. Results are preserved separately and excluded from the original comparison.

| Stage | Status |
|---|---|
| Copy and hash supplied scenarios | Complete; 25 YAMLs and 25 paired features unchanged |
| Applicability review | Complete; four core mechanisms directly testable, 13 partial, eight unsupported |
| Scenario-to-AgentBreaker configuration adapter | Implemented; native generation/refinement, first initial attack retained |
| Focused validation | 20 tests and 29 subtests passed |
| Actual OGX/MCP execution | Stopped; partial evidence retained |
| Final findings | Side experiment cancelled |

- [Live execution status](../runs/asago-pregenerated-klarna-pilot/execution-status.json)
- [First-cell log](../runs/asago-pregenerated-klarna-pilot/cells/AP-T2-01-28712e/safe.log)
- [Coverage report](../runs/asago-pregenerated-klarna-pilot/report.md)
- [Execution manifest and fixture adaptations](../configs/asago/pregenerated/execution-manifest.json)
- [All-scenario applicability review](../configs/asago/pregenerated/applicability.json)
- [Source hashes and original paths](../configs/asago/pregenerated/source-manifest.json)

The route is pre-generated scenario → our AgentBreaker configuration exporter → native analysis, attack generation, verification and refinement → the same real Qwen/OGX/MCP MiniKlarna. This is not native ToolChat replay. ToolChat artifacts do not gate these tests. Original files are unchanged; fixture adaptations and narrowed assertions are explicit. Applicability does not establish that every narrative consequence is reproduced, or that an untested scenario is safe.

Earlier setup is preserved under runs/asago-klarna-pilot. Its hand-authored-panel generation was stopped when the user supplied the existing corpus: no native artifact completed and no target test ran. The rejected heavy dependency installation was not retried.
