"""CodeSearchNet head-to-head: SuperContext MCP search vs Claude-native search.

Corpus: github/CodeSearchNet codebase (60 Python files), commit 106e827.
Two surfaces, same questions:
  * MCP search  — JSON-RPC to the local SuperContext server (graph lookups)
  * Native search — ripgrep + Python `ast` over the live tree (what an agent
    actually does when it has no graph: grep to candidates, then parse to confirm)

Ground truth is computed independently from Python's own AST so we score
precision / recall, not just speed. Latency is best-of-5.
"""
from __future__ import annotations
import ast, json, subprocess, time, urllib.request, collections
from pathlib import Path

CSN = Path("/Users/abcom/Desktop/github/CodeSearchNet")
MCP = "http://127.0.0.1:3845/mcp"
PY_FILES = [p for p in CSN.rglob("*.py")
            if "/resources/" not in str(p) and "/.git/" not in str(p)]

# ---------------------------------------------------------------- transports
def mcp_call(name, args, n=5):
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": name, "arguments": args}}).encode()
    best = 1e9; sc = {}
    for _ in range(n):
        t = time.perf_counter()
        req = urllib.request.Request(MCP, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            body = json.loads(r.read())
        best = min(best, time.perf_counter() - t)
        if "result" in body:
            sc = body["result"]["structuredContent"]
        else:  # JSON-RPC error envelope
            sc = {"status": "error", "error": body.get("error", {}).get("message")}
    return sc, best * 1000

def native_callers(symbol, n=5):
    """What an agent does WITHOUT scope info: rg to candidate files, AST-parse,
    count any function whose body mentions `short(` — direct OR attribute. This
    is the *naive* native result, and deliberately conflates `dropout()` with
    `tf.nn.dropout()` exactly as a grep-driven agent would."""
    short = symbol.split(".")[-1]
    best = 1e9; callers = set()
    for _ in range(n):
        t = time.perf_counter()
        rg = subprocess.run(["rg", "-l", "--glob", "*.py", rf"\b{short}\b", str(CSN)],
                            capture_output=True, text=True)
        cand = [f for f in rg.stdout.strip().split("\n") if f]
        found = set()
        for fp in cand:
            try: tree = ast.parse(Path(fp).read_text())
            except Exception: continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for sub in ast.walk(node):
                        if isinstance(sub, ast.Call):
                            fn = sub.func
                            nm = (getattr(fn, "id", None) or getattr(fn, "attr", None))
                            if nm == short:
                                found.add(node.name)
        best = min(best, time.perf_counter() - t); callers = found
    return callers, best * 1000

def native_grep_count(pattern, n=5):
    best = 1e9; lines = 0
    for _ in range(n):
        t = time.perf_counter()
        rg = subprocess.run(["rg", "-n", "--no-heading", "--glob", "*.py", pattern, str(CSN)],
                            capture_output=True, text=True)
        lines = len([l for l in rg.stdout.strip().split("\n") if l])
        best = min(best, time.perf_counter() - t)
    return lines, best * 1000

# ---------------------------------------------------------------- ground truth (independent AST)
def gt_callers(symbol):
    """Ground truth split by call form:
      direct  = `short(...)`      -> calls to the LOCAL free function (true callers)
      attr    = `something.short(...)` -> method-receiver or library call (e.g. tf.nn.dropout)
    The graph's CALLS edge models local resolution, so `direct` is the fair target
    for a free function. We report both so the scope-collision effect is visible."""
    short = symbol.split(".")[-1]
    direct, attr = set(), set()
    for fp in PY_FILES:
        try: tree = ast.parse(fp.read_text())
        except Exception: continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Call):
                        fn = sub.func
                        if getattr(fn, "id", None) == short:
                            direct.add(node.name)
                        elif getattr(fn, "attr", None) == short:
                            attr.add(node.name)
    return direct, attr

