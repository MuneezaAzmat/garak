# Ollama runtime observations and optional 8K follow-up

Preserve the active 32,768-context Qwen pilot so its conditions remain consistent. An 8,192-context alias is an **untried follow-up experiment**; no speed gain has been established. No alias was created and no Ollama setting or service was changed during this review.

## Measured conditions

- Ollama 0.32.6, `qwen2.5:14b`, Q4_K_M, approximately 9 GB weights and 15 GB loaded GPU allocation; host memory is 32 GiB.
- The first successful real baseline, safe `KLA-SCN-001`, took **78.492 seconds** for two model requests: 955 aggregate prompt tokens and 98 completion tokens. The individual requests used 427/41 and 528/57 prompt/completion tokens. Evidence: [Qwen traces](./runs/qwen-pilot/traces.jsonl).
- The corresponding Ollama server log (`~/.ollama/logs/server.log`) records 19.136 seconds prompt evaluation plus 19.558 seconds generation at 2.10 tokens/second, then 3.879 seconds prompt evaluation plus 35.487 seconds generation at 1.61 tokens/second. Later requests during this review generated about 1.89–1.93 tokens/second. These are individual observations, not a benchmark average.
- The same load reports a 6,144 MiB KV cache at 32,768 context. A smaller context should reduce that allocation; actual latency, residency and throughput must be measured separately.

## Supported experiment-specific context setting

Ollama explicitly documents that its OpenAI-compatible API does **not** provide a context-size request field. Its supported solution is to create a model from a Modelfile with `PARAMETER num_ctx`, then select that model name in requests. Adding `num_ctx` or `options.num_ctx` to this experiment's `/v1/chat/completions` request is not the documented solution. [Official OpenAI compatibility documentation](https://docs.ollama.com/api/openai-compatibility#setting-the-local-context-size).

For a separate future experiment, save the following as `configs/Modelfile.qwen-8k`:

```text
FROM qwen2.5:14b
PARAMETER num_ctx 8192
```

Then run:

```sh
ollama create qwen2.5-agentbreaker-8k -f ./configs/Modelfile.qwen-8k
ollama show qwen2.5-agentbreaker-8k --parameters
```

The installed CLI help confirms `create -f` and `show --parameters`; the parameter and inheritance syntax are documented in the [Modelfile reference](https://docs.ollama.com/modelfile). Select `qwen2.5-agentbreaker-8k` for target, attacker and judge in a new run configuration/directory. After its first normal request, verify the actual context with `ollama ps`. This avoids changing the base model or restarting the global service. [Ollama context verification](https://docs.ollama.com/context-length#check-allocated-context-length-and-model-offloading).

## Prompt size and runtime limits

The configured native campaign supplies one tool contract, so discovery is skipped. Measured fixed prompt sizes from the pinned templates and current contract are:

| Native prompt | 0.16.0 characters | 0.17.0 characters |
| --- | ---: | ---: |
| Initial tool analysis | 3,511 | 3,511 |
| Exploit template, before generated analysis/history | 1,618 | 1,618 |
| Verification, before attack/response text | 2,904 | 9,008 |

These are characters, **not Qwen token counts**. Native exploit generation includes up to five prior exchanges; the current two-attempt limit normally gives one prior exchange for the second attack in each independent attack line. Target conversations also accumulate their own tool results. An 8K window is plausible for this bounded pilot but is not proven sufficient for every generated prompt. Reserve output space: with a 1,536-token ceiling, nominal input headroom is 6,656 tokens before template overhead. Record actual prompt usage and any context-shift/truncation warnings before accepting an 8K comparison.

At the observed 1.6–2.1 tokens/second, generating the full 1,536-token ceiling alone would take **12.2–16.0 minutes**, before prompt evaluation. The ceiling is not a requested minimum. Native analysis requests several attack prompts plus structured analysis, while the 0.17 judge requests structured reasoning; a much smaller ceiling can truncate JSON and produce unavailable judgments. Keep current limits fixed. A later pilot could separately test 512–768 output tokens for target/judge while retaining room for attacker analysis, using identical per-role limits across releases and explicitly counting truncation failures. Even 512 generated tokens would take about 4.1–5.3 minutes at the current rate.

The [active configuration](./configs/ollama-qwen.json) retains direct-target `timeout=180`, `max_retries=1`, six tool turns, eight target requests and a **1,800-second outer campaign deadline**. The direct adapter passes 180 seconds to urllib's socket operations and allows at most two HTTP attempts; this is not a whole-agent deadline.

At **2026-09-20 05:49:13 UTC**, during the baseline and before the first native campaign or judge call, `provider.native_timeout=900` was added to allow longer native attacker/judge responses at the observed speed. This adaptation is recorded in the [run manifest](./runs/qwen-pilot/manifest.json). The worker uses a matching 900-second loopback fallback for the already-running parent process, whose in-memory configuration predates the new field. The direct-target timeout remains 180 seconds and the outer native campaign deadline remains 1,800 seconds. Model, context, output-token limits and evaluation budgets are unchanged by this timeout adaptation.

The configured native timeout is passed to **each SDK `chat.completions.create()` call**. Both pinned Garak generators load it as `self.timeout`, inspect the SDK method's signature, and forward matching attributes. A no-network verification executed their upstream configuration/forwarding methods against a scripted `create()` stub: both versions forwarded exactly 180 and 900 seconds when each was configured. Thus the SDK client's default 600-second read timeout is **not** the effective per-call timeout here.

Native retry behavior still differs from the direct adapter: the worker does not pass `max_retries` to the SDK. Installed OpenAI 2.54.0 defaults to two SDK retries, and both pinned Garak generators add backoff without a retry-count/deadline cap (`max_value=70` caps the delay only). The outer 1,800-second campaign deadline remains the overall native bound. At 1.6–2.1 tokens/second, a full 1,536-token output can approach or exceed the new 900-second per-call allowance once prompt evaluation is included; increasing the timeout does not establish that the entire campaign will finish within its deadline.
