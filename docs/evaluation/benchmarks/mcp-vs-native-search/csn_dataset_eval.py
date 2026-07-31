"""CodeSearchNet *dataset* eval — NL-query code retrieval, scored by official NDCG.

Runs on the ACTUAL downloaded CodeSearchNet Python corpus (457,461 functions from
HuggingFace `code-search-net/code_search_net`), not the project's own source tree.
Task = the dataset's real task: given a natural-language query, rank relevant
functions. Scored with the repo's official NDCG (ported from
CodeSearchNet/src/relevanceeval.py) against the human relevance annotations.

Retrievers compared:
  1. bm25   — BM25 over docstring+code+name tokens via an inverted index. This is
              the corpus-wide generalization of Claude-native keyword search
              (what an agent does with grep, but ranked and exhaustive).
  2. tfidf  — TF-IDF cosine, published-baseline style (sanity anchor vs Husain 2019).
  3. mcp    — SuperContext MCP: route each NL query through search_services /
              find_callers. A structural code-graph tool doing NL retrieval — the
              result IS the finding, quantified rather than asserted.

Run:  /tmp/csn_venv/bin/python csn_dataset_eval.py
"""
from __future__ import annotations
import csv, json, math, re, time, collections, urllib.request
from pathlib import Path

CSN = Path("/Users/abcom/Desktop/github/CodeSearchNet")
CORPUS = CSN / "resources/data/py_corpus.jsonl"
ANN = CSN / "annotationStore.csv"
MCP = "http://127.0.0.1:3845/mcp"
TOPK = 100

TOK = re.compile(r"[a-z][a-z0-9_]+")
def toks(s): return [t for t in TOK.findall((s or "").lower()) if len(t) > 1]

# ----------------------------------------------------------------- official NDCG (ported)
def ndcg(predictions, relevance, ignore_unannotated=True):
    num, acc = 0, 0.0
    for q, rel in relevance.items():
        rank, dcg = 1, 0.0
        for url in predictions.get(q, []):
            if url in rel:
                dcg += (2 ** rel[url] - 1) / math.log2(rank + 1)
                rank += 1
            elif not ignore_unannotated:
                rank += 1
        idcg = sum((2 ** ideal - 1) / math.log2(i + 1)
                   for i, ideal in enumerate(sorted(rel.values(), reverse=True), 1))
        if idcg == 0:
            continue
        num += 1; acc += dcg / idcg
    return acc / num if num else 0.0

def coverage(predictions, relevance, positive_only=False):
    n_ann = n_cov = 0
    for q, rel in relevance.items():
        got = set(predictions.get(q, []))
        for url, r in rel.items():
            if positive_only and r <= 0: continue
            n_ann += 1; n_cov += (url in got)
    return n_cov / n_ann if n_ann else 0.0

# ----------------------------------------------------------------- load
def load_annotations():
    rows = [r for r in csv.DictReader(open(ANN)) if r["Language"] == "Python"]
    agg = collections.defaultdict(lambda: collections.defaultdict(list))
    for r in rows:
        agg[r["Query"].lower()][r["GitHubUrl"]].append(int(r["Relevance"]))
    return {q: {u: sum(v) / len(v) for u, v in d.items()} for q, d in agg.items()}

def load_queries():
    return [l.strip() for l in open(CSN / "resources/queries.csv")][1:]

# ----------------------------------------------------------------- inverted index
def build_index(verbose=True):
    """Single pass: urls[], doc_len[], df, postings {term: [(doc_id, tf), ...]}."""
    urls, dl = [], []
    df = collections.Counter()
    postings = collections.defaultdict(list)
    t0 = time.time()
    for i, line in enumerate(open(CORPUS)):
        d = json.loads(line)
        urls.append(d["url"])
        tf = collections.Counter(toks(d["doc"]) + toks(d["code"]) + toks(d["name"]))
        dl.append(sum(tf.values()) or 1)
        for t, f in tf.items():
            postings[t].append((i, f)); df[t] += 1
        if verbose and i % 100000 == 0 and i:
            print(f"  indexed {i:,} ...", flush=True)
    N = len(urls); avgdl = sum(dl) / N
    if verbose:
        print(f"  indexed {N:,} docs, {len(postings):,} terms in {time.time()-t0:.1f}s", flush=True)
    return dict(urls=urls, dl=dl, df=df, postings=postings, N=N, avgdl=avgdl)

