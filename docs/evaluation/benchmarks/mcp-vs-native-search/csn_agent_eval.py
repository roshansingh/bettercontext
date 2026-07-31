"""CodeSearchNet agentic eval: Claude+MCP vs Claude-alone, official NDCG.

Unlike csn_dataset_eval.py (which compares *retrievers*), this compares two
*agents* actually running with tools, on the real CodeSearchNet dataset:

  * claude_alone — `claude -p` with native tools (Bash/Read/Grep) over a
                   per-query candidate pool file. The agent greps + reads + ranks.
  * claude_mcp   — same, plus the SuperContext MCP server registered and its
                   tools allowed. Tests whether the code-graph tools help (or are
                   inert) on a natural-language retrieval task.

Each query gets a ~300-function pool = all its in-corpus annotated functions +
random distractors, shuffled so the agent can't tell which are labelled. The
agent must return a ranked top-10 list of URLs. Scored with the official NDCG
(ported from CodeSearchNet/src/relevanceeval.py) against human relevance labels.

Auth note: Claude Code uses its own login; a stale ANTHROPIC_API_KEY in the env
breaks it, so we unset it for the subprocess.

Run: /tmp/csn_venv/bin/python csn_agent_eval.py --n 40 --arms claude_alone,claude_mcp
"""
from __future__ import annotations
import argparse, json, math, os, re, subprocess, time
from pathlib import Path

POOLS = Path("/tmp/csn_pools")
MAN = json.load(open(POOLS / "manifest.json"))
RELS = {q: v for q, v in MAN["relevances"].items()}
MCP_TOOLS = ("mcp__supercontext__search_services,mcp__supercontext__find_callers,"
             "mcp__supercontext__find_callees,mcp__supercontext__blast_radius,"
             "mcp__supercontext__get_service_brief")

PROMPT = """You are doing code search on the CodeSearchNet benchmark.

Query: "{query}"

The file {pool} contains candidate Python functions, one JSON object per line,
each with fields: url, name, doc, code. Find the functions MOST relevant to the
query and rank them best-first.

Use your tools to inspect the file (grep/read). {mcp_hint}

Output ONLY a JSON array of the top 10 url strings, best match first, e.g.
["https://...","https://..."]. No prose, no markdown fences, just the array."""

MCP_HINT_ON = ("You also have SuperContext MCP tools (mcp__supercontext__*) available "
               "if structural code-graph lookups help.")
MCP_HINT_OFF = ""

URL_RE = re.compile(r'https://github\.com/\S+?#L\d+-L\d+')

def run_agent(query, pool, arm, model=None, timeout=180):
    hint = MCP_HINT_ON if arm == "claude_mcp" else MCP_HINT_OFF
    prompt = PROMPT.format(query=query, pool=pool, mcp_hint=hint)
    allowed = "Bash Read Grep Glob"
    if arm == "claude_mcp":
        allowed = allowed + " " + MCP_TOOLS.replace(",", " ")
    cmd = ["claude", "-p", prompt, "--allowedTools", allowed]
    if model:
        cmd += ["--model", model]
    env = dict(os.environ); env.pop("ANTHROPIC_API_KEY", None)  # use Claude Code login
    t = time.perf_counter()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        dt = time.perf_counter() - t
        out = r.stdout.strip()
    except subprocess.TimeoutExpired:
        return [], (time.perf_counter() - t), "timeout"
    # parse: prefer a JSON array, else fall back to any github URLs in order
    urls = []
    m = re.search(r'\[.*\]', out, re.DOTALL)
    if m:
        try:
            urls = [u for u in json.loads(m.group(0)) if isinstance(u, str)]
        except Exception:
            urls = []
    if not urls:
        urls = URL_RE.findall(out)
    # dedup preserve order
    seen = set(); ranked = []
    for u in urls:
        if u not in seen:
            seen.add(u); ranked.append(u)
    return ranked[:10], dt, ("ok" if ranked else "no_urls")