def gt_importers(pkg):
    out = set()
    for fp in PY_FILES:
        try: tree = ast.parse(fp.read_text())
        except Exception: continue
        hit = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                hit |= any(a.name.split(".")[0] == pkg for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                hit |= (node.module or "").split(".")[0] == pkg
        if hit: out.add(fp.name)
    return out

def prf(found, truth):
    tp = len(found & truth); fp = len(found - truth); fn = len(truth - found)
    p = tp / (tp + fp) if (tp + fp) else (1.0 if not truth else 0.0)
    r = tp / (tp + fn) if (tp + fn) else 1.0
    f1 = 2 * p * r / (p + r) if (p + r) else 0.0
    return round(p, 3), round(r, 3), round(f1, 3)

# ---------------------------------------------------------------- dataset
CALL_TARGETS = ["get_shape_list", "create_initializer", "dropout",
                "Model.train_log", "layer_norm", "reshape_to_matrix",
                "nodes_are_equal", "SeqEncoder._to_subtoken_stream"]
IMPORT_TARGETS = ["tensorflow", "numpy", "wandb", "docopt", "dpu_utils"]

rows = []

# ----- A. reverse-dependency (who calls X) — graph's home turf
# Scored against DIRECT (local-resolution) ground truth. `attr` callers are
# reported separately to expose the short-name scope collision (tf.nn.dropout).
for sym in CALL_TARGETS:
    direct, attr = gt_callers(sym)
    sc, t_mcp = mcp_call("find_callers", {"symbol": sym, "limit": 100})
    mcp_callers = {c["subject"].split(".")[-1] for c in sc.get("callers", [])}
    nat, t_nat = native_callers(sym)
    nat_short = {c.split(".")[-1] for c in nat}     # naive: conflates direct + attr
    direct_short = {c.split(".")[-1] for c in direct}
    attr_short = {c.split(".")[-1] for c in attr}
    # method query (Class.method) -> receiver/attr calls are truth; free fn -> direct calls
    is_method = "." in sym
    truth = attr_short if is_method else direct_short
    p_m, r_m, f_m = prf(mcp_callers, truth)
    p_n, r_n, f_n = prf(nat_short, truth)
    rows.append(dict(cat="A find_callers", q=f"who calls {sym}",
                     kind="method" if is_method else "free-fn",
                     gt=len(truth), gt_direct=len(direct_short), gt_attr=len(attr_short),
                     mcp_status=sc.get("status"), mcp_conf=sc.get("target", {}).get("confidence"),
                     mcp_n=len(mcp_callers), mcp_f1=f_m, mcp_ms=round(t_mcp, 2),
                     nat_n=len(nat_short), nat_f1=f_n, nat_ms=round(t_nat, 1),
                     note=("scope collision: short name also appears as *." + sym.split('.')[-1]
                           + "()") if (attr and not is_method) else ""))

# ----- B. blast radius (transitive) — grep cannot express this
for sym in ["get_shape_list", "Model.train_log", "create_initializer"]:
    sc, t_mcp = mcp_call("blast_radius", {"symbol": sym, "depth": 2, "limit": 100})
    line_n, t_nat = native_grep_count(rf"\b{sym.split('.')[-1]}\b")
    rows.append(dict(cat="B blast_radius d=2", q=f"downstream of {sym}",
                     mcp_status=sc.get("status"), mcp_edges=len(sc.get("edges", [])),
                     mcp_ms=round(t_mcp, 2),
                     nat_n=f"{line_n} raw lines (no transitivity)", nat_ms=round(t_nat, 1)))

# ----- C. import / dependency
for pkg in IMPORT_TARGETS:
    truth = gt_importers(pkg)
    sc, t_mcp = mcp_call("search_services", {"query": pkg})   # MCP has no modules-importing tool
    line_n, t_nat = native_grep_count(rf"^\s*(import|from)\s+{pkg}")
    rows.append(dict(cat="C importers", q=f"who imports {pkg}", gt=len(truth),
                     mcp_status=sc.get("status"), mcp_n=sc.get("returned_count"),
                     mcp_ms=round(t_mcp, 2),
                     nat_files=len(truth), nat_ms=round(t_nat, 1),
                     note="no MCP modules-importing tool exposed; native AST is ground truth"))

# ----- D. service discovery
sc, t_mcp = mcp_call("search_services", {"query": None, "limit": 50})
line_n, t_nat = native_grep_count(r"class .*Model|class .*Encoder")
rows.append(dict(cat="D service_disc", q="what services exist",
                 mcp_status=sc.get("status"), mcp_n=sc.get("returned_count"), mcp_ms=round(t_mcp, 2),
                 nat_n=f"{line_n} class hits (heuristic)", nat_ms=round(t_nat, 1)))

# ----- E. semantic concept (graph blind spot)
for desc, pat in [("where is the loss computed", r"loss|cross_entropy|softmax"),
                  ("where is checkpoint save/restore", r"checkpoint|save_weights|restore|saver")]:
    sc, t_mcp = mcp_call("search_services", {"query": desc.split()[-1]})
    line_n, t_nat = native_grep_count(pat)
    rows.append(dict(cat="E semantic", q=desc, mcp_status=sc.get("status"),
                     mcp_n=sc.get("returned_count"), mcp_ms=round(t_mcp, 2),
                     nat_n=f"{line_n} lines", nat_ms=round(t_nat, 1),
                     note="no typed concept node; MCP blind, native wins"))

json.dump(rows, open("/tmp/csn_results.json", "w"), indent=2)

# ---------------------------------------------------------------- print
for r in rows:
    print("=" * 78)
    print(f"[{r['cat']}]  {r['q']}")
    for k, v in r.items():
        if k in ("cat", "q"): continue
        print(f"    {k:11} {v}")
print("=" * 78, "\nDONE")
