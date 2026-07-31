# Coverage Metrics: Measuring Extraction Quality

**A comprehensive guide to SuperContext coverage metrics—what they measure, how to generate them, and how to use them to improve your knowledge graph.**

**Last updated**: 2026-05-25

---

## Part 1: What is Coverage?

### Coverage ≠ Code Coverage

Many teams are familiar with code coverage (the percent of lines executed by tests). SuperContext coverage is different. It measures **extraction quality** — how much of your codebase SuperContext actually extracted and made queryable. It tells you which parts of your system are instrumented and which are silent.

### Definition: Extraction Quality Metrics

SuperContext coverage measures three dimensions:

**1. Entity Discovery Rate** — How many entities (functions, modules, services, endpoints) were found? Example: "87% of Python functions" means 87 out of 100 likely functions extracted.

**2. Relation Type Coverage** — How many relationships (imports, calls, endpoint hosting, events) were extracted? Example: "92% of Python imports" shows strong extraction vs "43% of Flask endpoints" shows weakness.

**3. Derivation Tier Distribution** — What fraction come from high-trust deterministic sources (AST parsing) versus lower-trust sources (LLM inference)? A graph with 95% deterministic facts is more actionable.

### Why Coverage Matters

**Identifies Gaps** — Shows where extractors work well and where they struggle. Flask at 40% endpoint coverage signals the extractor needs work.

**Guides Priorities** — Ranks improvements: "40% endpoint coverage is bigger than 87% function coverage."

**Tracks Progress** — Before/after metrics prove improvements worked.

**Prevents Overconfidence** — 1 million facts is impressive until you learn coverage is only 35% Python and 0% Kotlin.

### Example: A Real Codebase

Consider a Python/TypeScript microservice organization:

```
Repository: payment-service (Python)
Repository: web-ui (TypeScript/React)
Repository: admin-service (Python)
```

After building a KG snapshot, coverage might show:

| Entity Type | Found | Expected | Coverage % | Primary Source |
|-------------|-------|----------|------------|-----------------|
| CodeFunction (Python) | 342 | 392 | 87.2% | AST parser |
| CodeModule (Python) | 28 | 31 | 90.3% | AST parser |
| Endpoint (Python) | 18 | 45 | 40.0% | Flask decorator extraction |
| CodeFunction (TypeScript) | 156 | 412 | 37.9% | AST parser |
| Endpoint (TypeScript) | 24 | 68 | 35.3% | Express/Koa heuristic |
| ExternalPackage | 67 | 82 | 81.7% | Import graph analysis |

This snapshot tells a story: Python functions are well-extracted, but TypeScript lags. Flask endpoints are partially covered, and Express/Koa endpoints need work.

---

## Part 2: How Metrics Work

### The Coverage Model

SuperContext computes metrics from the `coverage.jsonl` file in every snapshot. Each row in this file describes what extraction state applies to a specific predicate, language, repository, or file:

```json
{
  "checked_at": "2026-05-14T02:09:42.102841+00:00",
  "coverage_id": "cov_72d886dd8ac6aaa16772e3a1",
  "predicate": "CALLS",
  "scope_ref": {
    "language": "python",
    "path_prefix": ".",
    "repo": "payment-service"
  },
  "source_system": "python_ast_v0",
  "state": "instrumented",
  "tenant_id": "default"
}
```

### Understanding Coverage Fields

**`predicate`** — Relation or entity type: `CALLS`, `IMPORTS`, `EXPOSES_ENDPOINT`, `CONSUMES_EVENT`, etc.

**`scope_ref`** — Scope: language (e.g., `"python"`, `"typescript"`), repo, or path. Lets you measure Python vs TypeScript separately.

**`source_system`** — How it was generated: `python_ast_v0`, `typescript_compiler_api_v0`, etc.

**`state`** — Status:
- `instrumented` — Extractor supported; facts extracted
- `partially_instrumented` — Supported with limitations (e.g., only Express/Koa for TypeScript endpoints, not Fastify)
- `uninstrumented` — Not supported. Reason in `scope_ref` (e.g., unsupported language, file too large, unresolved target)

### Coverage Record Example: Real Data

From an actual snapshot:

