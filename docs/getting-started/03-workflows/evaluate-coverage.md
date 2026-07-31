# Evaluate Coverage: Measure and Improve Your Knowledge Graph

**Understand extraction quality and identify gaps to improve your knowledge graph.**

**Time estimate**: 20 minutes | **Difficulty**: Intermediate

**Prerequisites**: Complete [Setup and Build Your First KG](./setup-and-first-kg.md) first

**Last updated**: 2026-05-25

---

## Overview

You have a knowledge graph snapshot of your codebase. But how complete is it? SuperContext coverage metrics tell you:

- How much of your codebase was actually extracted
- Which entity types are well-covered and which are missing
- Where gaps exist and what to improve next

This guide walks you through seven steps to measure, understand, and act on coverage gaps.

---

## Step 1: Why Coverage Matters

Coverage metrics answer one critical question: **How much of my system did SuperContext actually extract?**

### Coverage ≠ Code Coverage

Code coverage tells you "87% of lines were executed by tests." SuperContext coverage tells you "87% of Python functions were extracted and made queryable." It's fundamentally different — it measures **extraction quality**, not test quality.

### Three Key Insights

**1. Identifies Gaps** — Coverage reveals which parts of your system SuperContext understands well and which are invisible. Example: "Flask endpoints only 40% coverage" signals the Flask decorator extractor needs work.

**2. Guides Priorities** — When multiple improvements are possible, coverage metrics show you which will have the biggest impact. Low TypeScript function coverage (38%) affects more queries than low gRPC service coverage (25%).

**3. Tracks Progress** — Before/after metrics prove that improvements worked. "Flask endpoints: 40% → 78%" documents real progress.

---

## Step 2: Run Coverage Metrics

After building a knowledge graph snapshot, compute metrics with a single command:

```bash
python -m source.scripts.coverage_metrics --snapshot ./data/kg_runs/flask-first
```

This command scans your snapshot's `coverage.jsonl` file and produces `metrics.jsonl` containing aggregated statistics.

### What Happens

The command:
1. **Reads** the snapshot's `coverage.jsonl` file
2. **Aggregates** coverage rows by entity type, language, and framework
3. **Computes** percentages (found / expected)
4. **Writes** results to `metrics.jsonl`

**Time taken**: 30-60 seconds for typical snapshots

### Expected Output

```
[INFO] Reading snapshot: data/kg_runs/flask-first
[INFO] Found 5 coverage records
[INFO] Aggregating by entity type...
[INFO] Computed metrics for:
  - CodeFunction (Python): 87.2%
  - CodeModule (Python): 90.3%
  - EXPOSES_ENDPOINT (Flask): 40.0%
  - IMPORTS (Python): 95.1%
[INFO] Written metrics to: data/kg_runs/flask-first/metrics.jsonl
```

---

## Step 3: Generate a Coverage Report

Raw metrics are useful, but a formatted report is easier to read and share. Generate one with:

```bash
python -m source.scripts.coverage_report \
  --snapshot ./data/kg_runs/flask-first \
  --out docs/evaluation/runs/flask-first-coverage \
  --run-id flask-2026-05-25 \
  --tenant default \
  --expected-repos 1 \
  --metric-config source/kg/metrics/config.yaml
```

### Key Flags Explained

| Flag | Example | Purpose |
|------|---------|---------|
| `--snapshot` | `./data/kg_runs/flask-first` | Path to your KG snapshot |
| `--out` | `docs/evaluation/runs/flask-first-coverage` | Where to write the report |
| `--run-id` | `flask-2026-05-25` | Unique ID for this run (used in filenames) |
| `--tenant` | `default` | Org/team name (metadata only) |
| `--expected-repos` | `1` | How many repos you expected (for validation) |
| `--metric-config` | `source/kg/metrics/config.yaml` | Config defining what to measure |

### What Gets Created

Two files appear in `docs/evaluation/runs/flask-first-coverage/`:

1. **`coverage-run.md`** — Formatted, human-readable report (Markdown)
2. **`coverage-run.json`** — Structured data (JSON) for tools and dashboards

**Time taken**: 10-30 seconds

---

## Step 4: Read the Markdown Report

Open `docs/evaluation/runs/flask-first-coverage/coverage-run.md` in any text editor or browser.

### Report Structure

The report has four main sections:

#### Section 1: Metrics Summary Table

```
| Metric | Score | Status | Reason |
|--------|-------|--------|--------|
| M_inventory | 0.89 | PASS | Found 1 of 1 expected repos |
| M_extractor_opportunity | 0.72 | WARN | Only 72% of Python functions extracted |
| M_framework_coverage | 0.40 | FAIL | Flask endpoints only 40% instrumented |
```