# ---- official NDCG (ported) ----
def ndcg(pred, rel, ignore_unannotated=True):
    rank, dcg = 1, 0.0
    for url in pred:
        if url in rel:
            dcg += (2 ** rel[url] - 1) / math.log2(rank + 1)
            rank += 1
        elif not ignore_unannotated:
            rank += 1
    idcg = sum((2 ** ideal - 1) / math.log2(i + 1)
               for i, ideal in enumerate(sorted(rel.values(), reverse=True), 1))
    return (dcg / idcg) if idcg else None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--arms", default="claude_alone,claude_mcp")
    ap.add_argument("--model", default=None)
    ap.add_argument("--out", default="/tmp/csn_agent_results.json")
    ap.add_argument("--timeout", type=int, default=180)
    args = ap.parse_args()
    arms = args.arms.split(",")
    qs = MAN["queries"][:args.n]
    print(f"[agent eval] {len(qs)} queries x {len(arms)} arms = {len(qs)*len(arms)} runs", flush=True)

    per_arm = {a: {"ndcg_within": [], "ndcg_all": [], "ms": [], "status": [], "rows": []} for a in arms}
    for i, item in enumerate(qs):
        q = item["query"]; pool = item["pool_file"]; rel = RELS[q]
        for arm in arms:
            pred, dt, status = run_agent(q, pool, arm, model=args.model, timeout=args.timeout)
            ndw = ndcg(pred, rel, True)
            nda = ndcg(pred, rel, False)
            per_arm[arm]["ndcg_within"].append(ndw if ndw is not None else 0.0)
            per_arm[arm]["ndcg_all"].append(nda if nda is not None else 0.0)
            per_arm[arm]["ms"].append(dt * 1000)
            per_arm[arm]["status"].append(status)
            hits = sum(1 for u in pred if u in rel and rel[u] > 0)
            per_arm[arm]["rows"].append(dict(query=q, n_pred=len(pred), pos_hits=hits,
                                             ndcg_within=round(ndw, 4) if ndw is not None else None,
                                             status=status, ms=round(dt * 1000)))
        a0 = per_arm[arms[0]]["rows"][-1]
        print(f"  [{i+1}/{len(qs)}] {q[:42]:42}  " +
              "  ".join(f"{a}:{per_arm[a]['rows'][-1]['ndcg_within']}" for a in arms), flush=True)
        # checkpoint each query so a crash keeps partial data
        json.dump(_summary(per_arm, arms), open(args.out, "w"), indent=2)

    rep = _summary(per_arm, arms)
    print("\n================ AGENT EVAL RESULTS (official NDCG) ================")
    for a in arms:
        s = rep["approaches"][a]
        print(f"\n{a}")
        print(f"   mean NDCG (within): {s['mean_ndcg_within']:.4f}")
        print(f"   mean NDCG (full):   {s['mean_ndcg_all']:.4f}")
        print(f"   mean latency:       {s['mean_ms']:.0f} ms")
        print(f"   status counts:      {s['status_counts']}")
    json.dump(rep, open(args.out, "w"), indent=2)
    print(f"\nwrote {args.out}")

def _summary(per_arm, arms):
    import collections
    out = {"queries": None, "approaches": {}}
    for a in arms:
        d = per_arm[a]
        n = len(d["ndcg_within"]) or 1
        out["approaches"][a] = dict(
            n=len(d["ndcg_within"]),
            mean_ndcg_within=round(sum(d["ndcg_within"]) / n, 4),
            mean_ndcg_all=round(sum(d["ndcg_all"]) / n, 4),
            mean_ms=round(sum(d["ms"]) / n, 1),
            status_counts=dict(collections.Counter(d["status"])),
            rows=d["rows"])
    out["queries"] = len(per_arm[arms[0]]["ndcg_within"])
    return out

if __name__ == "__main__":
    main()
