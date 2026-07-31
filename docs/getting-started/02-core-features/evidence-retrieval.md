# Evidence Retrieval: Grounding Knowledge Graph Facts in Source Code

**A guide to evidence retrieval in SuperContext—how facts are backed by code, how to verify them, and how the two-mode retrieval ladder works.**

**Last updated**: 2026-05-25

---

## Part 1: What is Evidence?

### The Problem: How Do We Know Facts Are True?

When you ask SuperContext "Who calls the `authenticate()` function?" it returns a list. But how do you know that answer is correct? How do you verify that function A actually calls authenticate() and it's not a false positive from the extractor?

SuperContext solves this by **requiring every fact to be backed by evidence** — the actual code bytes that prove the claim.

### The Solution: Evidence-Backed Facts

Every fact in the SuperContext knowledge graph carries a **bytes reference** — a precise pointer to the location in your source code that proves the fact:

```
Fact: "get_user_info() calls authenticate()"
↓
Evidence: {
  "repo": "auth-service",
  "commit_sha": "abc1234567890def1234567890abcdef12345678",
  "path": "app/auth.py",
  "line_start": 45,
  "line_end": 46
}
↓
Actual bytes: authenticate(user)  # Line 45-46 in auth.py at commit abc123...
```

This is critical because:

1. **Verifiability** — You can always trace a fact back to the exact code line that proves it
2. **Trust** — No guessing, no inference without evidence markers
3. **Debugging** — If a fact seems wrong, you can inspect the actual code and ask why the extractor derived it
4. **Compliance** — Prove to auditors that AI recommendations are grounded in your actual codebase

### Why Evidence Matters

**Example: Risky Refactoring**

You want to refactor `process_order()`. The graph says 42 functions call it. Without evidence:

> "42 callers" — Do you trust this? Are they all real? Did the extractor miss some? False positives?

With evidence:

> "42 callers (all backed by evidence at specific file:line:commit). Click any result to see the actual code line."

You can spot-check a few, trust the rest.

**Example: Safe Deletion**

SuperContext says "No one calls `old_payment_handler()` anymore." Without evidence, you worry about edge cases. With evidence:

> You can verify that the graph truly searched the entire codebase and found zero calls. Not "we didn't look hard enough" — "we searched and found nothing."

**Example: Reasoning with Certainty**

When an AI agent uses SuperContext for planning, evidence lets the agent reason about certainty:

> "Function A calls B (high confidence: direct code reference)" vs. "Function A might call C (low confidence: inferred from patterns)"

---

## Part 2: Mode A — Commit-Pinned Retrieval

### What is Mode A?

**Mode A** is the highest-trust evidence retrieval method. It fetches raw source code bytes directly from Git at a **pinned commit**, using exact coordinates.

When you ask for evidence about a fact, Mode A guarantees:
- **Immutability** — The bytes at that commit:path:line will never change
- **Cryptographic verifiability** — Git's commit SHA serves as a content hash
- **No network dependency** — Works offline with a local clone

### How It Works

Every fact in the graph carries a `bytes_ref` structure:

```json
{
  "bytes_ref": {
    "repo": "payment-service",
    "commit_sha": "abc1234567890def1234567890abcdef12345678",
    "path": "handlers/order.py",
    "line_start": 87,
    "line_end": 88
  }
}
```

When you ask to verify this fact, Mode A:

1. **Clones or fetches the repo** at the pinned commit SHA
2. **Reads the file** at the exact path
3. **Extracts the line range** (lines 87–88)
4. **Returns the raw bytes** so you can inspect them

Example output:

```
Commit: abc1234567890def (2026-05-15 08:32:14 UTC)
File: payment-service/handlers/order.py
Lines 87–88:

    process_payment(order_id)
    notify_user(order_id)

Evidence status: VERIFIED ✓
```

### Advantages of Mode A

**Always Available** — If the commit exists in Git, you can fetch it. Commit SHAs are permanent references.

