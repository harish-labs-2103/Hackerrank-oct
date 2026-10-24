# Usage report — final full-dataset run

- Run: full audit at 2026-10-24T17:29:44Z -> output.csv
- Requests processed: 518
- AI assistance: ON
- Provider / model: ollama / `gemma3:4b` (digest `68ceab7e468a31c988b`)
- Token counts are the provider's own counters (Ollama `prompt_eval_count` / `eval_count`), recorded per call; cached replays report the tokens of the original live call and add no new cost.

## Per model and task

| Provider | Model | Task | Calls | Live | Cached | Input tokens | Output tokens | Total tokens | Live latency (s) | Guard accepted | Guard rejected |
|---|---|---|---|---|---|---|---|---|---|---|---|
| ollama | gemma3:4b | classify_request | 15 | 0 | 15 | 6,197 | 383 | 6,580 | 0.0 | 15 | 0 |
| ollama | gemma3:4b | extract_artifact | 22 | 0 | 22 | 13,288 | 4,723 | 18,011 | 0.0 | 22 | 0 |
| **all** | | | **37** | 0 | 37 | **19,485** | **5,106** | **24,591** | 0.0 | | |

## Totals

- Total tokens: **24,591** (input 19,485, output 5,106)
- Average tokens per request: **47.5** over 518 requests
- Estimated cost: **$0.00 total, $0.00 per request** — the model runs locally through Ollama on CPU, so there is no per-token charge. Marginal cost is local compute only (wall-clock latency shown above).

## Guard outcomes

- accepted:classify_request: 15
- accepted:extract_artifact: 14
- rejected:extract_artifact: 46

## Live computation behind the cached replay

The final run that wrote `output.csv` replayed every response from the cache (0 new calls, so its latency column is 0).
The responses it used were produced by these live calls on this machine (CPU only, 8 cores, no GPU):

| Prompt version | Live calls | Total live latency (s) | Mean per call (s) |
|---|---|---|---|
| `classify_request/v1` | 23 (15 used by the final run; 8 from texts no longer sent after heuristic fixes) | 225 | 10 |
| `extract_artifact/v2` | 22 | 1464 | 67 |

A discarded earlier run used `extract_artifact/v1` (22 more live calls). Its results were not used (see evaluation report, failure #12).
Guard outcomes count per extracted row: an accepted call can contain rejected rows (bad or ungrounded ids).