**How to read it:**
- **Score**: 0.0–1.0 (higher is better)
- **Status**: `PASS` (>0.8), `WARN` (0.5–0.8), `FAIL` (<0.5)
- **Reason**: Why the score is what it is

#### Section 2: Entity Coverage by Language and Repository

```
| Language | Repo | Entity Type | Found | Expected | Coverage % |
|----------|------|-------------|-------|----------|------------|
| python | flask | CodeFunction | 1,243 | 1,425 | 87.2% |
| python | flask | CodeModule | 340 | 376 | 90.4% |
| python | flask | EXPOSES_ENDPOINT | 18 | 45 | 40.0% |
```

**Key patterns to notice:**
- Functions are usually well-covered (85–95%)
- Endpoints by framework vary widely (40–95%)
- Imports are almost always complete (95%+)

**Interpretation example**: "Python functions at 87% — good extraction. Flask endpoints at 40% — needs work."

#### Section 3: Gaps Analysis (Low Coverage Predicates)

```
Predicates with lowest coverage:

1. EXPOSES_ENDPOINT (Flask) — 40% coverage
   - Found: 18 endpoints
   - Expected: 45 endpoints
   - Missing pattern: Programmatic route registration via app.add_url_rule()

2. EXTERNAL_PACKAGE — 81% coverage
   - Found: 67 packages
   - Expected: 82 packages
   - Missing: Packages in optional_dependencies groups
```

**How to use this**: These are the biggest wins for improvement. Focus on the top 2–3 gaps.

#### Section 4: Recommendations (Prioritized Actions)

```
Recommended improvements (prioritized by impact):

1. **Enhance Flask endpoint extraction** (impact: HIGH)
   - Current: 40%, Target: 80%
   - Effort: 4–6 hours
   - Approach: Extend AST visitor to handle app.add_url_rule() calls

2. **Improve external package discovery** (impact: MEDIUM)
   - Current: 81%, Target: 95%
   - Effort: 2–3 hours
   - Approach: Add pyproject.toml optional_dependencies parsing
```

**How to use this**: Pick the first recommendation to improve next.

---

## Step 5: Examine JSON for Details

For deeper analysis, open `coverage-run.json` in an editor or parse it with `jq`:

```bash
cat docs/evaluation/runs/flask-first-coverage/coverage-run.json | jq .
```

### JSON Structure

```json
{
  "run_id": "flask-2026-05-25",
  "tenant": "default",
  "snapshot_path": "data/kg_runs/flask-first",
  "entity_coverage": [
    {
      "entity_type": "CodeFunction",
      "language": "python",
      "found": 1243,
      "expected": 1425,
      "coverage_percent": 87.2,
      "derivation_distribution": {
        "deterministic_static": 1203,
        "inferred_llm": 40
      }
    }
  ],
  "gaps": [
    {
      "predicate": "EXPOSES_ENDPOINT",
      "framework": "flask",
      "coverage_percent": 40.0,
      "recommendation": "Enhance AST visitor for app.add_url_rule()"
    }
  ]
}
```

### Useful jq Queries

**Filter by language:**
```bash
cat coverage-run.json | jq '.entity_coverage[] | select(.language == "python")'
```

**Find lowest coverage predicates:**
```bash
cat coverage-run.json | jq '.gaps | sort_by(.coverage_percent) | .[0:3]'
```

**Count entities by type:**
```bash
cat coverage-run.json | jq '[.entity_coverage[] | {type: .entity_type, found: .found}]'
```

**Check derivation tier distribution** (how much is deterministic vs LLM-inferred):
```bash
cat coverage-run.json | jq '.entity_coverage[] | {type: .entity_type, deterministic: .derivation_distribution.deterministic_static, llm: .derivation_distribution.inferred_llm}'
```

---

## Step 6: Identify Gaps to Improve

Based on the report, decide which gaps matter most.

### Ask These Questions

1. **Is it worth fixing?** 
   - High-coverage gaps (already 85%+) may not justify effort
   - Low-coverage gaps (under 50%) are usually worth tackling

2. **Is it extractable?**
   - Can you write a reliable extractor? (Yes → pursue)
   - Or is it best-effort heuristic? (Maybe → lower priority)

3. **What's the impact?**
   - Does this entity type matter for your queries?
   - Or is it nice-to-have?

### Gap Categories

**Type A: Easily Fixed Gaps** (prioritize these)
- Missing decorator patterns (Flask `@app.post()`)
- Missing import types (optional dependencies in pyproject.toml)
- Missing framework registration (FastAPI `app.include_router()`)

**Type B: Hard but High-Value Gaps** (tackle after Type A)
- Service boundary detection (microservices architecture)
- Event publisher/consumer extraction
- Gait-based call detection (dynamic imports)