**Immutable** — Changing code at `HEAD` does not invalidate evidence from an old commit. You can ask "prove this fact from March" and get the exact March code.

**Cryptographically verifiable** — Git's SHA-1 (or SHA-256) ensures the bytes cannot be tampered with.

**No external dependency** — Works with any Git repository, no special indexing or preprocessing required.

### When Mode A Runs

Mode A is mandatory for:

- **Safety-critical facts** — Any claim that affects refactoring decisions, breaking-change warnings, or deploy sequencing
- **Surfaced results** — Any fact you return to the user in an IDE
- **Compliance audits** — When you need to prove "we cited the actual code"
- **Cross-repo queries** — Facts that span multiple repositories

It is optional for:

- **Cached results** — Facts recently verified can be cached without re-fetching
- **Historical queries** — Questions about "what was the codebase on June 1st?"

### bytes_ref Structure

The complete bytes reference includes:

```json
{
  "bytes_ref": {
    "repo": "string - Repository name or path",
    "commit_sha": "string - Git commit SHA (40 hex chars)",
    "path": "string - File path relative to repo root",
    "line_start": "integer - Start line (1-indexed)",
    "line_end": "integer - End line inclusive",
    "content_hash": "string - SHA-256 of fetched bytes (optional)"
  }
}
```

**Example usage:**

```bash
# Fetch the actual code for a fact
supercontext-init --serve

# In your agent:
# Ask for evidence of fact ID "fact_xyz"
# Returns bytes_ref above plus the actual code lines
```

### Implementation with go-git and pygit2

Mode A is implemented using:

- **go-git** — For Go-based environments
- **pygit2** — For Python-based environments

Both libraries let you:

```python
# Open a repo at a specific commit
repo = pygit2.Repository("/path/to/repo")
commit = repo[commit_sha]

# Read a file
blob = commit.tree[path]
content = blob.data.decode('utf-8')

# Extract lines
lines = content.split('\n')[line_start - 1:line_end]
```

---

## Part 3: Mode B — Selective Ladder Retrieval

### What is Mode B?

**Mode B** is a three-step search ladder for facts that can be re-derived or when you need flexible, approximate evidence. It runs on-demand and is more expensive (slower, more tokens), but more flexible.

The ladder:

```
┌─────────────────────────────┐
│  Query: "Find calls to X"   │
└──────────────┬──────────────┘
               │
        ┌──────▼──────┐
        │ Step 1      │
        │ ripgrep     │  Fast lexical search (~10ms)
        │ Find "X("   │
        └──────┬──────┘
               │ Found? Return.
               │ Not found?
        ┌──────▼─────────┐
        │ Step 2         │
        │ AST-grep       │  Structural pattern search (~100ms)
        │ Parse syntax   │  (only for specific frameworks)
        └──────┬─────────┘
               │ Found? Return.
               │ Not found?
        ┌──────▼──────────┐
        │ Step 3          │
        │ Claude Agent    │  Reasoning + code exploration
        │ Explorer        │  (budgeted, ~1-5s)
        └─────────────────┘
```

### When Mode B Runs

Mode B is selective — it does not run for every query. It runs when:

- **User asks for source/evidence** — "Show me where X is used"
- **Literal or cross-repo query** — Facts that span many repos where exact graph coordinates are insufficient
- **Graph returns low confidence** — Coverage shows `partially_instrumented` or `uninstrumented` state
- **Safety-critical independent verification** — You want a second opinion from code search, not just the graph
- **Ambiguity resolution** — Multiple symbol names collide; need to disambiguate by searching code

### Step 1: Lexical Search via ripgrep

**ripgrep** is a fast, parallel, line-oriented search tool. Mode B uses it for:

```bash
# Search for exact string
rg "authenticate()" /path/to/repo

# Search for symbol name
rg "def authenticate" /path/to/repo

# Search for function calls (loose pattern)
rg "authenticate\s*\(" /path/to/repo
```

**Output:**

