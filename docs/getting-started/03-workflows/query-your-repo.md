# Query Your Repo: Running Queries Against Your Knowledge Graph

**A hands-on tutorial for querying the knowledge graph you just built.**

**Time estimate**: 30-40 minutes | **Difficulty**: Beginner

**Prerequisites**: Complete [Setup and Build Your First KG](./setup-and-first-kg.md) first

**Last updated**: 2026-05-25

---

## Overview

You now have a knowledge graph snapshot of your codebase. This guide teaches you to **query it** to answer real questions about your code:

- Who calls this function?
- What does this function call?
- If I change this, what breaks?
- What are my top dependencies?

You'll run 8 queries step-by-step, learning the syntax, interpreting results, and chaining queries together.

---

## Part 1: Query Basics

### Command Structure

All queries use the same syntax:

```bash
python -m source.scripts.query_kg --snapshot <snapshot-path> <query-name> <arguments> [--limit N]
```

| Part | Example | Meaning |
|------|---------|---------|
| `--snapshot <path>` | `data/kg_runs/flask-first` | Path to your KG snapshot |
| `<query-name>` | `find-callers` | The query type |
| `<arguments>` | `flask.route` | What to search for |
| `--limit N` | `--limit 5` | Maximum results (optional) |

### Output Format

By default, queries return **human-readable tables**:

```
Function: authenticate()
Called by:
  - main.get_user_info (app.py:11)
  - main.post_login (app.py:25)
```

For scripting, use `--format json` to get structured output:

```bash
python -m source.scripts.query_kg --snapshot data/kg_runs/flask-first \
  find-callers flask.route --format json
```

### Getting Help

Every query has built-in documentation:

```bash
python -m source.scripts.query_kg --help
```

---

## Part 2: Eight Queries, Step-by-Step

### Query 1: Summary — Overview of Your Knowledge Graph

**What it answers**: How much of my codebase did SuperContext extract? What entity and fact counts do I have?

**Your task**: Get a high-level view of your snapshot.

**Command**:
```bash
python -m source.scripts.query_kg --snapshot data/kg_runs/flask-first summary
```

**Expected output**:
```
Knowledge Graph Summary
=======================

Snapshot: data/kg_runs/flask-first
Built: 2026-05-25T14:32:10Z
Repo commit: a1b2c3d4e5f6g7h8i9j0

Entities (nodes):
  CodeModule:        340
  CodeSymbol:      1,243
  ExternalPackage:    67
  ────────────────
  Total:           1,650

Facts (edges):
  IMPORTS:           340
  CALLS:           3,421
  DEFINED_IN:      1,243
  ────────────────
  Total:           5,004

Coverage:
  Instrumented files:    342 (Python)
  Partially instrumented: 0
  Uninstrumented:        0

Top packages:
  requests:         52 imports
  werkzeug:         38 imports
  pytest:           21 imports
  jinja2:           15 imports
  click:            12 imports
```

**How to read it**:
- **1,650 entities** means 1,243 functions, 340 modules, and 67 external packages were extracted
- **5,004 facts** means the extractor found 3,421 call relationships and 340 imports
- **Coverage 100%** means all files were parsed successfully
- **Top packages** tells you Flask's most common dependencies

**Try this next**: Pick one of the top packages (e.g., `requests`) and run Query 6 to see who imports it.

---

### Query 2: Find Callers — Who Calls This Function?

**What it answers**: Which functions call this one? Where are they located?

**Your task**: Find all callers of a function. For Flask, let's examine `jsonify`:

**Command**:
```bash
python -m source.scripts.query_kg --snapshot data/kg_runs/flask-first \
  find-callers flask.jsonify --limit 10
```

**Expected output**:
```
Function: flask.jsonify

Called by (10 results):

1. flask.app.Flask.json
   Location: src/flask/app.py:1421
   Calls: 1 time

2. examples.tutorial.hello.create_app
   Location: examples/tutorial/hello.py:15
   Calls: 1 time

3. tests.test_json.test_send_json
   Location: tests/test_json.py:34
   Calls: 2 times

4. tests.test_api.test_list_todos
   Location: tests/test_api.py:112
   Calls: 1 time

(6 more results...)

Total: 47 callers found
```

**How to read it**:
- Each result is a function that calls `flask.jsonify`
- **Location** shows the file path and line number
- **Calls** shows how many times that function calls it
- **Total** shows the complete count

**Interpretation**: `jsonify` is called 47 times across Flask's codebase — it's a commonly-used utility.

