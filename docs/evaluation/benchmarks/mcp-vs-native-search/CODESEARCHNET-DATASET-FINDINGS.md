# CodeSearchNet *Dataset* Eval: NL-Query Retrieval (MCP vs Claude-native), Official NDCG

**Date:** 2026-06-07
**This is the real dataset run** — distinct from
[`CODESEARCHNET-FINDINGS.md`](./CODESEARCHNET-FINDINGS.md), which ran on the repo's 60
source files. Here we ran on the **actual downloaded CodeSearchNet Python corpus**.

- **Corpus:** 457,461 Python functions, downloaded from the HuggingFace mirror
  `code-search-net/code_search_net` (the original S3 bucket
  `s3://code-search-net/...` now returns HTTP 403 / deprecated). 555 MB of Parquet →
  `CodeSearchNet/resources/data/py_corpus.jsonl`.
- **Task:** the dataset's real task — given a natural-language query, rank the relevant
  functions.
- **Labels:** 2,079 human relevance annotations (0–3) across 99 queries from
  `annotationStore.csv`; 515 annotated functions join into the corpus, 98/99 queries usable.
- **Metric:** official **NDCG**, ported verbatim from
  `CodeSearchNet/src/relevanceeval.py` (no behavior change), plus annotation coverage.
- **Latency:** mean wall-clock per query.

Harness: [`csn_dataset_eval.py`](./csn_dataset_eval.py) · raw:
[`csn_dataset_results.json`](./csn_dataset_results.json)

---

## Results (99 queries, official NDCG)

| Retriever | NDCG (within) | NDCG (full) | Annotation coverage | Mean latency |
|-----------|--------------:|------------:|--------------------:|-------------:|
| **BM25 — Claude-native keyword** | **0.3444** | **0.1860** | **29.0 %** | 110.7 ms |
| TF-IDF — published-baseline style | 0.2487 | 0.1110 | 19.8 % | 100.2 ms |
| **SuperContext MCP** | **0.0000** | **0.0000** | **0.0 %** | 4.3 ms |

Context — published Python NDCG-All from Husain et al. 2019: ElasticSearch 0.256,
Neural-BoW 0.223, 1D-CNN 0.166, biRNN 0.064. Our BM25 (0.186) lands between 1D-CNN and
Neural-BoW — a sane, honest baseline, not a tuned number.

---

## The headline: MCP scores 0.0 — and that is the finding, not a bug

SuperContext MCP returns **nothing** for every natural-language query, so NDCG = 0 and
coverage = 0%. This is verified genuine, not a transport failure:

- The server is healthy and responds — `search_services("string")` returns
  `status: not_found` (a real answer, not a connection error).
- MCP has **no natural-language retrieval tool**. Its 10 tools are all symbol- or
  channel-keyed (`find_callers`, `blast_radius`, `search_services` over *Service* nodes…).
- Even if a tool matched, MCP is keyed on a **structural snapshot** (entity URNs /
  qualified symbols), not the corpus's GitHub URLs — so its output **cannot match the
  annotation URLs by construction**.

```
'convert int to string' -> MCP returned 0 items
'priority queue'         -> MCP returned 0 items
'read csv file'          -> MCP returned 0 items
```

So on the CodeSearchNet *dataset task*, MCP is not a weak retriever — it is a
**non-participant**. It is the wrong category of tool for "rank a function from an English
description." Its 4.3 ms latency is just the cost of returning empty.

---

## Why BM25/keyword (the Claude-native approach) wins here

The dataset task is **lexical/semantic retrieval over docstrings + code text**. That is
exactly grep-family territory generalized to ranking:

- **BM25 beats TF-IDF** (0.344 vs 0.249 within; 0.186 vs 0.111 full) — proper length
  normalization and saturation matter when docstrings vary wildly in length.
- Both are pure **text** methods. They win because the relevance signal *is* in the text
  (the queries were authored from docstrings), and neither needs a code graph.
- Coverage is low (29%) because 47% of annotated URLs are absent from today's corpus
  (repos moved/deleted since 2019) — a corpus-decay ceiling that caps everyone equally.

---

## What this proves, alongside the structural benchmarks

This completes the picture across all three runs in this folder:

| Benchmark | Task type | Winner | Why |
|-----------|-----------|--------|-----|
| `README.md` (self-KG) | structural ("who calls X") | **MCP** | ~50–250× faster, expresses graph queries grep can't |
| `CODESEARCHNET-FINDINGS.md` (repo source) | structural + disambiguation | **MCP** | F1 0.98 vs 0.93; resolves `dropout()` vs `tf.nn.dropout()` |
| **this** (real dataset) | NL retrieval | **Claude-native (BM25)** | MCP has no NL-retrieval tool → scores 0 |

**The two surfaces do not compete on the same axis.** SuperContext MCP wins decisively on
*structural code navigation* (its design goal) and scores literally zero on *natural-language
code search* (not its design goal). Claude-native keyword/grep is the inverse: strong on NL
retrieval, structurally blind (no transitive closure, no scope resolution).

This is the empirical backing for the router conclusion from the other reports: **route by
intent.** "Who calls / what breaks / what imports" → MCP. "Find me a function that does X"
→ native text retrieval (BM25 / embeddings). Neither is a replacement for the other; an
agent needs both.

---

## Reproduce

```bash
# 1. download the real dataset (HuggingFace mirror; S3 is deprecated)
cd CodeSearchNet/resources/data
for s in train test validation; do
  curl -L -o py_${s}-00000-of-00001.parquet \
    "https://huggingface.co/datasets/code-search-net/code_search_net/resolve/main/python/${s}-00000-of-00001.parquet"
done
# 2. flatten parquet -> py_corpus.jsonl (needs pyarrow; see csn_dataset_eval.py header)
# 3. run
python docs/evaluation/benchmarks/mcp-vs-native-search/csn_dataset_eval.py
```

> Note: the corpus (~700 MB JSONL + 555 MB Parquet) is **not committed** — it's a generated
> download. Only the harness and results JSON are tracked.