```
payment-service/handlers/orders.py:45:    authenticate(user_id)
payment-service/handlers/payments.py:102:   result = authenticate(session)
web-ui/src/api.ts:78:  await authenticate(token)
```

**Cost:** ~10ms for a typical repo, ~100ms for a 50-repo fleet.

**Coverage:** Finds anything mentioned in source code — function calls, variable assignments, config references, error messages, comments. Good for "where is this string mentioned?" but not precise enough for "is this truly a function call?"

### Step 2: Structural Search via AST-grep

When ripgrep finds many false positives (e.g., searching for "order" finds "order_id", "order_handler", "recorded", etc.), **ast-grep** provides precise structural pattern matching.

```javascript
// Pattern: find all function calls to "authenticate"
// AST-grep pattern:
{
  "pattern": "$FUNC(authenticate)($$$)",  // Calls to authenticate() with any args
  "kind": "call_expression"
}
```

**Output:**

```
payment-service/handlers/orders.py:45:    authenticate(user_id)     ← Confirmed call
payment-service/handlers/payments.py:102:  result = authenticate(session)  ← Confirmed call
web-ui/src/api.ts:78:  await authenticate(token)  ← Confirmed call
```

AST-grep filters out false positives by parsing syntax trees, not just pattern-matching text.

**Cost:** ~100ms per language per repo (requires parsing).

**Status in v1:** Available only for specific framework patterns (e.g., Flask endpoint detection, Express route handlers). Not broad coverage.

### Step 3: Agentic Exploration

When lexical and structural search do not suffice, Mode B escalates to **Claude Agent with a narrow tool allowlist**.

The agent has access to:
- `read_file(path)` — Read specific files
- `glob(pattern)` — Search for matching file paths
- `grep(pattern)` — Run ripgrep
- `list_dir(path)` — List directory contents

The agent can reason through ambiguous code:

> I'm looking for calls to `authenticate()`. Ripgrep found 5 hits, but some are in comments. Let me read those files and filter to true function calls.

**Cost:** Expensive. ~1–5 seconds, multiple tokens per query, because the agent is reasoning and potentially reading many files.

**Budget:** Configurable limits prevent runaway exploration. Default: 10 files read, 30 seconds of runtime per query.

### Process Flow Example

**Query:** "Find all uses of the deprecated `old_login_handler()` function"

1. **ripgrep** — Searches for `"old_login_handler"` → finds 23 matches in 8 files
2. **Filtering** — Many are in comments or test code → agent reads the 8 files to confirm real usage
3. **Result** — Returns 6 confirmed production calls + 3 test calls + 14 false positives (comments)

---

## Part 4: Verification

### How to Verify Evidence

When a query returns results, each result includes evidence metadata. To verify:

1. **Inspect the bytes_ref** — Locate repo, commit, path, line range
2. **Fetch the bytes** — Use Mode A to get the actual code at that commit
3. **Compare** — Does the code match the claimed fact?

CLI example:

```bash
# Query returns this fact:
python -m source.scripts.query_kg --snapshot .supercontext/kg \
  find-callers authenticate --limit 3

# Output includes evidence:
# {
#   "symbol": "authenticate",
#   "callers": [
#     {
#       "caller": "get_user_info",
#       "file": "app/auth.py",
#       "line": 45,
#       "evidence_id": "ev_abc123"
#     }
#   ]
# }

# Fetch evidence to verify:
python -m source.scripts.verify_evidence ev_abc123
```

Output:

```
Evidence ID: ev_abc123
Type: CALLS relation
Fact: get_user_info() calls authenticate()

Mode A verification (commit-pinned):
Repo: auth-service
Commit: abc1234567890def1234567890abcdef12345678
Path: app/auth.py
Lines: 45

Actual bytes at that commit:
───────────────────────────────
45 |  def get_user_info(user_id):
46 |      authenticate(user_id)
───────────────────────────────

Status: VERIFIED ✓
Confidence: HIGH (direct code reference)
```

