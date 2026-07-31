"""SuperContext MCP vs native grep/ast search — head-to-head harness."""
from __future__ import annotations
import json, subprocess, time, urllib.request, collections, re

SNAP="data/kg_runs/self_kg"; MCP="http://127.0.0.1:3845/mcp"
ents={json.loads(l)['entity_id']: json.loads(l) for l in open(f"{SNAP}/entities.jsonl")}
facts=[json.loads(l) for l in open(f"{SNAP}/facts.jsonl")]
calls=[f for f in facts if f['predicate']=='CALLS']

def mcp(method,params,n=5):
    payload=json.dumps({"jsonrpc":"2.0","id":1,"method":method,"params":params}).encode()
    best=1e9;res=None
    for _ in range(n):
        t=time.perf_counter()
        req=urllib.request.Request(MCP,data=payload,headers={"Content-Type":"application/json"})
        with urllib.request.urlopen(req,timeout=10) as r: res=json.loads(r.read())
        best=min(best,time.perf_counter()-t)
    return res,best
def tool(name,args,n=5):
    res,dt=mcp("tools/call",{"name":name,"arguments":args},n)
    return res.get("result",{}).get("structuredContent",{}),dt
def sh(cmd,n=5):
    best=1e9;out=""
    for _ in range(n):
        t=time.perf_counter()
        p=subprocess.run(cmd,shell=True,capture_output=True,text=True)
        best=min(best,time.perf_counter()-t);out=p.stdout
    return out,best

# ground-truth callers of a qualname (unique subject qualnames)
def gt_callers(qualname):
    tgt_ids={eid for eid,e in ents.items() if e['kind']=='CodeSymbol' and e['identity'].get('qualname')==qualname}
    subs=set()
    for f in calls:
        if f['object_id'] in tgt_ids:
            s=ents.get(f['subject_id'])
            if s: subs.add(s['identity'].get('qualified_name') or s['identity'].get('qualname'))
    return subs

R=[]
def add(**k): R.append(k)

# ---- A: reverse-dep, unique symbol (_grep_for_query lives once) ----
sc,tm=tool("find_callers",{"symbol":"_grep_for_query","limit":100})
mcpc={c["subject"] for c in sc.get("callers",[])}
gt=gt_callers("_grep_for_query")
out,tg=sh("rg -n --no-heading '_grep_for_query' --glob '*.py' | wc -l")
add(cat="A reverse-dep (unique)",q="who calls _grep_for_query",
    mcp_status=sc.get("status"),mcp_callers=len(mcpc),gt=len(gt),
    correct=(mcpc==gt),mcp_ms=tm*1000,grep_lines=int(out),grep_ms=tg*1000,
    note="grep line-count includes def + dup lines; MCP returns unique callers")

# ---- A2: ambiguous '_read_jsonl' (4 defs, 13 call edges) ----
sc,tm=tool("find_callers",{"symbol":"_read_jsonl","limit":100})
add(cat="A2 reverse-dep (ambiguous name, no path)",q="who calls _read_jsonl",
    mcp_status=sc.get("status"),mcp_conf=sc.get("target",{}).get("confidence"),
    mcp_cands=sc.get("target",{}).get("candidate_count"),
    mcp_callers=sc.get("caller_count"),gt_total_edges=13,mcp_ms=tm*1000,
    note="4 defs share the name; MCP picks ONE def -> undercounts vs 13 total edges")

# ---- A3: same query, path-disambiguated ----
sc,tm=tool("find_callers",{"symbol":"_read_jsonl","path":"source/kg/eval/corpus.py","limit":100})
add(cat="A3 reverse-dep (path-disambiguated)",q="who calls corpus._read_jsonl",
    mcp_status=sc.get("status"),mcp_conf=sc.get("target",{}).get("confidence"),
    mcp_callers=sc.get("caller_count"),mcp_ms=tm*1000,
    note="path qualifier resolves the ambiguity cleanly -> MCP's edge over grep")