# ----------------------------------------------------------------- BM25 (native keyword)
def bm25_rank(query, ix, k=TOPK, k1=1.5, b=0.75):
    N, avgdl, dl, df, post = ix["N"], ix["avgdl"], ix["dl"], ix["df"], ix["postings"]
    scores = collections.defaultdict(float)
    for t in set(toks(query)):
        if t not in post: continue
        idf = math.log(1 + (N - df[t] + 0.5) / (df[t] + 0.5))
        for doc_id, f in post[t]:
            scores[doc_id] += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * dl[doc_id] / avgdl))
    top = sorted(scores.items(), key=lambda x: -x[1])[:k]
    return [ix["urls"][d] for d, _ in top]

# ----------------------------------------------------------------- TF-IDF cosine
def tfidf_rank(query, ix, k=TOPK):
    N, df, post, dl = ix["N"], ix["df"], ix["postings"], ix["dl"]
    qts = collections.Counter(toks(query))
    qw = {t: qts[t] * math.log(N / (1 + df.get(t, 0))) for t in qts if t in post}
    qn = math.sqrt(sum(w * w for w in qw.values())) or 1.0
    scores = collections.defaultdict(float)
    for t, w in qw.items():
        idf = math.log(N / (1 + df[t]))
        for doc_id, f in post[t]:
            scores[doc_id] += w * (f * idf)
    # approximate doc norm by doc length (cheap, stable ranking proxy)
    top = sorted(scores.items(), key=lambda x: -x[1] / math.sqrt(dl[x[0]]))[:k]
    return [ix["urls"][d] for d, _ in top]

# ----------------------------------------------------------------- MCP
def mcp_call(name, args):
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": name, "arguments": args}}).encode()
    try:
        req = urllib.request.Request(MCP, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()).get("result", {}).get("structuredContent", {"status": "error"})
    except Exception as e:
        return {"status": "transport_error", "error": str(e)}

def mcp_rank(query):
    out = []
    for word in toks(query):
        sc = mcp_call("search_services", {"query": word})
        out += [s.get("urn", "") for s in (sc.get("services") or [])]
        sc2 = mcp_call("find_callers", {"symbol": word})
        out += [c.get("subject", "") for c in (sc2.get("callers") or [])]
    return [u for u in out if u]

# ----------------------------------------------------------------- main
def main():
    print("[load] annotations + queries", flush=True)
    rel = load_annotations()
    queries = load_queries()
    print(f"  {len(rel)} annotated queries, {len(queries)} query strings", flush=True)

    print("[index] building inverted index over 457K functions", flush=True)
    ix = build_index()

    # only score queries that have annotations (NDCG needs them)
    qset = [q for q in queries if q.lower() in rel]
    print(f"[run] {len(qset)} queries have annotations", flush=True)

    preds = {"bm25": {}, "tfidf": {}, "mcp": {}}
    timings = {"bm25": [], "tfidf": [], "mcp": []}
    for n, q in enumerate(qset, 1):
        ql = q.lower()
        for name, fn in (("bm25", lambda: bm25_rank(q, ix)),
                         ("tfidf", lambda: tfidf_rank(q, ix)),
                         ("mcp", lambda: mcp_rank(q))):
            t = time.perf_counter()
            preds[name][ql] = fn()
            timings[name].append((time.perf_counter() - t) * 1000)
        if n % 20 == 0:
            print(f"  {n}/{len(qset)} queries scored", flush=True)

    relevance = {q.lower(): rel[q.lower()] for q in qset}
    report = {"corpus_size": ix["N"], "queries_scored": len(qset), "approaches": {}}
    print("\n================ RESULTS (official NDCG) ================")
    for name in ("bm25", "tfidf", "mcp"):
        nd_within = ndcg(preds[name], relevance, ignore_unannotated=True)
        nd_all = ndcg(preds[name], relevance, ignore_unannotated=False)
        cov = coverage(preds[name], relevance)
        cov_pos = coverage(preds[name], relevance, positive_only=True)
        mean_ms = sum(timings[name]) / len(timings[name])
        report["approaches"][name] = dict(
            ndcg_within=round(nd_within, 4), ndcg_all=round(nd_all, 4),
            coverage=round(cov, 4), coverage_pos=round(cov_pos, 4),
            mean_ms=round(mean_ms, 1))
        label = {"bm25": "BM25 (Claude-native keyword)", "tfidf": "TF-IDF (baseline)",
                 "mcp": "SuperContext MCP"}[name]
        print(f"\n{label}")
        print(f"   NDCG (within annotated): {nd_within:.4f}")
        print(f"   NDCG (full ranking):     {nd_all:.4f}")
        print(f"   coverage of annotations: {cov*100:.1f}%   (positive-rel: {cov_pos*100:.1f}%)")
        print(f"   mean latency/query:      {mean_ms:.1f} ms")

    json.dump(report, open("/tmp/csn_dataset_results.json", "w"), indent=2)
    print("\nwrote /tmp/csn_dataset_results.json")

if __name__ == "__main__":
    main()