```json
{
  "checked_at": "2026-05-14T02:09:42.102844+00:00",
  "coverage_id": "cov_1fe762316ab76f2c100fb4e6",
  "predicate": "CALLS_ENDPOINT",
  "scope_ref": {
    "language": "javascript/typescript",
    "reason": "parser_backed_js_ts_client_endpoint_extraction_partial_fetch_axios_only",
    "repo": "mercury_api"
  },
  "source_system": "static_config_v0",
  "state": "partially_instrumented",
  "tenant_id": "default"
}
```

This row says: "TypeScript client-side endpoint calls are partially instrumented — we extract calls made with `fetch` and `axios`, but not other HTTP libraries."

### How "Expected" is Calculated

The `expected` count (e.g., "342 found / 392 expected = 87.2%") is computed heuristically:

- **Python functions** — Count all `def` statements (over-counts test fixtures but gives reasonable ceiling)
- **TypeScript functions** — Count `function` declarations and arrow assignments
- **Flask endpoints** — Count `@app.route()` decorators (misses programmatic registration)
- **Imports** — Count all import statements in AST
- **External packages** — Count unique package names

`expected` is not ground truth—it's an educated guess. Use it to understand relative gaps, not absolute completeness.

### Computing Metrics from Coverage Rows

The `coverage_metrics` command aggregates coverage rows into per-entity-type metrics:

```bash
python -m source.scripts.coverage_metrics --snapshot ./data/kg_runs/payment-service
```

This scans the snapshot's `coverage.jsonl`, groups by `predicate` and language/repo scope, and outputs `metrics.jsonl` with aggregated counts and percentages. The schema tracks both deterministic (AST-backed) and inferred (LLM-backed) facts separately, so you can measure the quality mix of your graph.

---

## Part 3: Using Coverage Reports

### Generating Metrics

Step 1: Build a KG snapshot from one or more repositories:

```bash
python -m source.scripts.build_kg --repo /path/to/payment-service --out data/kg_runs/payment-service
python -m source.scripts.build_kg --repo /path/to/web-ui --out data/kg_runs/payment-service
# or multi-repo in one go:
python -m source.scripts.build_multi_kg \
  --repo /path/to/payment-service \
  --repo /path/to/web-ui \
  --out data/kg_runs/payment-service
```

Output: snapshot directory with `entities.jsonl`, `facts.jsonl`, `evidence.jsonl`, and `coverage.jsonl`.

Step 2: Compute metrics:

```bash
python -m source.scripts.coverage_metrics \
  --snapshot data/kg_runs/payment-service \
  --expected-repos 2
```

Output: `data/kg_runs/payment-service/metrics.jsonl`

### Generating a Full Coverage Report

Step 3: Generate a human-readable markdown report plus structured JSON:

```bash
python -m source.scripts.coverage_report \
  --snapshot data/kg_runs/payment-service \
  --out docs/evaluation/runs/payment-service-2026-05-25 \
  --run-id payment-2026-05-25 \
  --tenant engineering \
  --expected-repos 2 \
  --metric-config source/kg/metrics/config.yaml
```

Output:
- `docs/evaluation/runs/payment-service-2026-05-25/coverage-run.md` — Formatted report for humans
- `docs/evaluation/runs/payment-service-2026-05-25/coverage-run.json` — Structured data for tools

### Reading the Markdown Report

The generated `coverage-run.md` has four sections:

**1. Summary (Table)** — Each metric scores 0.0–1.0 with status and explanation:

| Metric | Score | Status | Reason |
|--------|-------|--------|--------|
| M_inventory | 0.89 | PASS | Found 21 of 23 expected repos |
| M_extractor_opportunity | 0.72 | WARN | Only 72% of Python calls extracted |

**2. Entity Coverage** — Shows discovery by language and repo:

| Language | Repo | Found | Expected | Coverage % |
|----------|------|-------|----------|------------|
| python | payment-service | 156 | 178 | 87.6% |
| typescript | web-ui | 234 | 618 | 37.9% |

Notice TypeScript's lower coverage signals opportunity for improvement.

**3. Gaps Analysis** — Identifies biggest improvement opportunities:

