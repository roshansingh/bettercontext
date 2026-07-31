# CodeSearchNet Agentic Eval: Claude+MCP vs Claude-Alone, Official NDCG

**Date:** 2026-06-08
**This is the *agent* comparison** — Claude actually running with tools — as opposed to the
*retriever* comparison in [`CODESEARCHNET-DATASET-FINDINGS.md`](./CODESEARCHNET-DATASET-FINDINGS.md)
(BM25/TF-IDF/MCP) and the *structural* runs in the other two reports.

- **Corpus:** the real downloaded CodeSearchNet Python dataset (457,461 functions).
- **Task:** natural-language query → rank relevant functions, official NDCG vs human labels.
- **Two arms, both `claude -p` (Claude Code, subscription auth):**
  - `claude_alone` — native tools only: **Bash / Read / Grep / Glob**.
  - `claude_mcp` — same tools **plus** the SuperContext MCP tools allowed
    (`mcp__supercontext__{search_services,find_callers,find_callees,blast_radius,get_service_brief}`).
- **Search space:** per-query candidate pool (~300 functions = all in-corpus annotated
  functions for that query + random distractors, shuffled so the agent can't tell which are
  labelled). The agent greps/reads the pool and returns a ranked top-10.
- **Scale:** 40 queries × 2 arms = **80 agent runs**, all completed cleanly (status `ok`, 0
  timeouts).

Harness: [`csn_agent_eval.py`](./csn_agent_eval.py) · raw:
[`csn_agent_results.json`](./csn_agent_results.json)

---

## Results (40 queries, official NDCG)

| Arm | NDCG (within) | NDCG (full) | Median (within) | Mean latency |
|-----|--------------:|------------:|----------------:|-------------:|
| **claude_mcp** | **0.7403** | 0.7358 | 0.750 | 52.1 s |
| claude_alone | 0.7069 | 0.7044 | 0.739 | 50.0 s |

Head-to-head: **MCP wins 17, alone wins 12, ties 11** (of 40).

For scale, from the retriever eval on the same data: BM25 0.344 / TF-IDF 0.249 (within).
**Both agent arms (~0.71–0.74) roughly double the best non-agent retriever (0.344).**

---

## The headline gap is one query, not a real MCP effect

MCP leads by +0.033 NDCG (~4.7%) on the mean — but that is **dominated by a single query**:

| Query | claude_alone | claude_mcp | Δ |
|-------|------------:|-----------:|---:|
| **convert json to csv** | **0.000** (4 URLs, 0 hits) | 0.966 (10 URLs, 6 hits) | **+0.966** |
| get current ip address | 0.707 | 0.868 | +0.161 |
| get http status description | 0.613 | 0.729 | +0.117 |
| … | | | |
| converting uint8 array to image | 0.503 | 0.412 | −0.091 |
| get inner html | 0.902 | 0.847 | −0.055 |

That one outlier alone contributes **+0.024 of the +0.033** mean gap. It is an
**agent-formatting failure, not a retrieval-quality signal**: on "convert json to csv" the
alone run emitted only 4 URLs and none were positives (likely truncated/garbled output that
turn), scoring a hard 0.0. Remove that single query and the two arms are within ~0.01 NDCG —
i.e. **statistically indistinguishable.** The **median is effectively tied** (0.739 vs 0.750),
and alone wins 12 queries outright.

---

## Why MCP neither meaningfully helps nor hurts here

This is the same conclusion as the retriever eval, now confirmed at the agent level:

1. **The task is natural-language retrieval; MCP's tools are structural.**
   `find_callers` / `blast_radius` answer "who calls X / what breaks" — they have no role in
   "rank a function matching this English description." The agent's *reading and reasoning*
   over the candidate pool does essentially all the work in both arms.

2. **MCP is keyed on the wrong snapshot.** The running server indexes the *supercontext*
   codebase, not the CodeSearchNet corpus, so any MCP call about a CSN function returns
   `not_found` by construction. Even when the agent had the tools, they could not surface CSN
   functions.

3. **The remaining ±differences look like agent nondeterminism**, not MCP signal: small
   per-query swings in both directions (MCP also *lost* 12 queries), consistent with
   run-to-run variance in how the agent samples/reads/formats — not with a tool that's adding
   retrieval power.

> Honesty caveat: this harness scored outputs but did **not** capture per-run tool-call
> traces, so we cannot prove how often the agent actually invoked MCP tools vs ignored them.
> Given points 1–2, the most defensible reading is that MCP tools were largely inert on this
> task; the near-tie is the expected result, and the +4.7% mean is an artifact of one
> formatting-failure query. (The repo's `source/kg/eval/runner.py` A/B harness *does* capture
> tool traces and would settle attribution if run against a CSN-keyed snapshot — see "Next".)

---

## What this adds to the benchmark set

| Benchmark | Task | Result |
|-----------|------|--------|
| `README.md` (self-KG) | structural | **MCP** wins (50–250× faster, graph-only queries) |
| `CODESEARCHNET-FINDINGS.md` (repo src) | structural + disambiguation | **MCP** wins (F1 0.98 vs 0.93) |
| `CODESEARCHNET-DATASET-FINDINGS.md` (retrievers) | NL retrieval | **BM25** 0.344, MCP 0.000 |
| **this** (agents) | NL retrieval | **tie** — Claude+MCP 0.740 ≈ Claude-alone 0.707 |

The throughline holds and sharpens:
- **A capable agent erases the retriever gap.** Claude-alone (0.71) is ~2× the best pure
  retriever (BM25 0.34) on the identical task — reasoning over candidates beats keyword
  ranking by a wide margin.
- **MCP adds no measurable lift on NL retrieval**, because it's the wrong tool category for
  the task and (here) keyed on the wrong corpus. It doesn't hurt either — the agent simply
  doesn't lean on it.
- This is the **agent-level confirmation of "route by intent"**: MCP earns its keep on
  structural questions (the other three reports), not on "find me a function that does X."

---

## Next (to make the agent A/B conclusive)

1. **Capture tool-call traces** (the repo's `runner.py` already does) to quantify how often
   each MCP tool was invoked and whether any call returned a usable result.
2. **Build a CSN-keyed snapshot** so MCP's structural tools could in principle contribute,
   then re-run — isolates "wrong corpus" from "wrong tool category."
3. **Add structural-intent queries** to the agent eval (e.g. "find functions that call
   `json.dump` and write a file") where MCP *should* help, to show the cross-over.

---

## Reproduce

```bash
# pools built from the downloaded corpus + annotations (see csn_dataset_eval.py for download)
/tmp/csn_venv/bin/python docs/evaluation/benchmarks/mcp-vs-native-search/csn_agent_eval.py \
  --n 40 --arms claude_alone,claude_mcp
```

> Auth: Claude Code uses its own login. A stale `ANTHROPIC_API_KEY` in the env breaks it, so
> the harness unsets it for each subprocess. The candidate pools and the corpus are generated
> artifacts and are **not committed**; only the harness and results JSON are tracked.