**Type C: Accept and Document** (low priority)
- Docstring-based entity inference (unreliable)
- Runtime-only patterns (can't extract statically)
- Private vendor frameworks (limited applicability)

### Example: Flask Endpoint Gap

Suppose your report shows: "Flask endpoints: 40% coverage."

**Step 1: Understand the gap**
- Found 18 endpoints, but expect 45
- Manual inspection shows missing patterns:
  - Decorator-based: `@app.route('/path')` (captured)
  - Programmatic: `app.add_url_rule('/path', 'name', handler)` (missed)
  - Class-based: `@app.post() class WebhookHandler` (missed)

**Step 2: Decide: Improve or accept?**
- Improving → Write extended Flask extractor (see [Extend with Custom Extractor](./extend-with-custom-extractor.md))
- Accept → Document in BACKLOG.md; rely on 40% coverage

**Step 3: Plan improvement**
- Extend the Flask extractor to handle `add_url_rule()` calls
- Add AST visitor for class-based views
- Test with fixtures
- Re-run coverage to verify improvement

---

## Step 7: Next Steps

### If You Found Gaps You Want to Fill

Jump to **[Extend with Custom Extractor](./extend-with-custom-extractor.md)** for a detailed walkthrough of:
- When to write a custom extractor
- How extractors work
- Building and testing one for your gap

### If Your Coverage is Acceptable

Use coverage as a **baseline for future work**:

1. **Save this report** — Store `coverage-run.json` alongside your snapshot
2. **Commit metadata** — Track report in git (optional, helps with historical comparison)
3. **Re-run periodically** — After major code changes, rebuild the snapshot and re-run coverage
4. **Detect regressions** — If coverage drops unexpectedly, use `jq` to diff old vs new JSON

### If You Want to Track Multiple Snapshots

Build coverage comparisons:

```bash
# Build reports for multiple repos or versions
python -m source.scripts.coverage_report \
  --snapshot ./data/kg_runs/flask-v1 \
  --out docs/evaluation/runs/flask-v1 \
  --run-id flask-v1

python -m source.scripts.coverage_report \
  --snapshot ./data/kg_runs/flask-v2 \
  --out docs/evaluation/runs/flask-v2 \
  --run-id flask-v2

# Compare side-by-side
cat docs/evaluation/runs/flask-v1/coverage-run.json | jq .entity_coverage
cat docs/evaluation/runs/flask-v2/coverage-run.json | jq .entity_coverage
```

---

## Troubleshooting

### Issue: "coverage.jsonl not found"

**Cause**: Snapshot was built with an older version of SuperContext

**Solution**:
```bash
# Rebuild the snapshot with current version
python -m source.scripts.build_kg --repo /tmp/flask --out data/kg_runs/flask-first
```

### Issue: Metrics command hangs or is slow

**Cause**: Very large snapshot (100,000+ entities)

**Solution**:
```bash
# Add --limit to cap analysis
python -m source.scripts.coverage_metrics --snapshot ./data/kg_runs/flask-first --limit 1000
```

### Issue: Report shows 0% coverage for some predicates

**Cause**: Extractor not implemented for that language/framework

**Solution**: Check the coverage.json `state` field:
```bash
cat docs/evaluation/runs/flask-first-coverage/coverage-run.json | jq '.gaps[] | select(.coverage_percent == 0)'
```

Look for `"state": "uninstrumented"`. Implement the extractor, or document the limitation.

---

## Summary

You now know how to:

1. **Measure** coverage with `coverage_metrics`
2. **Generate** human-readable and JSON reports
3. **Interpret** coverage data to identify gaps
4. **Decide** which gaps to improve
5. **Track** progress over time

**Key takeaway**: Coverage metrics guide your improvement priorities. Start with the lowest-coverage predicates (highest impact), and re-run metrics after each improvement to verify it worked.

---

## Quick Reference

### Commands Used in This Guide

| Command | Purpose |
|---------|---------|
| `python -m source.scripts.coverage_metrics --snapshot <path>` | Compute coverage metrics |
| `python -m source.scripts.coverage_report --snapshot <path> --out <dir>` | Generate formatted report |

### Files Created

| File | Contains |
|------|----------|
| `<snapshot>/metrics.jsonl` | Aggregated coverage metrics |
| `<out>/coverage-run.md` | Human-readable report |
| `<out>/coverage-run.json` | Machine-readable metrics |

### Report Sections

| Section | Shows |
|---------|-------|
| Metrics Summary | Overall scores and status |
| Entity Coverage | Found vs expected by type/language |
| Gaps Analysis | Lowest-coverage predicates |
| Recommendations | Prioritized improvement actions |

---

**Next step**: Ready to improve your coverage? See [Extend with Custom Extractor](./extend-with-custom-extractor.md).