**Try this next**: Pick one of these callers and run `find-callers` on it to see its callers too (building a call chain).

---

### Query 3: Find Callees — What Does This Function Call?

**What it answers**: Which functions does this one call? What dependencies does it have?

**Your task**: Find all functions called by `create_app`:

**Command**:
```bash
python -m source.scripts.query_kg --snapshot data/kg_runs/flask-first \
  find-callees flask.Flask --limit 10
```

**Expected output**:
```
Function: flask.Flask

Calls (10 results):

1. werkzeug.routing.MapAdapter
   Location: src/flask/app.py:456
   Called: 3 times

2. jinja2.Environment
   Location: src/flask/app.py:501
   Called: 1 time

3. flask.globals.request_ctx_push
   Location: src/flask/app.py:789
   Called: 1 time

4. builtins.setattr
   Location: src/flask/app.py:234
   Called: 6 times

(6 more results...)

Total: 31 callees found
```

**How to read it**:
- Each result is a function that this one calls
- **Calls** shows how many times this function calls it
- **Location** shows where in the code the call happens

**Interpretation**: The Flask app initializer calls 31 different functions, including key dependencies like werkzeug routing and Jinja2 templates.

**Try this next**: Run `find-callers` on one of these callees to understand its broader impact.

---

### Query 4: Blast Radius — What Breaks If I Change This?

**What it answers**: If I modify this function, what will break? Show the full transitive impact.

**Your task**: Find the blast radius of `url_for`, a critical Flask routing function:

**Command**:
```bash
python -m source.scripts.query_kg --snapshot data/kg_runs/flask-first \
  blast-radius flask.url_for --depth 3
```

**Expected output**:
```
Symbol: flask.url_for

Blast Radius (depth 3):

Direct callers (1 hop):
  - flask.helpers.url_for (18 locations)
  - tests.test_routing.test_url_for (7 locations)
  - tests.test_routing.test_external_url (3 locations)

Indirect impact (2 hops):
  ├─ test_api.test_list_todos
  │  ├─ test_helpers.test_url_generation
  │  └─ test_routing.test_subdomain_urls
  └─ examples.tutorial.hello.index
     └─ examples.tutorial.hello.item_detail

Transitive impact (3 hops):
  └─ tests.integration.test_all_examples

Impact Summary:
  Direct dependents:      3
  Indirect dependents:   15
  Transitive dependents: 42
  Total affected:        60
```

**How to read it**:
- **Direct callers** — Functions that directly call `url_for`
- **Indirect impact** — Functions that call those callers (2 hops away)
- **Transitive** — The full chain (up to depth limit)
- **Impact Summary** — How many functions would be affected overall

**Interpretation**: Changing `url_for` could impact 60 different locations in Flask — a risky operation that needs careful testing.

**Try this next**: Use `--depth 5` for a deeper look at transitive dependencies.

---

### Query 5: Top Dependencies — What Do I Depend On Most?

**What it answers**: Which packages or modules are most commonly imported? Where is my technical debt?

**Your task**: Identify Flask's most heavily-depended-upon packages:

**Command**:
```bash
python -m source.scripts.query_kg --snapshot data/kg_runs/flask-first \
  top-dependencies --limit 10
```

**Expected output**:
```
Top Dependencies by Import Frequency

1. werkzeug
   Imported by: 18 modules
   Total imports: 52

2. jinja2
   Imported by: 12 modules
   Total imports: 38

3. click
   Imported by: 8 modules
   Total imports: 21

4. itsdangerous
   Imported by: 6 modules
   Total imports: 15

5. markupsafe
   Imported by: 4 modules
   Total imports: 12

(5 more)
```

**How to read it**:
- **Rank** — Position by import frequency
- **Imported by** — How many modules depend on it
- **Total imports** — Sum of all import statements across the codebase

**Interpretation**: Flask critically depends on werkzeug (52 times) — a version bump could be risky. Click is less critical (21 times).

**Try this next**: Run Query 6 to see exactly which modules import these packages.

---

### Query 6: Modules Importing a Package — Who Uses This Dependency?

**What it answers**: Which of my modules import a specific package? Where is it used?

**Your task**: Find all modules that import werkzeug:

**Command**:
```bash
python -m source.scripts.query_kg --snapshot data/kg_runs/flask-first \
  modules-importing werkzeug --limit 10
```

