# Setup and Build Your First Knowledge Graph

**A step-by-step guide to installing SuperContext and building your first knowledge graph snapshot.**

**Time estimate**: 15-20 minutes | **Difficulty**: Beginner

**Last updated**: 2026-05-25

---

## Overview

This guide walks you through:
1. Verifying prerequisites on your machine
2. Installing SuperContext
3. Choosing a repository to analyze
4. Building your first knowledge graph snapshot
5. Verifying the snapshot was created correctly

By the end, you'll have a queryable knowledge graph of a real codebase and be ready to run queries against it.

---

## Step 1: Verify Prerequisites

Before installing SuperContext, ensure you have the required tools installed.

### Python 3.11+

SuperContext requires Python 3.11 or later.

```bash
python3 --version
```

**Expected output**:
```
Python 3.11.0
```
or later.

**If you don't have Python 3.11+**: 
- macOS: `brew install python@3.11`
- Ubuntu/Debian: `sudo apt-get install python3.11 python3.11-venv`
- Windows: Download from [python.org](https://www.python.org/downloads/)

### Node.js 16+

SuperContext can analyze TypeScript and JavaScript repositories. Node.js is optional but recommended.

```bash
node --version
```

**Expected output**:
```
v18.0.0
```
or later.

**If you don't have Node.js**: 
- Visit [nodejs.org](https://nodejs.org/) and install the LTS version
- Or: `brew install node` (macOS)

### Git

SuperContext uses git to resolve commit hashes and retrieve source bytes.

```bash
git --version
```

**Expected output**:
```
git version 2.40.0
```

**If you don't have git**: 
- macOS: `brew install git`
- Ubuntu/Debian: `sudo apt-get install git`
- Windows: Download from [git-scm.com](https://git-scm.com/)

---

## Step 2: Install SuperContext

Clone the SuperContext repository and install it locally.

### Clone the Repository

```bash
git clone https://github.com/roshansingh/bettercontext.git
cd bettercontext
```

### Install Dependencies

Create a Python virtual environment and install SuperContext:

```bash
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

Install the package with all optional dependencies:

```bash
pip install -e ".[python,typescript]"
```

This installs:
- Core SuperContext libraries
- Python AST extractor
- TypeScript/JavaScript extractor
- Query CLI tools

### Verify Installation

```bash
python -m source.scripts.build_kg --help
```

**Expected output**:
```
usage: build_kg.py [-h] --repo REPO --out OUT

Build a knowledge graph from a repository

options:
  -h, --help     show this help message and exit
  --repo REPO    Path to repository to analyze
  --out OUT      Output directory for KG snapshot
```

If you see the help message, installation was successful.

---

## Step 3: Choose a Repository

For your first knowledge graph, we recommend using a well-known open-source repository. This helps you:
- See realistic extraction results
- Explore a familiar codebase
- Run meaningful queries

### Recommended Starter Repositories

**Flask** (Python, ~1200 functions)
- Small, well-structured Python project
- Clear, readable code
- Fast build time (~2 minutes)

```bash
git clone https://github.com/pallets/flask.git /tmp/flask
```

**React** (TypeScript/JavaScript, ~2000 functions)
- Medium-sized JavaScript project
- Demonstrates TS/JS extraction
- Build time: ~3-4 minutes

```bash
git clone https://github.com/facebook/react.git /tmp/react
```

**Your Own Repository**
- Replace `/path/to/repo` with the path to your codebase
- SuperContext analyzes Python and TypeScript/JavaScript
- Works best with repos 500+ functions

### For This Guide

We'll use Flask as the example:

```bash
git clone https://github.com/pallets/flask.git /tmp/flask
```

---

## Step 4: Clone the Repository

Navigate to the repository you want to analyze. For Flask:

```bash
cd /tmp/flask
```

Verify the repo is a git repository:

```bash
git rev-parse --git-dir
```

**Expected output**:
```
.git
```

This confirms the repo is valid and git can access it.

---

## Step 5: Initialize SuperContext

Before building, initialize SuperContext for your project. This creates a `.supercontext/` directory to store configuration.

```bash
cd /Users/roshan/work/code/bettercontext
python -m source.scripts.build_kg --repo /tmp/flask --out data/kg_runs/flask-first
```

SuperContext will:
1. Scan the repository for Python and TypeScript files
2. Extract code entities and relationships
3. Generate evidence files
4. Write the snapshot to `data/kg_runs/flask-first`

**This takes 2-4 minutes.** You'll see progress output:

```
[INFO] Discovering files in /tmp/flask...
[INFO] Found 342 Python files
[INFO] Python extractor: Parsing AST...
[INFO] Python extractor: Extracting 1,243 functions
[INFO] Python extractor: Extracting 340 imports
[INFO] Writing snapshot to data/kg_runs/flask-first
[INFO] Build complete. Snapshot: data/kg_runs/flask-first
```

---

## Step 6: Build Your First Knowledge Graph

Now build the actual knowledge graph snapshot:

```bash
python -m source.scripts.build_kg --repo /tmp/flask --out data/kg_runs/flask-first
```

The command does:
1. **Scan** — Walks the repository, identifying all code files
2. **Parse** — Uses language-specific AST parsers (Python AST, TypeScript Compiler API) to extract code structure
3. **Extract** — Pulls entities (functions, modules, imports) and facts (calls, dependencies)
4. **Verify** — Cross-references extracted facts with source code
5. **Write** — Outputs the snapshot files

**Expected time**: 2-4 minutes for Flask

**Progress indicators**:
- `[INFO]` messages show extraction progress
- `[WARNING]` messages flag files that couldn't be parsed (usually safe)
- No `[ERROR]` messages expected for a clean repo

### What Gets Created

Five files are written to `data/kg_runs/flask-first/`:

1. **`entities.jsonl`** — All code entities (functions, modules, etc.)
2. **`facts.jsonl`** — All relationships (calls, imports, etc.)
3. **`evidence.jsonl`** — Source code proof for each fact (file, line, bytes)
4. **`coverage.jsonl`** — Extraction quality metrics per language/framework
5. **`manifest.json`** — Snapshot metadata (timestamp, repo commit, entity counts)

---

## Step 7: Verify the Snapshot

After the build completes, verify the snapshot was created:

```bash
ls -lah data/kg_runs/flask-first/
```

**Expected output**:
```
total 2.4M
-rw-r--r--  1 roshan  staff   892K entities.jsonl
-rw-r--r--  1 roshan  staff   1.2M facts.jsonl
-rw-r--r--  1 roshan  staff   156K evidence.jsonl
-rw-r--r--  1 roshan  staff    12K coverage.jsonl
-rw-r--r--  1 roshan  staff   2.8K manifest.json
```

Now run a summary query to inspect the snapshot:

```bash
python -m source.scripts.query_kg --snapshot data/kg_runs/flask-first summary
```

**Expected output** (summary will vary by repo):
```
Knowledge Graph Summary
=======================

Snapshot: data/kg_runs/flask-first
Built: 2026-05-25T14:32:10Z
Commit: abc1234def5678

Entities:
  CodeModule:        340
  CodeSymbol:      1,243
  ExternalPackage:    67
  ────────────────
  Total:           1,650

Facts (Relations):
  IMPORTS:           340
  CALLS:           3,421
  DEFINED_IN:      1,243
  ────────────────
  Total:           5,004

Coverage:
  Instrumented:     342 Python files
  Partially:          0
  Uninstrumented:     0
```

This confirms:
- **1,650 entities** were extracted (functions, modules, packages)
- **5,004 relationships** between them (calls, imports)
- All files were instrumented (coverage is good)

---

## Step 8: Troubleshooting

### Issue: `ModuleNotFoundError: No module named 'source'`

**Cause**: Virtual environment not activated or installation incomplete

**Solution**:
```bash
source venv/bin/activate  # macOS/Linux
pip install -e ".[python,typescript]"
```

### Issue: `[WARNING] Could not parse file: ...`

**Cause**: Parser encountered a syntax error or language construct it doesn't recognize

**Solution**: This is normal. A few unparseable files don't impact the overall snapshot. Check the manifest to see overall coverage.

### Issue: TypeScript/JavaScript files not extracted

**Cause**: Node.js dependencies not installed in the target repo

**Solution**:
```bash
cd /tmp/flask  # or your repo
npm ci  # Install dependencies
```

Then rebuild the KG.

### Issue: Snapshot directory is empty or missing files

**Cause**: Build failed silently

**Solution**: Run with verbose output:
```bash
python -m source.scripts.build_kg --repo /tmp/flask --out data/kg_runs/flask-first --verbose
```

Look for `[ERROR]` messages in the output.

### Issue: Build takes longer than expected

**Cause**: Large repository (10,000+ files) or slow disk I/O

**Solution**: Wait. Typical timings:
- Flask (~300 files): 2 minutes
- React (~2,000 files): 4 minutes
- Large monorepos (10,000+ files): 10-30 minutes

---

## Next Steps

Now that you have your first knowledge graph, you're ready to:

1. **[Query Your Repo](./query-your-repo.md)** — Learn the eight standard queries and explore your snapshot
2. **[Evaluate Coverage](./evaluate-coverage.md)** — Measure extraction quality and identify gaps
3. **[Architecture Overview](../01-concepts/architecture-overview.md)** — Understand how SuperContext works

---

## Quick Reference

### Commands Used in This Guide

| Command | Purpose |
|---------|---------|
| `git clone <url>` | Clone a repository |
| `python -m source.scripts.build_kg --repo <path> --out <dir>` | Build a KG snapshot |
| `python -m source.scripts.query_kg --snapshot <dir> summary` | View snapshot summary |

### File Locations

| Item | Path |
|------|------|
| Your Flask snapshot | `data/kg_runs/flask-first/` |
| Snapshot entities | `data/kg_runs/flask-first/entities.jsonl` |
| Snapshot facts | `data/kg_runs/flask-first/facts.jsonl` |
| SuperContext installation | `bettercontext/` (wherever you cloned it) |

---

*Ready to query? Jump to [Query Your Repo](./query-your-repo.md).*
