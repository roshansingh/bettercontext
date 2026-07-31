# SuperContext MCP vs. Native Search — Qualitative Head-to-Head

**Date:** 2026-05-31
**Snapshot under test:** `data/kg_runs/self_kg` (self-KG of this repo, commit-pinned to `10849f1`)
**Server:** local read-only MCP (`source.scripts.mcp_server`) on `http://127.0.0.1:3845/mcp`
**Method:** 10-query dataset across 6 categories, each run through **both** the SuperContext
MCP and native `ripgrep`/AST search. Timed best-of-5. Correctness scored against ground
truth computed directly from the JSONL snapshot.

Reproduce with [`harness.py`](./harness.py); raw results in [`results.json`](./results.json).

> **Companion run on a third-party codebase:**
> [`CODESEARCHNET-FINDINGS.md`](./CODESEARCHNET-FINDINGS.md) repeats this head-to-head on the
> [github/CodeSearchNet](https://github.com/github/CodeSearchNet) source tree with
> AST-derived ground truth and precision/recall scoring (harness:
> [`csn_harness.py`](./csn_harness.py), raw: [`csn_results.json`](./csn_results.json)). It
> confirms the two-regime split and adds the strongest evidence for the graph's *accuracy*
> edge — MCP correctly disambiguates a local `dropout()` from `tf.nn.dropout()` where grep
> structurally cannot — plus a quantified imports-tool gap (308 `IMPORTS` facts in the graph,
> 0 reachable via any MCP tool).
>
> **The actual CodeSearchNet *dataset* (not the repo source):**
> [`CODESEARCHNET-DATASET-FINDINGS.md`](./CODESEARCHNET-DATASET-FINDINGS.md) runs the
> dataset's real NL-query retrieval task on all **457,461 downloaded Python functions**,
> scored by the **official NDCG** (ported from the repo's `relevanceeval.py`) against 2,079
> human relevance labels. Result: BM25/keyword (Claude-native) NDCG 0.344, TF-IDF 0.249,
> **SuperContext MCP 0.000** — MCP has no natural-language retrieval tool, so on the dataset
> task it is a non-participant. Empirical proof that the two surfaces compete on different
> axes: MCP owns structural navigation, native owns NL retrieval. Harness:
> [`csn_dataset_eval.py`](./csn_dataset_eval.py), raw:
> [`csn_dataset_results.json`](./csn_dataset_results.json).
>
> **Agentic run — Claude+MCP vs Claude-alone on the dataset:**
> [`CODESEARCHNET-AGENT-FINDINGS.md`](./CODESEARCHNET-AGENT-FINDINGS.md) runs Claude as an
> *agent* (with tools) on 40 dataset queries × 2 arms (80 runs), scored by official NDCG.
> Result: **Claude+MCP 0.740 ≈ Claude-alone 0.707** — a statistical tie (median 0.750 vs
> 0.739; head-to-head 17–12–11), with the +4.7% mean gap almost entirely one
> formatting-failure query. Both agent arms ~2× the best pure retriever (BM25 0.344),
> confirming reasoning-over-candidates beats keyword ranking, and that MCP's structural tools
> are inert on NL retrieval. Harness: [`csn_agent_eval.py`](./csn_agent_eval.py), raw:
> [`csn_agent_results.json`](./csn_agent_results.json).

---

## Results

| # | Category | Query | MCP result | MCP time | Native result | Native time | Winner |
|---|----------|-------|-----------|---------|--------------|------------|--------|
| A | Reverse-dep (unique) | who calls `_grep_for_query` | 1 caller ✅ exact | **2.6 ms** | 2 raw lines (def+call, needs filtering) | 125 ms | **MCP** |
| A2 | Reverse-dep (shared name) | who calls `_read_jsonl` | 1 caller ✅ exact_unique | **2.3 ms** | — | — | **MCP** |
| A3 | Reverse-dep (path-qualified) | `corpus._read_jsonl` | **not_found** ❌ | 0.9 ms | — | — | native |
| A4 | Reverse-dep (extreme ambiguity) | who calls `main()` | **ambiguous, 30 candidates** ✅ | 2.5 ms | 140 undifferentiated lines | 123 ms | **MCP** |
| B | Blast radius depth 2 | downstream of `_grep_for_query` | 8 edges w/ evidence ✅ | **2.6 ms** | no single command | n×reads | **MCP** |
| C | Service discovery | what services exist | 1 (authoritative, pyproject) ✅ | **0.5 ms** | 8 files (framework-string guess) | 121 ms | **MCP** |
| D | Event topology | producers/consumers of `orders-created` | 0 prod / 2 cons | **1.4 ms** | 110 string hits, undirected | 128 ms | **MCP** (partial) |
| E | Semantic concept | where is auth/oauth | not_found ❌ | 0.4 ms | 0 files | 130 ms | tie (neither) |
| E | Semantic concept | where are retries/backoff | not_found ❌ | 0.5 ms | 1 file | 138 ms | **native** |
| F | Prose/comments | find TODO/FIXME | no tool ❌ | — | 1 hit | 131 ms | **native** |

> **Scoring corrections during the run:** two initial assumptions were wrong and MCP was
> actually *right* in both — `_read_jsonl` has only **1** definition (not 4), so
> `exact_unique` was correct; and the A `correct=False` was a `qualname`-vs-`qualified_name`
> mismatch in the scoring code, not an MCP error. Both fixed before tabulating.

---

## Dominant pattern: ~50× latency gap, but only on questions the graph models

**Speed.** Every MCP structured query answered in **0.4–2.6 ms** vs **121–138 ms** for
ripgrep — a **~50–250× latency advantage**. The reason is structural: the graph is
precomputed, so reverse-dependency is a hash-join over `facts.jsonl`, while grep rescans
271 Python files every time. The gap *widens* with repo size (grep is O(repo), MCP is
O(answer)).

**But that speed only exists for the questions the ontology has a node/edge type for.**
The benchmark splits cleanly into two regimes:

- **Structural queries (A, A2, A4, B, C, D)** — "who calls X", "blast radius", "what
  services", "event topology". MCP wins decisively: faster *and* more correct. Grep can't
  even express B (transitive closure) or distinguish D's produce-vs-consume direction.
- **Semantic/textual queries (E, F, A3-miss)** — "where is auth", "find retries", "TODO".
  MCP returns `not_found` because there's no `AuthFlow` node type and no tool indexes
  comments. Grep is the *only* option.

---

## The five real gaps (what "replacing Claude Code search" will hit)

**1. No semantic/concept layer (biggest gap).** The 8 typed tools answer "who calls this
*exact symbol*" but not "where is X *handled*". Real agent questions — "where's the rate
limiter", "how does auth work", "what handles retries" — map to no node type, so MCP
returns empty. An agent that trusts MCP as primary gets a false "nothing here" and stops.
This is the wedge where it cannot replace grep.

**2. The freshness tax.** The snapshot is **commit-pinned** (`10849f1`). A full rebuild
took **~12 s** for this 271-file repo. Until a rebuild runs, MCP answers about the *last
snapshot*, not the working tree — the instant an agent edits a file, every MCP answer is
potentially stale, while grep is always live. For a tool whose pitch is "tell the agent
what breaks *before* the diff," answering against pre-diff state is a structural tension.
Needs incremental / on-save reindex to be viable.

**3. Ambiguity resolution is half-built.** A4 (`main`, 30 defs) correctly returns
`status: ambiguous` instead of guessing — genuinely better than grep's 140-line dump.
**But** the documented escape hatch failed: A3,
`find_callers(_read_jsonl, path="source/kg/eval/corpus.py")`, returned `not_found` —
because `_read_jsonl` is only defined in `capture_snapshot_baseline.py`, not corpus.py.
Path-disambiguation works, but a *wrong* path silently yields empty rather than "no such
symbol there; did you mean…". An agent can't tell "no callers" from "I gave a bad path".

**4. Coverage is invisible at query time.** Only **1 service**, **4 endpoints**,
**4 TS/JS files** indexed vs 271 Python. Polyglot repos are mostly dark. The
`coverage_warnings` field exists in every response but came back empty even when the
answer was demonstrably partial (D: 2 consumers but 0 producers for `orders-created`, no
warning that producer coverage was thin). Silent partial answers are the dangerous failure
mode for an agent.

**5. Event direction modeling is incomplete.** `orders-created` has 2 `CONSUMES_EVENT`
edges but 0 `PRODUCES_EVENT` — yet 18 `REFERENCES_EVENT_CHANNEL` edges point at it. The
producer side is under-extracted, so "who publishes to orders-created" returns nothing even
though references clearly exist.

---

## Bottom line

SuperContext MCP is **not a grep replacement — it's a grep *complement* that wins a
different game.** Where the question maps to a typed relationship (call graph, blast radius,
service/event topology), it's ~50× faster, returns commit-pinned evidence, and expresses
queries grep structurally cannot. Where the question is semantic ("where is X handled") or
textual (comments/TODOs/config prose), it's blind and grep is mandatory.

**The correct architecture is a router, not a winner:**

- structural intent → MCP first
- conceptual / textual intent → grep first
- and crucially, **MCP `not_found`/`ambiguous` must fall through to grep**, never terminate
  the search.

The three things that would most move SuperContext toward "primary search":

1. a semantic concept layer or hybrid text index,
2. incremental reindex to kill the freshness tax,
3. honest coverage warnings so an agent knows when an empty answer means "absent" vs
   "uninstrumented".