**Expected output**:
```
Modules importing: werkzeug

Results (10 of 18 total):

1. src/flask/app.py
   Import: from werkzeug.exceptions import HTTPException
   Uses: 4 locations

2. src/flask/routing.py
   Import: from werkzeug.routing import MapAdapter, Map
   Uses: 8 locations

3. src/flask/testing.py
   Import: import werkzeug
   Uses: 3 locations

4. src/flask/security.py
   Import: from werkzeug.security import generate_password_hash
   Uses: 2 locations

(6 more)
```

**How to read it**:
- **Module path** — Which file imports the package
- **Import statement** — The actual import line
- **Uses** — How many times it's used in that module

**Interpretation**: werkzeug is imported in 18 modules, most heavily in routing.py (8 uses). This is Flask's routing engine — critical to protect.

**Try this next**: Pick one of these modules and run `find-callees` on its main function to understand its dependencies in context.

---

### Query 7: Dependency Info — Deep Dive Into One Package

**What it answers**: Everything about one package: who imports it, what version, where it comes from.

**Your task**: Get complete information about requests:

**Command**:
```bash
python -m source.scripts.query_kg --snapshot data/kg_runs/flask-first \
  dependency-info click
```

**Expected output**:
```
External Package: click

Status: Instrumented (actively imported)

Imported by 8 modules:
  - src/flask/cli.py (4 imports)
  - src/flask/commands.py (2 imports)
  - examples/tutorial/create_app.py (1 import)
  - tests/test_cli.py (8 imports)
  (4 more)

Import statements:
  - import click
  - from click import command, option, argument
  - from click.exceptions import ClickException

Callers in your codebase:
  - flask.cli.main (34 calls)
  - flask.cli.run_server (12 calls)
  - examples.commands.init_db (5 calls)

Version constraints:
  - Specified in: requirements.txt, pyproject.toml
  - Current version: >=7.1

Risk assessment:
  - Critical: HIGH (part of Flask CLI)
  - Breakage risk: MEDIUM (interface changes would affect 8 modules)
```

**How to read it**:
- **Status** — Is this package actually used?
- **Imported by** — Which modules depend on it
- **Import statements** — How it's imported (specific functions vs. whole package)
- **Risk assessment** — Qualitative breakdown of impact

**Interpretation**: Click is used heavily in Flask's CLI — a breaking change in click would impact Flask CLI functionality.

**Try this next**: Run `find-callers` on one of the listed callers to trace the impact further.

---

### Query 8: Cross-Repo Links (Multi-Repo Snapshots)

**What it answers**: If you built a snapshot of multiple repositories, which services depend on which?

**Your task**: (Only applicable if you built a multi-repo snapshot)

If you have a multi-repo snapshot (e.g., Flask + another service):

**Command**:
```bash
python -m source.scripts.query_kg --snapshot data/kg_runs/multi-repo \
  find-callees service-a --limit 10
```

**Expected output**:
```
Service: service-a

Calls (cross-repo):

1. service-b.handler
   Repo: service-b
   Type: HTTP endpoint call
   Evidence: service-a/client.py:45

2. shared-lib.utils.parse_json
   Repo: shared-library
   Type: Internal library call
   Evidence: service-a/api.py:102

...
```

**How to read it**:
- **Service** — The target service
- **Calls** — Functions or endpoints in other repos it depends on
- **Repo** — Which repository the callee is in
- **Type** — HTTP call, function call, event publish, etc.

**Interpretation**: Cross-repo queries show inter-service dependencies.

**Try this next**: For each cross-repo dependency, run `blast-radius` to see transitive impact across services.

---

## Part 3: Combining Queries — Building Investigation Chains

Real-world analysis chains queries together. Here's a workflow:

### Workflow: "What if I deprecate this function?"

**Step 1: Find who calls it**
```bash
python -m source.scripts.query_kg --snapshot data/kg_runs/flask-first \
  find-callers flask.url_for
```

Result: 47 callers found.

**Step 2: For the top 3 callers, get their blast radius**
```bash
python -m source.scripts.query_kg --snapshot data/kg_runs/flask-first \
  blast-radius test_routing.test_url_for --depth 2
```

This shows what breaks if each caller stops working.

**Step 3: Identify the modules with highest impact**
```bash
python -m source.scripts.query_kg --snapshot data/kg_runs/flask-first \
  find-callees test_routing.test_url_for
```

Understand what each high-impact caller depends on.

**Step 4: Check if there are easier alternatives**
```bash
python -m source.scripts.query_kg --snapshot data/kg_runs/flask-first \
  modules-importing flask
```