### Interpreting Results

**VERIFIED** — Evidence bytes matched the claimed fact. High confidence.

**PARTIAL** — Evidence is present but does not fully confirm the claim. Example: fact says "service A calls service B," evidence shows "module in service A imports module in service B," but not the exact call.

**STALE** — Bytes were retrieved from an old commit. The code at `HEAD` may differ. Use evidence date to understand freshness.

**MISSING** — No bytes found at the coordinate. The evidence reference may be broken. The fact should be reviewed.

**UNINSTRUMENTED** — The scope (language, framework, file path) lacks coverage. The graph may not have extracted all facts in this area.

### Understanding bytes_ref Fields

**repo** — Repository identifier (name or path). Used to locate the Git repository.

**commit_sha** — Full 40-character Git commit SHA. Immutable; uniquely identifies a code snapshot.

**path** — File path relative to repo root (not absolute). Example: `src/handlers/auth.py`, not `/home/user/auth-service/src/handlers/auth.py`.

**line_start / line_end** — 1-indexed line numbers. `line_start: 45, line_end: 46` means lines 45 and 46 inclusive.

**Example:**

```
File path: payment-service/handlers/order.py
Line 87: def process_order(order_id):
Line 88:     charge_card(order_id)

bytes_ref: {
  "repo": "payment-service",
  "commit_sha": "abc1234...",
  "path": "handlers/order.py",
  "line_start": 87,
  "line_end": 88
}
```

### Sourcing Facts Back to Code

Workflow:

1. **Agent calls a tool** — Example: `find_callers("process_payment")`
2. **Tool returns results** — Each result includes `evidence_ids`
3. **Agent asks for evidence** — Calls `get_evidence(evidence_id)`
4. **Evidence service returns bytes_ref** — Points to exact code location
5. **Agent fetches code** — Uses Mode A to retrieve actual bytes
6. **Agent cites code** — Shows you the evidence in context

Real example in IDE:

```
Agent's answer:
"process_payment() is called by 5 functions:
  1. charge_order() [auth-service, handlers/order.py:45]
     Click to see → authenticate(user) ← Evidence
  2. retry_payment() [payment-service, api/handlers.py:102]
     Click to see → authenticate(session) ← Evidence
  ..."
```

Clicking a link retrieves the bytes at that commit and shows the exact line.

### When Verification Fails

If you find that verification fails (expected code does not match the bytes_ref), investigate:

1. **Stale snapshot** — If the code has changed since the snapshot was built, Mode A retrieves the old code from Git. Rebuild the snapshot with `supercontext-init --refresh` to pick up new code.

2. **Extractor error** — The graph may have incorrectly derived the fact. Check the extractor logic for that relation type (in `source/kg/extractors/`).

3. **Missing evidence** — The bytes_ref may point to the wrong line or file. This indicates a bug in the extractor's evidence collection.

4. **Cross-repo coordination** — If the fact involves multiple repositories, ensure all repos are included in the snapshot. Cross-repo evidence is more complex.

5. **Language-specific parsing** — Some languages have syntax that confuses parsers. TypeScript dynamic imports, Python metaclasses, and Go interface assertions are examples.

**Report tool:**

```bash
# Report verification failures for debugging
python -m source.scripts.report_evidence_failures \
  --snapshot .supercontext/kg \
  --sample-size 100
```

This command spot-checks 100 random evidence entries and reports any that fail verification.

---

## Cross-References

- **`knowledge-graph.md`** — KG structure and how entities/facts are organized
- **`querying.md`** — How to query the graph and interpret results
- **`mcp-integration.md`** — How MCP tools return evidence
- **`adr/0005-modular-evidence-retrieval-with-coordinate-fetch-and-selective-ladder.md`** — Full architectural decision
- **`adr/0006-canonical-ontology-and-fact-metadata-envelope.md`** — Entity + Fact + Evidence data model
- **`docs/getting-started/README.md`** — Getting started overview