# ---- A4: extreme ambiguity 'main' (124 edges, ~40 defs) ----
sc,tm=tool("find_callers",{"symbol":"main"})
out,tg=sh("rg -n --no-heading '\\bmain\\(' --glob '*.py' | wc -l")
add(cat="A4 reverse-dep (extreme ambiguity)",q="who calls main()",
    mcp_status=sc.get("status"),mcp_conf=sc.get("target",{}).get("confidence"),
    mcp_cands=sc.get("target",{}).get("candidate_count"),
    mcp_ms=tm*1000,grep_lines=int(out),grep_ms=tg*1000,
    note="MCP flags ambiguity instead of guessing; grep dumps everything")

# ---- B: blast radius depth 2 ----
sc,tm=tool("blast_radius",{"symbol":"_grep_for_query","depth":2,"limit":100})
add(cat="B blast-radius d=2",q="downstream closure of _grep_for_query",
    mcp_status=sc.get("status"),mcp_edges=len(sc.get("edges",[])),mcp_ms=tm*1000,
    grep_lines="N/A",grep_ms=None,
    note="no single grep; agent must read+AST-walk recursively (many tool calls)")

# ---- C: service discovery ----
sc,tm=tool("search_services",{"query":None,"limit":50})
out,tg=sh("rg -l --glob '*.py' 'FastAPI|Flask|app = |create_app' | wc -l")
add(cat="C service-discovery",q="what services exist",
    mcp_status=sc.get("status"),mcp_services=sc.get("returned_count"),mcp_ms=tm*1000,
    grep_files=int(out),grep_ms=tg*1000,
    note="MCP authoritative from pyproject; grep guesses via framework strings")

# ---- D: event topology ----
sc,tm=tool("get_event_producers",{"channel":"orders-created"})
sc2,tm2=tool("get_event_consumers",{"channel":"orders-created"})
out,tg=sh("rg -n --no-heading 'orders-created' --glob '*.py' | wc -l")
add(cat="D event-topology",q="producers/consumers of orders-created",
    mcp_prod=sc.get("returned_count"),mcp_cons=sc2.get("returned_count"),
    mcp_status=f"{sc.get('status')}/{sc2.get('status')}",mcp_ms=(tm+tm2)*1000,
    grep_lines=int(out),grep_ms=tg*1000,
    note="grep finds string hits, cannot classify produce vs consume direction")

# ---- E: semantic / conceptual (graph weakness) ----
for desc,pat in [("where is auth/oauth handled","oauth|OAuth|authenticate|bearer token"),
                 ("where are retries/backoff","retry|backoff|max_retries|exponential")]:
    sc,tm=tool("search_services",{"query":desc.split()[-1]})
    out,tg=sh(f"rg -il --glob '*.py' '{pat}' | wc -l")
    add(cat="E semantic-concept",q=desc,
        mcp_status=sc.get("status"),mcp_hits=sc.get("returned_count"),mcp_ms=tm*1000,
        grep_files=int(out),grep_ms=tg*1000,
        note="no typed node for concept -> MCP not_found; grep is the only tool")

# ---- F: prose/comments (graph blind) ----
out,tg=sh("rg -n --no-heading 'TODO|FIXME|XXX|HACK' --glob '*.py' | wc -l")
add(cat="F text-content",q="find all TODO/FIXME",
    mcp_status="no tool indexes comments",mcp_ms=None,
    grep_lines=int(out),grep_ms=tg*1000,
    note="graph models structure not prose; grep only option")

json.dump(R,open("/tmp/sc_bench_out.json","w"),indent=2)
for r in R:
    print("="*72); print(f"[{r['cat']}]  {r['q']}")
    for k,v in r.items():
        if k in ("cat","q"):continue
        if k.endswith("_ms") and isinstance(v,float): print(f"   {k:14}{v:9.1f} ms")
        else: print(f"   {k:14}{v}")
print("="*72,"\nDONE")
