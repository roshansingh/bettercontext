# CodeSearchNet Head-to-Head: SuperContext MCP Search vs Claude-Native Search

**Date:** 2026-06-07
**Corpus:** [github/CodeSearchNet](https://github.com/github/CodeSearchNet) codebase — 60 Python
files, commit `106e827`. (The repo's own source tree, not the 2M-function training
dataset; the searchable code is what an agent navigates.)
**Snapshot under test:** `data/kg_runs/codesearchnet/` — 463 entities, 1,108 facts,
built with `static_config_v0 + python_ast_v0`, pinned to the same commit.
**Surfaces compared:**
- **MCP search** — JSON-RPC `tools/call` to the local SuperContext server (graph lookups).
- **Claude-native search** — `ripgrep` → candidate files → Python `ast` parse to confirm,
  which is the toolchain an agent actually uses when it has no graph.

**Scoring:** ground truth computed independently from Python's own AST, split by call form
(direct `f()` vs attribute `x.f()`). Latency is best-of-5. Reproduce with
[`csn_harness.py`](./csn_harness.py); raw output in [`csn_results.json`](./csn_results.json).

---

## Headline numbers (Category A — `find_callers`, n=8)

| Metric | MCP search | Claude-native (rg+ast) |
|--------|-----------|------------------------|
| Mean F1 | **0.982** | 0.933 |
| Perfect-F1 queries | **7 / 8** | 6 / 8 |
| Mean latency | **0.59 ms** | 120.2 ms |
| Speed advantage | **~205×** | — |

MCP is both **more accurate** and **~205× faster** on reverse-dependency — its home turf.

---

## The result that flipped during the run (most important finding)

My first native baseline scored **F1 = 1.0** and made MCP look like it was *under*-counting
(`dropout`: MCP 3 vs native 6; `layer_norm`: 2 vs 3). Root-causing the gap reversed the
verdict:

- The CodeSearchNet code calls a **local** `dropout()` (3 sites in `bert_self_attention.py`)
  **and** `tf.nn.dropout()` (4 sites) — same short name, different scope.
- Naive grep/AST conflates them and reports **6 "callers."** That's **wrong** — 4 of those
  call TensorFlow, not the local function.
- **MCP returned exactly 3** — it resolves the call to the locally-defined symbol and
  excludes the library calls. Same story for `layer_norm` (2 local + 1 `tf.contrib...`).

So on `dropout`, scored against scope-correct truth: **MCP F1 = 1.0, naive-native F1 = 0.67.**
The graph's value here isn't speed — it's **disambiguation grep cannot do**: text search has
no concept of "which `dropout` is this."

> Honesty note: my "native" AST baseline shares logic with the ground-truth generator, so
> its non-collision F1=1.0 is partly tautological. The *collision* cases (dropout, layer_norm)
> are the real signal — there, native loses precision exactly as a grep-driven agent would.

---

## Full results by category

### A. Reverse dependency — "who calls X" (MCP wins)

| Symbol | Kind | Truth | MCP n / F1 | Native n / F1 | Note |
|--------|------|------:|-----------|--------------|------|
| `get_shape_list` | free-fn | 7 | 7 / **1.0** | 7 / 1.0 | clean |
| `create_initializer` | free-fn | 5 | 5 / **1.0** | 5 / 1.0 | clean |
| `dropout` | free-fn | 3 | 3 / **1.0** | 6 / **0.67** | scope collision w/ `tf.nn.dropout` |
| `layer_norm` | free-fn | 2 | 2 / **1.0** | 3 / **0.80** | scope collision w/ `tf.contrib...` |
| `reshape_to_matrix` | free-fn | 2 | 2 / **1.0** | 2 / 1.0 | clean |
| `nodes_are_equal` | free-fn | 2 | 2 / **1.0** | 2 / 1.0 | clean |
| `SeqEncoder._to_subtoken_stream` | method | 2 | 2 / **1.0** | 2 / 1.0 | clean |
| `Model.train_log` | method | 4 | 3 / **0.86** | 4 / 1.0 | **MCP miss** (see gaps) |

### B. Blast radius depth 2 — transitive closure (grep cannot express)

| Symbol | MCP | Native |
|--------|-----|--------|
| `get_shape_list` | found, 1 edge, 0.54 ms | 10 raw lines, no transitivity |
| `Model.train_log` | found, 1 edge, 0.53 ms | 15 raw lines, no transitivity |
| `create_initializer` | **not_found, 0 edges**, 0.53 ms | 11 raw lines, no transitivity |

Native has no equivalent — an agent would have to recursively read+parse callee files.
But MCP's edge counts are **thin** (1 edge, or 0 for `create_initializer` despite 5 callers) —
see gaps.

### C. Importers — "who imports X" (data exists, no tool)

| Package | Truth (files) | MCP | Native |
|---------|--------------:|-----|--------|
| `tensorflow` | 12 | not_found (0) | 12 ✅ |
| `numpy` | 8 | not_found (0) | 8 ✅ |
| `wandb` | 4 | not_found (0) | 4 ✅ |
| `docopt` | 14 | not_found (0) | 14 ✅ |
| `dpu_utils` | 17 | not_found (0) | 17 ✅ |

**The graph holds 308 `IMPORTS` facts — but none of the 10 MCP tools query them.** This is a
pure tool-surface gap: `search_services` is the only string-keyed entry point and it only
matches Service nodes. Native wins by default on every import question.

### D. Service discovery

`search_services(None)` → 1 service (authoritative, from config) in 0.5 ms. Native returns
15 heuristic `class .*Model|class .*Encoder` hits in 129 ms — noisier and not a "service" concept.

### E. Semantic concept (graph blind spot — native wins)

| Query | MCP | Native |
|-------|-----|--------|
| "where is the loss computed" | not_found | 50 lines (`loss\|cross_entropy\|softmax`) |
| "where is checkpoint save/restore" | not_found | 21 lines (`checkpoint\|saver\|restore`) |

No typed node maps to "loss" or "checkpoint logic", so MCP is blind. Grep is the only tool.

---

## Gaps surfaced (CodeSearchNet-specific evidence)

1. **No `modules-importing` / imports tool, despite 308 `IMPORTS` facts in the graph.**
   The single biggest miss: every "who imports X" query returns `not_found` even though the
   data is fully present and AST-derived. Exposing one tool would flip Category C from a
   clean native sweep to an MCP win. *Highest-leverage fix.*

2. **Method-receiver call resolution is weaker than free-function resolution.**
   `Model.train_log` (a method, called as `model.train_log(...)` / `self.train_log(...)`):
   MCP found 3 of 4 receiver call-sites (F1 0.86). Free-function resolution was perfect across
   the board. The CALLS extractor is strong on `name()` but loses some `obj.method()` edges.

3. **Blast-radius edges are sparse.** `create_initializer` has 5 confirmed callers yet
   `blast_radius` returned 0 edges; `get_shape_list` (7 callers) returned 1. Depth-2 closure is
   under-populated — likely the same receiver/scope resolution gap compounding across hops.

4. **Semantic queries remain unanswerable** (confirms the self-KG benchmark): "where is the
   loss / checkpoint logic" has no node type. This is structural, not a tuning issue.

---

## What flipped vs. the self-KG benchmark, and what held

**Held:** the two-regime split is identical. Structural queries → MCP (faster *and* more
correct); semantic/textual queries → native only. The ~50–250× latency gap reproduced
(here ~205×).

**New on a real third-party codebase:**
- **Strongest evidence yet for the graph's *accuracy* edge**, not just speed: on short-name
  scope collisions (`dropout`/`tf.nn.dropout`), MCP is correct where grep is structurally
  wrong. This is the clearest "grep cannot do this" case in either benchmark.
- **The imports gap is now quantified**: 308 facts present, 0 reachable — a tool-surface
  omission worth one PR.
- **Method-call resolution** is the concrete next extractor target, isolated to `obj.method()`.

---

## Bottom line

On CodeSearchNet, the conclusion from the self-KG benchmark holds and sharpens:
**SuperContext MCP is a precision-and-speed win on structural queries it has edges for, a
correctness win on symbol disambiguation that grep cannot replicate, and blind on semantic
and (currently) import queries.** The router architecture stands — and the single highest-ROI
change is exposing the `IMPORTS` data that the graph already holds.