- **EXPOSES_ENDPOINT** (TypeScript): 35% — only Express/Koa supported
- **CONSUMES_EVENT** (Python): 42% — heuristic-based discovery

**4. Recommendations** — Prioritized actions:

1. Improve TypeScript function extraction (currently 38%)
2. Add Next.js endpoint extraction (would reach ~50%)

### Interpreting JSON Output

The JSON report (`coverage-run.json`) contains machine-readable metrics:

```json
{
  "run_id": "payment-2026-05-25",
  "entity_coverage": [
    {
      "entity_type": "CodeFunction",
      "language": "python",
      "found": 156,
      "expected": 178,
      "coverage_percent": 87.6,
      "derivation_distribution": {
        "deterministic_static": 153,
        "inferred_llm": 3
      }
    }
  ]
}
```

Use JSON to:
- Integrate with dashboards or alerts
- Diff coverage between runs
- Filter by language/repo
- Track metrics over time

---

## Part 4: Contributing to Metrics

### Improving Coverage: A Case Study

Suppose coverage shows: "Flask endpoints: 40%."

**Step 1: Identify the Gap** — 18 endpoints found, but manual inspection reveals 45 total.

**Step 2: Inspect Missing Patterns** — Manually find what's not extracted:

```python
# Programmatic registration (currently missed):
app.add_url_rule('/admin/reports/', 'reports', views.reports)

# Class-based views (partially missed):
@app.post('/webhook')
class WebhookHandler(Resource):
    ...
```

**Step 3: Enhance the Extractor** — Upgrade Flask extractor from regex to AST:

```python
def extract_flask_endpoints(ast_tree, module_name):
    endpoints = []
    for node in ast.walk(ast_tree):
        if isinstance(node, ast.FunctionDef):
            # Handle decorators
            for decorator in node.decorator_list:
                if is_flask_route_decorator(decorator):
                    endpoints.append(parse_flask_decorator(decorator, node))
        elif isinstance(node, ast.Call):
            # Handle add_url_rule()
            if is_add_url_rule_call(node):
                endpoints.append(parse_add_url_rule(node))
    return endpoints
```

**Step 4: Re-run Coverage** — Rebuild snapshot and metrics with improved extractor.

**Step 5: Verify Improvement** — Old: 18/45 (40%), New: 35/45 (78%). Success.

### Adding New Coverage Checks

Define custom checks in the metric config:

```yaml
# source/kg/metrics/config.yaml
checks:
  - name: flask_endpoints
    predicate: EXPOSES_ENDPOINT
    language: python
    framework: flask
    priority: high
  
  - name: grpc_services
    predicate: EXPOSES_SERVICE
    language: python
    framework: grpc
    priority: medium
```

Run metrics with your config:

```bash
python -m source.scripts.coverage_metrics \
  --snapshot data/kg_runs/payment-service \
  --metric-config source/kg/metrics/config.yaml
```

Track domain-specific patterns (gRPC services, GraphQL resolvers, Celery tasks) alongside standard metrics.

### Refactoring with Confidence

With coverage metrics in place:

1. **Baseline** — Run coverage (e.g., "81% of functions extracted")
2. **Refactor** — Move code, split modules, rename functions
3. **Verify** — Re-run coverage. If it stays at 81%, refactoring preserved KG structure. If it drops to 60%, something broke.
4. **Investigate** — Use queries to understand what changed. Did you move Python to TypeScript? Add a new microservice?

---

## Cross-References

**Related guides:**
- [Knowledge Graph: Building and Using SuperContext's Core](./knowledge-graph.md) — How snapshots are built and what entities/facts they contain
- [Querying the Knowledge Graph](./querying.md) — How to ask questions of your snapshot using MCP tools
- [Evaluating Coverage](../03-workflows/evaluate-coverage.md) — Workflow for interpreting coverage reports and planning improvements (forthcoming)

**Reference documents:**
- [COVERAGE-METRICS.md](../../COVERAGE-METRICS.md) — Formal metric definitions and derivation classes
- [ADR-0006: Canonical Ontology and Fact Metadata Envelope](../../../adr/0006-canonical-ontology-and-fact-metadata-envelope.md) — Architecture behind coverage row schema

---

**Last updated**: 2026-05-25