See if other modules import Flask that might be affected.

**Conclusion**: Use these results to decide:
- Is deprecation safe? (if few callers)
- What's the migration path? (which modules need updates)
- In what order should I migrate? (start with low-impact modules)

---

## Part 4: Advanced: Combining with jq for Filtering

For power users, use `--format json` with `jq` to filter results:

### Example 1: Find callers only in tests/

```bash
python -m source.scripts.query_kg --snapshot data/kg_runs/flask-first \
  find-callers flask.jsonify --format json | \
  jq '.results[] | select(.location | contains("tests/"))'
```

### Example 2: Count total call frequency

```bash
python -m source.scripts.query_kg --snapshot data/kg_runs/flask-first \
  find-callers flask.jsonify --format json | \
  jq '[.results[].call_count] | add'
```

**Result**: `47` (total number of calls to jsonify)

### Example 3: Filter by module

```bash
python -m source.scripts.query_kg --snapshot data/kg_runs/flask-first \
  find-callers flask.jsonify --format json | \
  jq '.results[] | select(.caller | contains("flask.app"))'
```

### Example 4: Save results to file

```bash
python -m source.scripts.query_kg --snapshot data/kg_runs/flask-first \
  blast-radius flask.url_for --depth 3 --format json > blast-radius.json

# Then analyze offline
jq '.impact_summary' blast-radius.json
```

---

## Part 5: Troubleshooting

### Issue: "No results found" for a query

**Cause**: 
- Function name doesn't match exact symbol name
- Function is not in the snapshot
- Query uses wrong syntax

**Solution**:
1. Verify the exact function name:
```bash
python -m source.scripts.query_kg --snapshot data/kg_runs/flask-first \
  summary | grep -i "jsonify"
```

2. Use the full qualified name:
```bash
python -m source.scripts.query_kg --snapshot data/kg_runs/flask-first \
  find-callers flask.json.jsonify  # Use full path
```

### Issue: Query takes a long time

**Cause**: 
- Large blast radius (--depth is too high)
- Large snapshot (10,000+ entities)

**Solution**:
- Use `--limit` to reduce results:
```bash
python -m source.scripts.query_kg --snapshot data/kg_runs/flask-first \
  find-callers flask.jsonify --limit 5  # Just top 5
```

- Reduce depth:
```bash
python -m source.scripts.query_kg --snapshot data/kg_runs/flask-first \
  blast-radius flask.url_for --depth 2  # Instead of 5
```

### Issue: Unexpected results or missing symbols

**Cause**: 
- Snapshot is incomplete (coverage issues)
- Symbol was not extracted

**Solution**:
1. Check coverage:
```bash
python -m source.scripts.query_kg --snapshot data/kg_runs/flask-first \
  summary
```

2. Check if symbol exists at all:
```bash
python -m source.scripts.query_kg --snapshot data/kg_runs/flask-first \
  find-callers module.symbol --limit 1
```

3. If still missing, see [Evaluate Coverage](./evaluate-coverage.md) to diagnose extraction gaps.

---

## What's Next

Now that you can query your knowledge graph, you're ready to:

1. **[Evaluate Coverage](./evaluate-coverage.md)** — Measure extraction quality and understand gaps
2. **[Knowledge Graph Explained](../02-core-features/knowledge-graph.md)** — Learn more about entity and fact types
3. **Build a multi-repo snapshot** — Apply the same queries to multiple services

---

## Quick Reference

### Query Syntax

```bash
python -m source.scripts.query_kg \
  --snapshot <path> \
  <query-name> \
  <arguments> \
  [--limit N] \
  [--depth D] \
  [--format json|table]
```

### All Query Types

| Query | Purpose |
|-------|---------|
| `summary` | Overview of KG: entity and fact counts |
| `find-callers <symbol>` | Who calls this? |
| `find-callees <symbol>` | What does this call? |
| `blast-radius <symbol> --depth 2` | Full transitive impact |
| `top-dependencies` | Most-imported packages |
| `modules-importing <package>` | Which modules use this? |
| `dependency-info <package>` | Complete package info |
| Cross-repo (multi-snapshot) | Service dependencies across repos |

### Common Options

```bash
--limit 5           # Return only 5 results
--depth 3           # Transitive depth (blast-radius only)
--format json       # Output as JSON instead of table
--help              # View query documentation
```

---

*Ready to dive deeper?* Check out [Evaluate Coverage](./evaluate-coverage.md) to understand extraction quality.
