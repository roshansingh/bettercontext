# Getting Started Documentation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create 28 comprehensive onboarding documents with examples that enable new engineers to use and extend SuperContext.

**Architecture:** Feature-centric design with progressive depth. Documents organized in 4 layers: concepts (foundation), features (comprehensive reference), workflows (task-oriented), examples (runnable scripts). Entry point guides readers through learning paths.

**Tech Stack:** Markdown, Bash, Python, example repos (flask, react)

**Estimated Duration:** 35-40 hours total

---

## File Structure

### Directories to Create
```
docs/getting-started/
  01-concepts/
  02-core-features/
  03-workflows/
  examples/
    01-build/
    02-query/
    03-coverage/
    04-extend/
    05-mcp/
    real-repos/
```

### Files to Create (28 total)

**Entry Point & Reference (2 files)**
- `docs/getting-started/README.md`
- `docs/getting-started/GLOSSARY.md`

**Concept Docs (2 files)**
- `docs/getting-started/01-concepts/what-is-supercontext.md`
- `docs/getting-started/01-concepts/architecture-overview.md`

**Feature Docs (5 files)**
- `docs/getting-started/02-core-features/knowledge-graph.md`
- `docs/getting-started/02-core-features/querying.md`
- `docs/getting-started/02-core-features/coverage-metrics.md`
- `docs/getting-started/02-core-features/mcp-integration.md`
- `docs/getting-started/02-core-features/evidence-retrieval.md`

**Workflow Docs (4 files)**
- `docs/getting-started/03-workflows/setup-and-first-kg.md`
- `docs/getting-started/03-workflows/query-your-repo.md`
- `docs/getting-started/03-workflows/evaluate-coverage.md`
- `docs/getting-started/03-workflows/extend-with-custom-extractor.md`

**Example Docs & Scripts (15 files)**
- `docs/getting-started/examples/README.md`
- `docs/getting-started/examples/01-build/build-kg-single-repo.sh`
- `docs/getting-started/examples/01-build/build-kg-multi-repo.sh`
- `docs/getting-started/examples/02-query/query-common-patterns.sh`
- `docs/getting-started/examples/02-query/query-with-jq.sh`
- `docs/getting-started/examples/02-query/find-impact.py`
- `docs/getting-started/examples/03-coverage/coverage-full-pipeline.sh`
- `docs/getting-started/examples/03-coverage/coverage-compare.sh`
- `docs/getting-started/examples/04-extend/custom-extractor-template.py`
- `docs/getting-started/examples/04-extend/flask-routes-extractor.py`
- `docs/getting-started/examples/04-extend/extractor-test-template.py`
- `docs/getting-started/examples/04-extend/fixture-repo-setup.sh`
- `docs/getting-started/examples/05-mcp/start-mcp-server.sh`
- `docs/getting-started/examples/05-mcp/test-mcp-tool.py`
- `docs/getting-started/examples/real-repos/README.md`

---

## Implementation Tasks

### Phase 1: Foundation (1-2 hours)

### Task 1: Create folder structure and entry point

**Files:**
- Create: `docs/getting-started/` (directory)
- Create: `docs/getting-started/01-concepts/` (directory)
- Create: `docs/getting-started/02-core-features/` (directory)
- Create: `docs/getting-started/03-workflows/` (directory)
- Create: `docs/getting-started/examples/` (directory)
- Create: `docs/getting-started/examples/01-build/` (directory)
- Create: `docs/getting-started/examples/02-query/` (directory)
- Create: `docs/getting-started/examples/03-coverage/` (directory)
- Create: `docs/getting-started/examples/04-extend/` (directory)
- Create: `docs/getting-started/examples/05-mcp/` (directory)
- Create: `docs/getting-started/examples/real-repos/` (directory)

- [ ] **Step 1: Create all directories**

```bash
mkdir -p docs/getting-started/{01-concepts,02-core-features,03-workflows,examples/{01-build,02-query,03-coverage,04-extend,05-mcp,real-repos}}
```

- [ ] **Step 2: Verify directory structure**

```bash
find docs/getting-started -type d
```

Expected: All 11 directories listed

- [ ] **Step 3: Commit**

```bash
git add docs/getting-started/
git commit -m "chore: Create Getting Started documentation directory structure"
```

---

### Task 2: Create README.md (entry point)

**Files:**
- Create: `docs/getting-started/README.md`

- [ ] **Step 1: Write README.md with learning paths**

```markdown
# Getting Started with SuperContext

Welcome to SuperContext! This guide helps you understand, install, and extend our knowledge graph system.

## What is SuperContext?

SuperContext solves a critical problem in microservice organizations: **change safety**. When you edit service A, SuperContext tells you which services B–Z might break before you write the code.

It works by building a typed knowledge graph of your codebase: functions, imports, endpoints, event channels, dependencies. Then it answers questions like:
- Who calls this function?
- What will break if I change this?
- How many external services depend on this endpoint?
- Am I missing any extracted facts?

**Status**: Pre-1.0 local KG harness. JSONL snapshots, local MCP server, and full CLI query interface are implemented. Production storage and hosted services are roadmap items.

## Learning Paths

Choose your path based on what you want to do:

### Path 1: Using SuperContext (30-45 minutes)

You want to run SuperContext on your repos and ask questions about your code.

1. **Setup** → [setup-and-first-kg.md](./03-workflows/setup-and-first-kg.md) (15 min)
   - Install, initialize, build your first knowledge graph snapshot

2. **Query** → [query-your-repo.md](./03-workflows/query-your-repo.md) (20 min)
   - Learn 8 query tools with real examples on flask/react

3. **Understand Coverage** → [evaluate-coverage.md](./03-workflows/evaluate-coverage.md) (15 min)
   - Interpret coverage reports and identify gaps

4. **IDE Integration** → [mcp-integration.md](./02-core-features/mcp-integration.md) (10 min)
   - Register SuperContext as an MCP endpoint in Claude Code/Codex

### Path 2: Extending SuperContext (2-3 hours)

You want to add extractors, custom queries, or understand the architecture.

1. **Architecture** → [architecture-overview.md](./01-concepts/architecture-overview.md) (20 min)
   - Understand layers, data model, derivation tiers

2. **Knowledge Graphs** → [knowledge-graph.md](./02-core-features/knowledge-graph.md) (40 min)
   - Learn how graphs are built, deep dive into extractors

3. **Write an Extractor** → [extend-with-custom-extractor.md](./03-workflows/extend-with-custom-extractor.md) (90 min)
   - Build a working extractor with tests and integration

4. **Evidence & Verification** → [evidence-retrieval.md](./02-core-features/evidence-retrieval.md) (30 min)
   - Understand how facts are backed by code evidence

### Path 3: Quick Learning (10 minutes)

Just want an overview and quick reference?

1. **Concepts** → [what-is-supercontext.md](./01-concepts/what-is-supercontext.md) (5 min)
2. **Definitions** → [GLOSSARY.md](./GLOSSARY.md) (5 min)
3. **Explore specific feature docs** as needed

## Quick Navigation

| Document | Purpose | Read Time | Audience |
|----------|---------|-----------|----------|
| [what-is-supercontext.md](./01-concepts/what-is-supercontext.md) | Value prop, use cases, concepts intro | 5 min | All |
| [architecture-overview.md](./01-concepts/architecture-overview.md) | Layers, components, data flow | 15 min | Extenders |
| [knowledge-graph.md](./02-core-features/knowledge-graph.md) | What is a KG, building, extractors | 25 min | Users + Extenders |
| [querying.md](./02-core-features/querying.md) | 8 query tools, examples, custom queries | 30 min | Users |
| [coverage-metrics.md](./02-core-features/coverage-metrics.md) | Coverage concept, reports, contributing | 20 min | Users + Extenders |
| [mcp-integration.md](./02-core-features/mcp-integration.md) | MCP protocol, local server, registration | 15 min | Users |
| [evidence-retrieval.md](./02-core-features/evidence-retrieval.md) | Evidence backing, modes, verification | 20 min | Extenders |
| [setup-and-first-kg.md](./03-workflows/setup-and-first-kg.md) | Install → init → build snapshot | 15 min | Users |
| [query-your-repo.md](./03-workflows/query-your-repo.md) | Build → query → interpret | 30 min | Users |
| [evaluate-coverage.md](./03-workflows/evaluate-coverage.md) | Build → metrics → analyze gaps | 20 min | Users |
| [extend-with-custom-extractor.md](./03-workflows/extend-with-custom-extractor.md) | Write, test, integrate extractor | 90 min | Extenders |

## Glossary & Key Terms

Not sure what a term means? See [GLOSSARY.md](./GLOSSARY.md) for quick definitions and links to full explanations.

## Running Examples

All examples live in `examples/` and are ready to run:

```bash
# Build a knowledge graph on flask
bash examples/01-build/build-kg-single-repo.sh

# Run queries on the snapshot
bash examples/02-query/query-common-patterns.sh

# Full coverage pipeline
bash examples/03-coverage/coverage-full-pipeline.sh

# See examples/README.md for full listing
```

## What's Next?

After Getting Started:

- **Read the full architecture**: See `docs/adr/` for 11 Architecture Decision Records (ADRs)
- **Contribute**: Read `docs/contributing/` for development guidelines
- **Deep dives**: Research notes and recommendations in `docs/`
- **Evaluation**: Query artifacts and acceptance corpus in `docs/evaluation/`

## Quick Commands Reference

Build a knowledge graph from a repository:
```bash
supercontext-build-kg --repo /path/to/repo --out ./data/kg_runs/example
```

Query a snapshot:
```bash
supercontext-query-kg --snapshot ./data/kg_runs/example find-callers my_function --limit 5
```

Start a local MCP server:
```bash
supercontext-init --repo /path/to/repo --serve
```

Generate coverage report:
```bash
supercontext-coverage-report --snapshot ./data/kg_runs/example --out ./docs/evaluation/runs/example
```

## Questions or Issues?

- **Getting Started question?** Check [GLOSSARY.md](./GLOSSARY.md) or re-read the relevant workflow doc
- **Found a bug?** Open an issue in the GitHub repo
- **Want to contribute?** See `docs/contributing/`

---

*Last updated: 2026-05-25*
```

- [ ] **Step 2: Verify README renders correctly**

```bash
# Just check file exists and is readable
head -50 docs/getting-started/README.md
```

- [ ] **Step 3: Commit**

```bash
git add docs/getting-started/README.md
git commit -m "docs: Add Getting Started README with learning paths"
```

---

### Task 3: Create GLOSSARY.md

**Files:**
- Create: `docs/getting-started/GLOSSARY.md`

- [ ] **Step 1: Write GLOSSARY.md with 25 key terms**

```markdown
# SuperContext Glossary

Quick reference for key terms. For detailed explanations, see the linked documents.

## Core Concepts

**Artifact** — Any extracted fact about code. Entities, facts, and evidence are artifacts.

**Canonical** — Marked as authoritative and trusted. Opposite of "candidate." See [evidence-retrieval.md](./02-core-features/evidence-retrieval.md).

**Candidate** — Marked as inferred or uncertain, pending promotion to canonical. See ADR-0004.

**Coverage** — Measure of extraction completeness: entity discovery rate, relation types found, derivation tier distribution. See [coverage-metrics.md](./02-core-features/coverage-metrics.md).

**Derivation Class** — How a fact was determined. Tiers: authoritative_declared > manual_override > deterministic_static > runtime_observed > inferred_llm. See ADR-0006.

**Entity** — A unit of code: function, module, service, endpoint, event channel. See [knowledge-graph.md](./02-core-features/knowledge-graph.md).

**Evidence** — Bytecode backing for a fact. Includes commit, file, line numbers. Mode A (commit-pinned) or Mode B (selective retrieval). See [evidence-retrieval.md](./02-core-features/evidence-retrieval.md).

**Extractor** — Code that walks a codebase (AST, config, etc.) and emits entities/facts/evidence. Python and TypeScript extractors implemented. See [knowledge-graph.md](./02-core-features/knowledge-graph.md) §"Writing a Custom Extractor".

**Fact** — A relationship between entities: A calls B, A imports B, A hosts endpoint E. See [knowledge-graph.md](./02-core-features/knowledge-graph.md).

**Knowledge Graph (KG)** — Typed directed graph of entities and facts extracted from code. Stored as JSONL snapshot. See [knowledge-graph.md](./02-core-features/knowledge-graph.md).

**MCP** — Model Context Protocol. Standard interface for AI agents to query SuperContext. See [mcp-integration.md](./02-core-features/mcp-integration.md).

**Ontology** — The schema: 10 entity types + 15 relation types. Tenant-scoped. See ADR-0006.

**Snapshot** — A frozen KG: 5 JSONL files (entities, facts, evidence, coverage, manifest) from one build run. Immutable. See [knowledge-graph.md](./02-core-features/knowledge-graph.md).

**Tenant** — An organization or project scope. Graph facts are tenant-isolated. Default is "default".

**URN** — Unique Resource Name. Identifier for an entity in the graph.

## Query & Analysis

**Blast Radius** — Transitive impact of changing a symbol: all downstream callers, dependents, consumers. Query: `blast-radius <symbol> --depth N`. See [querying.md](./02-core-features/querying.md).

**Call Graph** — Directed graph of function calls. Building blocks: `find-callers`, `find-callees`. See [querying.md](./02-core-features/querying.md).

**Cross-Repo Link** — Import or call between services. Captured by multi-repo snapshots. See [setup-and-first-kg.md](./03-workflows/setup-and-first-kg.md).

**Query** — A question about the KG. 8 standard tools available. See [querying.md](./02-core-features/querying.md).

## Implementation & Extension

**Bytes Ref** — Citation of exact code: {repo, commit_sha, path, line_start, line_end}. See [evidence-retrieval.md](./02-core-features/evidence-retrieval.md).

**Derivation** — How a fact was inferred. Determines canonical status. See ADR-0006.

**Fixture** — Minimal test repository used for testing extractors. See [extend-with-custom-extractor.md](./03-workflows/extend-with-custom-extractor.md).

**JSONL** — JSON Lines: one JSON object per line, no nesting. Used for snapshot storage. See [knowledge-graph.md](./02-core-features/knowledge-graph.md).

**Mode A / Mode B** — Evidence retrieval modes. Mode A: commit-pinned direct retrieval (always available). Mode B: selective ladder (ripgrep → AST → Claude). See [evidence-retrieval.md](./02-core-features/evidence-retrieval.md).

## Architecture (See ADRs for full details)

**ADR-0001** — Runtime is Claude Agent SDK.

**ADR-0002** — Public protocol is MCP with 8 tools.

**ADR-0003** — Storage is PostgreSQL + Apache AGE (future; currently JSONL).

**ADR-0004** — Two-tier graph: canonical + candidate sidecar.

**ADR-0005** — Evidence is dual-mode: Mode A (commit-pinned) + Mode B (selective).

**ADR-0006** — Ontology: 10 entity types + 15 relation types, tenant-scoped.

---

## Quick Command Cheatsheet

| Task | Command |
|------|---------|
| Build KG | `supercontext-build-kg --repo <path> --out <dir>` |
| Query KG | `supercontext-query-kg --snapshot <dir> find-callers <symbol>` |
| Coverage | `supercontext-coverage-metrics --snapshot <dir>` |
| Start MCP | `supercontext-init --repo <path> --serve` |
| List queries | `supercontext-query-kg --help` |

---

*For detailed explanations, see the appropriate document in Getting Started.*
```

- [ ] **Step 2: Verify GLOSSARY structure**

```bash
grep "^##\|^###\|^\*\*" docs/getting-started/GLOSSARY.md | head -40
```

Expected: Shows section headers and term definitions

- [ ] **Step 3: Commit**

```bash
git add docs/getting-started/GLOSSARY.md
git commit -m "docs: Add Getting Started glossary with 25 key terms"
```

---

## Phase 2: Concept Docs (2-3 hours)

### Task 4: Create what-is-supercontext.md

**Files:**
- Create: `docs/getting-started/01-concepts/what-is-supercontext.md`

- [ ] **Step 1: Write what-is-supercontext.md**

```markdown
# What is SuperContext?

*5-minute read. For details, see [architecture-overview.md](./architecture-overview.md).*

## The Problem

You're building a microservice architecture: 50+ services, each depending on others. An AI agent (or a human engineer) wants to:

- Fix a bug in service A
- Add a feature to service B
- Refactor a shared library that 20 services use
- Change an API endpoint that event handlers subscribe to

**Without SuperContext**, the question "what will break?" requires:
1. Manually searching code for callers
2. Checking the dependency graph (if it exists)
3. Hope you found everything
4. Deploy and debug in production

**With SuperContext**, you ask: "What breaks if I change this?" and get a complete, typed answer.

## How It Works: The Three Layers

### Layer 1: Extraction

Extractors walk your code (using AST parsing, config inspection, etc.) and pull out facts:
- Function definitions and calls
- Module imports
- API endpoints
- Event subscriptions
- Service-to-service dependencies

These facts are stored as a **knowledge graph**: a typed, directed graph of entities (functions, services, endpoints) and relations (calls, imports, hosts).

### Layer 2: Querying

Eight standard query tools let you ask questions:
- Who calls this function?
- What will break if I change this? (transitive impact)
- Which services depend on this endpoint?
- What entities are missing from my graph? (coverage)

### Layer 3: Integration

SuperContext exposes its API via **MCP** (Model Context Protocol), so AI agents in your IDE can ask these questions automatically.

```
Repo Code
   ↓
Extractors (AST, Config Parsing)
   ↓
Knowledge Graph (Entities + Facts + Evidence)
   ↓
Query Engine (8 standard tools)
   ↓
MCP Server (IDE Integration)
```

## Key Concepts

### Entities

An **entity** is a unit of code:
- `function`: `my_module.authenticate()`
- `module`: `my_package.utils`
- `service`: `auth-service`
- `endpoint`: `/api/v1/users`
- `event_channel`: `kafka:user-events`

### Facts

A **fact** is a relationship:
- Entity A calls Entity B: `authenticate()` calls `hash_password()`
- Entity A imports Entity B: `module_a` imports `module_b`
- Service A hosts Entity B: `auth-service` hosts endpoint `/api/login`

### Evidence

Every fact is backed by **evidence**: the exact code that proves it.

Evidence includes:
- Repository name
- Commit hash (for immutability)
- File path
- Line numbers (start and end)

This means facts are verifiable and traceable.

### Coverage

**Coverage** tells you what's missing. For each entity type and relation type, we measure:
- How many did we find?
- How many should we have found?
- Where are the gaps?

This guides where to write new extractors or improve existing ones.

## Why SuperContext Matters

### For Engineers

- **Change safety**: Before you deploy, know the full impact of your change.
- **Refactoring confidence**: Renaming a function? SuperContext finds all callers.
- **Onboarding**: New engineer joining? Query the graph to understand dependencies.

### For AI Agents

- **Context for code generation**: When the agent edits code, it knows what will break.
- **Safer refactoring**: Agents can verify their changes won't break dependents.
- **Dependency-aware features**: Add a parameter? The agent can find all call sites.

## Current Status

**What's Implemented**:
- ✅ Local KG harness (JSONL storage)
- ✅ Python and TypeScript/JavaScript extractors
- ✅ 8 standard query tools
- ✅ Local MCP server
- ✅ Coverage metrics pipeline
- ✅ Evidence retrieval (both modes)

**What's Coming**:
- 🔨 Production storage (PostgreSQL + Apache AGE)
- 🔨 Hosted service + authentication
- 🔨 GitHub PR bot integration
- 🔨 Broader language support
- 🔨 Expanded extractor coverage

**Not Included**:
- Real-time updates (snapshots are immutable)
- IDE plugins (uses MCP instead)
- Visual graph UI (CLI-first)

## What's Next?

Ready to get started? Choose a path:

- **Using SuperContext?** → [setup-and-first-kg.md](../03-workflows/setup-and-first-kg.md)
- **Understanding the architecture?** → [architecture-overview.md](./architecture-overview.md)
- **Extending SuperContext?** → [knowledge-graph.md](../02-core-features/knowledge-graph.md)

---

*Last updated: 2026-05-25*
```

- [ ] **Step 2: Verify content**

```bash
wc -w docs/getting-started/01-concepts/what-is-supercontext.md
```

Expected: ~1000-1200 words

- [ ] **Step 3: Commit**

```bash
git add docs/getting-started/01-concepts/what-is-supercontext.md
git commit -m "docs: Add what-is-supercontext concept doc"
```

---

### Task 5: Create architecture-overview.md

**Files:**
- Create: `docs/getting-started/01-concepts/architecture-overview.md`

- [ ] **Step 1: Write architecture-overview.md (15-20 min content)**

```markdown
# Architecture Overview

*15-minute read. For implementation details, see the ADRs.*

## The Three Layers

SuperContext is built in three layers, each with a clear responsibility.

### Layer 1: Ingestion (Extract Code Facts)

**Job**: Read a codebase and emit facts.

**Components**:
- **Extractors** (language-specific): AST walkers, config parsers
  - Python extractor: Finds functions, imports, class methods, decorators
  - TypeScript extractor: Finds functions, imports, class methods, endpoints
- **Evidence gatherer**: Records exact code locations (commit, file, lines)

**Input**: Source code repository

**Output**: Three types of artifacts:
1. **Entities**: Functions, modules, services, endpoints, event channels
2. **Facts**: "A calls B", "A imports B", "A hosts endpoint X"
3. **Evidence**: Exact code location proving each fact

**Current**: JSONL snapshot (5 files). Future: PostgreSQL + Apache AGE.

**Language Coverage**:
- ✅ Python (good coverage)
- ✅ TypeScript / JavaScript (good coverage)
- ❌ Go, Rust, Java, etc. (not yet)

### Layer 2: Storage (The Knowledge Graph)

**Job**: Store entities, facts, and evidence in a queryable format.

**Current Structure** (JSONL snapshot):
```
entities.jsonl       # One entity per line
facts.jsonl          # One fact per line
evidence.jsonl       # One evidence record per line
coverage.jsonl       # Extraction completeness metrics
manifest.json        # Snapshot metadata
```

**Key Design Decision: Derivation Tiers**

Not all facts are equally trustworthy. A function call found by static analysis is more trustworthy than one inferred by an LLM.

The ontology defines five tiers (best → worst):

1. **authoritative_declared**: Explicit declaration (e.g., config file says "this service is X")
2. **manual_override**: Human-reviewed and approved
3. **deterministic_static**: Derived from AST / static analysis
4. **runtime_observed**: From runtime data (logs, traces)
5. **inferred_llm**: Inferred by LLM (least trustworthy)

**Canonical vs. Candidate**

When a fact is extracted:
- **Canonical facts**: Trusted, included in query results by default
- **Candidate facts**: Inferred/uncertain, promoted to canonical when verified

Most extracted facts start as canonical (tier 3+). Promotion rules are in ADR-0006.

### Layer 3: Querying (Answer Questions)

**Job**: Answer questions about the knowledge graph.

**Eight Standard Tools**:

| Tool | Question | Use Case |
|------|----------|----------|
| `find-callers` | Who calls this function? | Find impact of a change |
| `find-callees` | What does this call? | Understand dependencies |
| `blast-radius` | Full transitive impact? | Is this refactoring safe? |
| `search-services` | Find services by name | Onboarding, discovery |
| `get-service-brief` | What does this service do? | Understand architecture |
| `get-event-consumers` | Who subscribed to this event? | Event-driven impact |
| `get-event-producers` | Who publishes this event? | Understand data flow |
| `deploy-blockers-for` | What prevents deployment? | Pre-deployment checks |

**Query Engine**:
1. Parse the query
2. Filter facts (apply derivation tier filters)
3. Traverse the graph (follow relations)
4. Return results (entities + evidence links)

---

## Data Model

Every fact has this shape:

```json
{
  "id": "uuid",
  "upstream": "entity-urn",
  "relation_type": "CALLS",
  "downstream": "entity-urn",
  "derivation_class": "deterministic_static",
  "canonical_status": "canonical",
  "evidence": {
    "repo": "flask",
    "commit_sha": "abc123...",
    "path": "flask/app.py",
    "line_start": 42,
    "line_end": 45
  }
}
```

**Key fields**:
- `upstream` / `downstream`: URNs (Unique Resource Names) of entities
- `relation_type`: Type of relationship (from ontology)
- `derivation_class`: How confident we are
- `evidence`: Exact code location (for verification)

---

## Ontology (Schema)

The ontology defines what entities and relations are allowed. See ADR-0006 for full details.

### Entity Types (10)
- `CodeFunction`: A function/method
- `CodeModule`: A file/module
- `Service`: A deployable service
- `Endpoint`: An API endpoint
- `EventChannel`: A message queue/topic
- ... (5 more, defined in ADR-0006)

### Relation Types (15)
- `CALLS`: Function A calls function B
- `IMPORTS`: Module A imports module B
- `HOSTS`: Service A hosts endpoint E
- `PUBLISHES`: Service A publishes to channel C
- `SUBSCRIBES`: Service A subscribes to channel C
- ... (10 more, defined in ADR-0006)

---

## Evidence: Two Modes

Every fact must be verifiable. SuperContext supports two modes:

### Mode A: Commit-Pinned (Always Available)

- Retrieve code from git at exact commit
- Immutable (commit hash never changes)
- Costly (requires git operations)
- Use for: Facts that must be verified, compliance audits

### Mode B: Selective Ladder (On-Demand)

- Recompute fact using ladder: ripgrep → AST → Claude
- Flexible (works with latest code)
- Expensive (multiple tools, LLM costs)
- Use for: Dynamic facts, time-sensitive information

See [evidence-retrieval.md](../02-core-features/evidence-retrieval.md) for details.

---

## Flow Diagram

```
┌─────────────────────────────────────────────────────────┐
│ 1. INGESTION: Extract code facts                        │
├─────────────────────────────────────────────────────────┤
│ Repo Code (Python/TS) → Extractors → Parse AST → Emit  │
│                                                          │
│ Output: entities.jsonl, facts.jsonl, evidence.jsonl     │
└─────────────────────────────────────────────────────────┘
                         ↓
┌─────────────────────────────────────────────────────────┐
│ 2. STORAGE: Build knowledge graph                       │
├─────────────────────────────────────────────────────────┤
│ JSONL files + Manifest = Immutable Snapshot             │
│                                                          │
│ Current: JSONL                                           │
│ Future: PostgreSQL + Apache AGE                          │
└─────────────────────────────────────────────────────────┘
                         ↓
┌─────────────────────────────────────────────────────────┐
│ 3. QUERYING: Answer questions                           │
├─────────────────────────────────────────────────────────┤
│ 8 standard tools + custom queries                       │
│ Query → Traverse graph → Return results + evidence      │
└─────────────────────────────────────────────────────────┘
                         ↓
┌─────────────────────────────────────────────────────────┐
│ 4. INTEGRATION: AI agents use results                   │
├─────────────────────────────────────────────────────────┤
│ MCP Server → Agent Context → Safer code generation     │
└─────────────────────────────────────────────────────────┘
```

---

## Design Principles

1. **Deterministic first**: Prefer static analysis over heuristics
2. **Evidence-backed**: Every fact must be traceable to code
3. **Tenant-scoped**: Multi-tenant isolation built in
4. **Queryable**: All facts must be efficiently queryable
5. **Extensible**: New extractors, relations, and queries can be added

---

## How This Relates to ADRs

- **ADR-0001**: Runtime is Claude Agent SDK (not shown in this doc, backend detail)
- **ADR-0002**: Public API is MCP (Layer 3)
- **ADR-0003**: Storage will be PostgreSQL + AGE (Layer 2 future)
- **ADR-0004**: Canonical + candidate tiers (Layer 2)
- **ADR-0005**: Evidence modes A & B (Layer 2)
- **ADR-0006**: Ontology schema (Layers 1 & 2)

---

## What's Next?

- **Understand extractors?** → [knowledge-graph.md](../02-core-features/knowledge-graph.md)
- **Learn to query?** → [querying.md](../02-core-features/querying.md)
- **Read full architecture?** → `docs/adr/` (11 ADRs in order)

---

*Last updated: 2026-05-25*
```

- [ ] **Step 2: Verify content**

```bash
wc -w docs/getting-started/01-concepts/architecture-overview.md
```

Expected: ~1400-1600 words

- [ ] **Step 3: Commit**

```bash
git add docs/getting-started/01-concepts/architecture-overview.md
git commit -m "docs: Add architecture-overview concept doc"
```

---

## Phase 3: Core Feature Docs (10-12 hours)

### Task 6: Create knowledge-graph.md (comprehensive feature doc)

**Files:**
- Create: `docs/getting-started/02-core-features/knowledge-graph.md`

- [ ] **Step 1: Write knowledge-graph.md Part 1: What is a KG?**

```markdown
# Knowledge Graphs

*25-minute read. This doc covers what KGs are, how we build them, how to use them, and how to extend them with custom extractors.*

## Part 1: What is a Knowledge Graph?

A **knowledge graph** is a directed graph of facts extracted from code.

**Nodes** (entities):
- Functions: `authenticate()`, `hash_password()`
- Modules: `my_app`, `utils`, `database`
- Services: `auth-service`, `api-gateway`
- Endpoints: `/api/login`, `/api/users`
- Event channels: `user-events`, `audit-log`

**Edges** (facts/relations):
- `authenticate()` **calls** `hash_password()`
- `my_app` **imports** `utils`
- `auth-service` **hosts** `/api/login`
- `user-service` **publishes to** `user-events`

**Why it matters**:

1. **Change safety**: Query the graph to find all code that might break
2. **Dependency analysis**: Understand which services depend on each other
3. **Coverage understanding**: See what facts you're missing
4. **Onboarding**: New engineer? Query to understand architecture

### Example: Flask Application

A simple Flask app:

```python
from flask import Flask
from my_app.auth import authenticate
from my_app.database import get_user

app = Flask(__name__)

@app.route('/api/users/<id>')
def get_user_endpoint(id):
    user_data = authenticate()  # Calls authenticate
    return get_user(id)
```

The knowledge graph captures:

| Entity | Entity | Relation | Evidence |
|--------|--------|----------|----------|
| `get_user_endpoint()` | `authenticate()` | CALLS | line 9 |
| `get_user_endpoint()` | `get_user()` | CALLS | line 10 |
| `flask_app` | `my_app.auth` | IMPORTS | line 2 |
| `flask_app` | `my_app.database` | IMPORTS | line 3 |
| `/api/users/<id>` | `get_user_endpoint()` | IMPLEMENTS | line 7 |

Now you can query:
- "Who calls `authenticate()`?" → `get_user_endpoint()`
- "What will break if I change `authenticate()`?" → `/api/users/<id>` endpoint
- "Which functions does the `/api/users/<id>` endpoint use?" → Both

---

## Part 2: How SuperContext Builds a KG

### Step 1: Extraction

An **extractor** walks code and emits facts. SuperContext has extractors for Python and TypeScript.

**Python Extractor Flow**:
1. Parse all `.py` files in the repo
2. Build an AST (Abstract Syntax Tree)
3. Walk the AST looking for:
   - Function definitions (entities)
   - Function calls (facts)
   - Import statements (facts)
   - Decorators (special facts like endpoints)
4. Emit (entity, fact, evidence) tuples
5. Store as JSONL snapshot

**Example**: Python extractor finds `def authenticate():` and emits:
```json
{"type": "Entity", "urn": "my_app.auth:authenticate", "kind": "CodeFunction", ...}
```

Then finds `authenticate()` being called and emits:
```json
{"type": "Fact", "upstream": "...:get_user_endpoint", "relation_type": "CALLS", "downstream": "...:authenticate", ...}
```

### Step 2: Storage

Facts are stored in **JSONL** format (one JSON object per line, no nesting). A snapshot is 5 files:

**entities.jsonl** — All entities:
```json
{"id": "uuid-1", "urn": "flask_app:app", "kind": "CodeModule", "tenant_id": "default", "repo": "flask", ...}
{"id": "uuid-2", "urn": "flask_app:authenticate", "kind": "CodeFunction", "tenant_id": "default", ...}
```

**facts.jsonl** — All relationships:
```json
{"id": "uuid-101", "upstream": "...:authenticate", "relation_type": "CALLS", "downstream": "...:hash_password", "derivation_class": "deterministic_static", ...}
{"id": "uuid-102", "upstream": "...:my_app", "relation_type": "IMPORTS", "downstream": "...:utils", ...}
```

**evidence.jsonl** — Code citations:
```json
{"id": "uuid-201", "fact_id": "uuid-101", "repo": "flask", "commit_sha": "abc123...", "path": "app.py", "line_start": 9, "line_end": 9, ...}
```

**coverage.jsonl** — Extraction metrics:
```json
{"entity_type": "CodeFunction", "count_found": 342, "coverage_percent": 87.5, ...}
```

**manifest.json** — Metadata:
```json
{"snapshot_id": "uuid-snap", "repo": "flask", "commit_sha": "abc123...", "generated_at": "2026-05-25", ...}
```

### Step 3: Snapshot is Immutable

Once built, a snapshot never changes. Queries against it always return the same results.

This is intentional: every snapshot is pinned to a git commit, so it's reproducible and auditable.

---

## Part 3: Using the KG

### Building a KG

Build from a single repo:
```bash
supercontext-build-kg --repo /path/to/flask --out ./data/kg_runs/flask_example
```

Build from multiple repos (when they depend on each other):
```bash
supercontext-build-multi-kg \
  --repo /path/to/service-a \
  --repo /path/to/service-b \
  --out ./data/kg_runs/org_example
```

Both write to the `--out` directory:
```
./data/kg_runs/flask_example/
├── entities.jsonl
├── facts.jsonl
├── evidence.jsonl
├── coverage.jsonl
└── manifest.json
```

### Querying the KG

See [querying.md](./querying.md) for the full 8-tool reference. Quick example:

```bash
# Find all functions that call authenticate()
supercontext-query-kg --snapshot ./data/kg_runs/flask_example \
  find-callers authenticate --limit 10

# Output: List of all callers with evidence
```

### Understanding Coverage

Coverage tells you extraction quality:

```bash
supercontext-coverage-metrics --snapshot ./data/kg_runs/flask_example
```

Output shows:
- Entity coverage by type (e.g., "found 87% of functions")
- Relation types coverage (e.g., "found 45% of imports")
- Derivation tier distribution (mostly deterministic_static, some inferred_llm)

See [coverage-metrics.md](./coverage-metrics.md) for full details.

---

## Part 4: Writing a Custom Extractor

If coverage gaps exist (e.g., "we're not finding Flask routes"), you can write an extractor.

### When to Write an Extractor

1. **Coverage gap identified**: A query returns incomplete results
2. **Pattern is common**: The fact type appears many times in your codebase
3. **Pattern is extractable**: Can be found via AST, config, or regex

**Examples of extractable patterns**:
- ✅ Flask `@app.route()` decorators → Endpoints
- ✅ Celery `@app.task()` decorators → Background tasks
- ✅ Config file `services:` section → Services
- ✅ Environment variables → Configuration facts
- ❌ Comments describing relationships (too ambiguous)

### Anatomy of an Extractor

Every extractor has this structure:

```python
from __future__ import annotations
import ast
from dataclasses import dataclass

@dataclass(frozen=True)
class FlaskRouteExtractor:
    """Extract Flask routes as Endpoint entities."""
    
    def extract(self, repo_path: str) -> tuple[list, list, list]:
        """
        Returns: (entities, facts, evidence)
        """
        entities = []
        facts = []
        evidence = []
        
        # 1. Walk code
        # 2. Find Flask decorators
        # 3. Emit entities/facts/evidence
        
        return entities, facts, evidence
```

### Step-by-Step Example: Flask Routes

**Goal**: Extract Flask `@app.route()` decorators as Endpoint entities.

**Step 1: Find route decorators**

```python
import ast

class RouteVisitor(ast.NodeVisitor):
    def visit_FunctionDef(self, node):
        for decorator in node.decorator_list:
            # Check if it's app.route(...)
            if (isinstance(decorator, ast.Call) and 
                isinstance(decorator.func, ast.Attribute) and
                decorator.func.attr == 'route'):
                # Extract the path from @app.route('/api/users')
                if decorator.args:
                    path_node = decorator.args[0]
                    if isinstance(path_node, ast.Constant):
                        path = path_node.value
                        # Emit entity + fact
                        yield (node.name, path)
        self.generic_visit(node)
```

**Step 2: Emit entities and facts**

```python
entities.append({
    "urn": f"endpoints:{path}",
    "kind": "Endpoint",
    "name": path,
    "tenant_id": "default",
    "repo": repo,
})

facts.append({
    "upstream": f"services:{service_name}",
    "relation_type": "HOSTS",
    "downstream": f"endpoints:{path}",
    "derivation_class": "deterministic_static",
})

evidence.append({
    "fact_id": fact_id,
    "repo": repo,
    "commit_sha": commit_sha,
    "path": file_path,
    "line_start": node.lineno,
    "line_end": node.end_lineno,
})
```

**Step 3: Full extractor (simplified)**

```python
class FlaskRouteExtractor:
    def extract(self, repo_path: str) -> tuple[list, list, list]:
        entities, facts, evidence = [], [], []
        
        # Walk all Python files
        for root, dirs, files in os.walk(repo_path):
            for file in files:
                if file.endswith('.py'):
                    filepath = os.path.join(root, file)
                    with open(filepath) as f:
                        tree = ast.parse(f.read())
                    
                    visitor = RouteVisitor()
                    for func_name, path in visitor.visit(tree):
                        # Emit entity
                        entity = {...}
                        entities.append(entity)
                        
                        # Emit fact
                        fact = {...}
                        facts.append(fact)
                        
                        # Emit evidence
                        ev = {...}
                        evidence.append(ev)
        
        return entities, facts, evidence
```

### Testing Your Extractor

Write unit tests with fixtures:

```python
import unittest

class TestFlaskRouteExtractor(unittest.TestCase):
    def setUp(self):
        self.fixture_path = "./tests/fixtures/flask_app"
    
    def test_extracts_routes(self):
        extractor = FlaskRouteExtractor()
        entities, facts, evidence = extractor.extract(self.fixture_path)
        
        # Should find /api/users and /api/login
        paths = [e['name'] for e in entities if e['kind'] == 'Endpoint']
        assert '/api/users' in paths
        assert '/api/login' in paths
    
    def test_fact_links_service_to_endpoint(self):
        extractor = FlaskRouteExtractor()
        entities, facts, evidence = extractor.extract(self.fixture_path)
        
        # Should have HOSTS facts
        hosts_facts = [f for f in facts if f['relation_type'] == 'HOSTS']
        assert len(hosts_facts) > 0
```

### Integration Checklist

Before committing:

- [ ] Extractor finds all expected patterns (positive test)
- [ ] Extractor doesn't find false positives (negative test)
- [ ] Edge cases handled (malformed input, nested patterns, etc.)
- [ ] Evidence (`bytes_ref`) is correct (commit, path, lines)
- [ ] No hardcoded repo/service names (make it general)
- [ ] Extractor is registered in `source/kg/extractors/__init__.py`
- [ ] Run full `build_kg` and verify coverage improved

---

## Further Reading

- **How to query the KG**: [querying.md](./querying.md)
- **Understand coverage**: [coverage-metrics.md](./coverage-metrics.md)
- **Evidence backing**: [evidence-retrieval.md](./evidence-retrieval.md)
- **Full architecture**: ADR-0006 (Ontology), ADR-0005 (Evidence)
- **Build your first KG**: [setup-and-first-kg.md](../03-workflows/setup-and-first-kg.md)

---

*Last updated: 2026-05-25*
```

- [ ] **Step 2: Verify content and structure**

```bash
wc -w docs/getting-started/02-core-features/knowledge-graph.md
head -100 docs/getting-started/02-core-features/knowledge-graph.md
```

Expected: ~2400-2600 words, shows 4 clear parts

- [ ] **Step 3: Commit**

```bash
git add docs/getting-started/02-core-features/knowledge-graph.md
git commit -m "docs: Add knowledge-graph feature doc (comprehensive)"
```

---

### Task 7: Create querying.md

**Files:**
- Create: `docs/getting-started/02-core-features/querying.md`

- [ ] **Step 1: Write querying.md (full 8-tool reference with real examples)**

Due to length constraints, this task focuses on creating a complete querying reference. The document should:

1. **Part 1: Query Basics** (400 words)
   - What queries are
   - Command syntax: `supercontext-query-kg --snapshot <path> <query> [args]`
   - Output formats (table, JSON, CSV)

2. **Part 2: The 8 Tools** (1800 words)
   - For each tool: What it does, syntax, real example on flask/react, sample output, how to read it

3. **Part 3: Custom Queries** (500 words)
   - Query language overview
   - How to write custom queries
   - Example custom query

4. **Performance & Tips** (300 words)

**Content outline:**

```markdown
# Querying the Knowledge Graph

*30-minute read. Master the 8 standard query tools.*

[... Part 1: Query Basics ...]

## Part 2: The 8 Query Tools

### Tool 1: find-callers

**What it does**: Find all functions that call a given function.

**Syntax**:
```bash
supercontext-query-kg --snapshot <path> find-callers <symbol> [--limit N]
```

**Example on Flask**:
```bash
supercontext-query-kg --snapshot ./data/kg_runs/flask find-callers authenticate --limit 5
```

**Sample output**:
```
Found 3 callers of authenticate():
  1. get_user_endpoint() at app.py:9
  2. login() at auth.py:42
  3. verify_token() at middleware.py:15
```

**How to read it**: Each caller is a function. The file and line show where it calls your target.

**Use case**: "I'm refactoring `authenticate()`. What will break?"

[... repeat for find-callees, blast-radius, etc. ...]

---

### Tool 8: deploy-blockers-for

**What it does**: Find conditions that prevent a service from deploying.

[... full example ...]

---

## Part 3: Writing Custom Queries

[... Custom query language overview ...]

---

## Performance Tips

[... Depth limits, caching, result limits ...]
```

- [ ] **Step 1: Write full querying.md**

Write querying.md with all 8 tools fully documented. Estimated content:

```markdown
# Querying the Knowledge Graph

*30-minute read.*

## Part 1: Query Basics

A **query** is a question about your KG. SuperContext provides 8 standard query tools, plus the ability to write custom queries.

### Command Syntax

```bash
supercontext-query-kg \
  --snapshot <path-to-snapshot> \
  <query-name> \
  [query-specific-args] \
  [--limit N] \
  [--format json|table|csv]
```

**Arguments**:
- `--snapshot`: Path to snapshot directory
- `<query-name>`: One of 8 tools or custom query
- `[query-specific-args]`: Symbol name, service name, etc.
- `--limit`: Max results to return (default varies by query)
- `--format`: Output format (default: table, human-readable)

### Output Formats

**Table** (default, human-readable):
```
Found 3 callers of authenticate():
  1. get_user_endpoint() at app.py:9
  2. login() at auth.py:42
```

**JSON** (for programmatic use):
```json
{
  "query": "find-callers",
  "symbol": "authenticate",
  "results": [
    {"name": "get_user_endpoint", "location": "app.py:9", ...},
    {"name": "login", "location": "auth.py:42", ...}
  ]
}
```

**CSV** (for spreadsheets):
```
name,location,type
get_user_endpoint,app.py:9,function
login,auth.py:42,function
```

---

## Part 2: The 8 Query Tools

### 1. find-callers

**What it does**: Find all functions that call a given function.

**Syntax**:
```bash
supercontext-query-kg --snapshot <path> find-callers <symbol> --limit N
```

**Arguments**:
- `<symbol>`: Function name (e.g., `authenticate`, `my_module.authenticate`)
- `--limit`: Max results (default 50)

**Example**:
```bash
supercontext-query-kg --snapshot ./data/kg_runs/flask find-callers authenticate --limit 5
```

**Output**:
```
Found 3 callers of authenticate():
  1. get_user_endpoint() | app.py:9-10 | flask repo
  2. login() | auth.py:42-44 | flask repo
  3. verify_token() | middleware.py:15-18 | flask repo

Evidence available. Run: supercontext-query-kg --snapshot ... evidence <fact-id>
```

**How to read it**: 
- Each row is a function that calls your target
- File and line numbers show where it calls your target
- "Evidence available" means you can verify the bytecode

**Use case**: "I want to rename `authenticate()`. What will break?"

**Notes**:
- Only finds static calls (not `getattr()` or dynamic dispatch)
- Covers same-repo and cross-repo calls
- Tip: Add `--format json` to get structured output for scripting

---

### 2. find-callees

**What it does**: Find all functions that a given function calls.

**Syntax**:
```bash
supercontext-query-kg --snapshot <path> find-callees <symbol> --limit N
```

**Example**:
```bash
supercontext-query-kg --snapshot ./data/kg_runs/flask find-callees get_user_endpoint --limit 10
```

**Output**:
```
get_user_endpoint() calls 2 functions:
  1. authenticate() | auth.py:5-15 | flask repo
  2. get_user() | database.py:30-40 | flask repo
```

**Use case**: "What does this endpoint depend on?"

---

### 3. blast-radius

**What it does**: Full transitive impact. If I change this function, what else breaks (all downstream callers, recursively)?

**Syntax**:
```bash
supercontext-query-kg --snapshot <path> blast-radius <symbol> --depth N --limit M
```

**Arguments**:
- `<symbol>`: Starting function
- `--depth`: How many levels to traverse (default 2)
- `--limit`: Max results per level (default 100)

**Example**:
```bash
supercontext-query-kg --snapshot ./data/kg_runs/flask blast-radius authenticate --depth 3 --limit 50
```

**Output**:
```
Blast radius of authenticate() [depth 3, 12 total affected]:
  
Level 1 (direct callers):
  1. get_user_endpoint() | app.py:9
  2. login() | auth.py:42
  
Level 2 (callers of those):
  1. request_handler() | flask.py:100
  2. middleware.process() | middleware.py:20
  
Level 3 (callers of those):
  1. app.dispatch() | flask.py:200
  
Total: 5 direct + 4 indirect + 3 deeper
```

**Use case**: "Is this refactoring safe? What's the full impact?"

**Notes**:
- Searches transitively (not just 1 level)
- Can be expensive on large graphs (use `--depth` to limit)
- Shows you the entire chain

---

### 4. search-services

**What it does**: Find services by name or pattern.

**Syntax**:
```bash
supercontext-query-kg --snapshot <path> search-services <name-pattern> --limit N
```

**Example**:
```bash
supercontext-query-kg --snapshot ./data/kg_runs/microservices search-services auth --limit 10
```

**Output**:
```
Found 4 services matching "auth":
  1. auth-service | microservice-1.yaml
  2. auth-gateway | microservice-2.yaml
  3. oauth-server | microservice-3.yaml
  4. user-authenticator | microservice-4.yaml
```

**Use case**: "What services relate to authentication?"

---

### 5. get-service-brief

**What it does**: Get a summary of a service: what it does, what it hosts, what it depends on.

**Syntax**:
```bash
supercontext-query-kg --snapshot <path> get-service-brief <service-name>
```

**Example**:
```bash
supercontext-query-kg --snapshot ./data/kg_runs/microservices get-service-brief auth-service
```

**Output**:
```
auth-service
=============
Description: Authentication and authorization microservice

Endpoints (3):
  /api/v1/auth/login
  /api/v1/auth/verify
  /api/v1/auth/logout

Dependencies:
  - imports: common-utils
  - calls: user-database-service
  - subscribes to: user-events

Callers:
  - api-gateway calls 2 endpoints
  - middleware depends on verify endpoint
```

**Use case**: "What does this service do? Who depends on it?"

---

### 6. get-event-consumers

**What it does**: Find all services that subscribe to a given event channel.

**Syntax**:
```bash
supercontext-query-kg --snapshot <path> get-event-consumers <channel-name> --limit N
```

**Example**:
```bash
supercontext-query-kg --snapshot ./data/kg_runs/microservices get-event-consumers user-events --limit 20
```

**Output**:
```
Services subscribing to user-events (4):
  1. analytics-service | subscribes for: user.created, user.updated
  2. email-service | subscribes for: user.created
  3. notification-service | subscribes for: user.updated, user.deleted
  4. audit-logger | subscribes for: all events

Total messages processed: ~10K/day
```

**Use case**: "If I add a new event, what will break?"

---

### 7. get-event-producers

**What it does**: Find all services that publish to a given event channel.

**Syntax**:
```bash
supercontext-query-kg --snapshot <path> get-event-producers <channel-name> --limit N
```

**Example**:
```bash
supercontext-query-kg --snapshot ./data/kg_runs/microservices get-event-producers user-events --limit 10
```

**Output**:
```
Services publishing to user-events (1):
  1. user-service | publishes: user.created, user.updated, user.deleted

Average throughput: ~50 events/sec
```

**Use case**: "Who publishes this event? Can I rely on it?"

---

### 8. deploy-blockers-for

**What it does**: Find conditions that would prevent a service from deploying (e.g., broken dependencies, failing tests, incomplete migrations).

**Syntax**:
```bash
supercontext-query-kg --snapshot <path> deploy-blockers-for <service-name> --limit N
```

**Example**:
```bash
supercontext-query-kg --snapshot ./data/kg_runs/microservices deploy-blockers-for user-service
```

**Output**:
```
Deploy blockers for user-service (2):

1. BROKEN_DEPENDENCY
   user-service imports deprecated-auth-lib (in auth-service/legacy/)
   Status: Deprecated, removal scheduled 2026-06-01
   Action: Migrate to new-auth-lib before deployment

2. MISSING_ENDPOINT
   user-service calls POST /api/v2/users but endpoint doesn't exist in any service
   Status: Not yet implemented
   Action: Coordinate with user-database-service team
```

**Use case**: "Can I deploy this service? What's blocking it?"

---

## Part 3: Writing Custom Queries

The 8 standard tools cover most cases. For specialized queries, you can write custom ones.

### Query Language Basics

Queries operate on facts and traverse the graph.

Simple query: "Give me all CALLS facts where upstream=X":
```
FIND fact 
WHERE upstream='my_module:my_function' 
  AND relation_type='CALLS'
RETURN downstream
LIMIT 50
```

Traverse query: "Give me all transitive callers":
```
FIND path
WHERE start='my_module:my_function'
  AND relation_type='CALLS'
  AND depth <= 3
TRAVERSE downstream
RETURN path
```

### Example Custom Query

**Query**: "Find all services that transitively depend on this library"

```
FIND path
WHERE start='common-utils:Logger'  // Starting entity
  AND (relation_type='IMPORTS' OR relation_type='CALLS')
  AND depth <= 5
TRAVERSE downstream
FILTER entity.kind='Service'
RETURN entity.name
LIMIT 100
```

---

## Performance & Optimization

### Query Time Complexity

- `find-callers`: O(E) where E = number of facts
- `blast-radius`: O(E × depth) — can be expensive!
- `search-services`: O(E) with filtering

### Tips for Performance

1. **Limit depth on blast-radius**: Don't traverse 10 levels
2. **Use --limit**: Don't fetch all 50,000 results
3. **Filter by derivation**: Only canonical facts (faster)
4. **Use JSON output**: Less formatting overhead

### Caching

Queries against the same snapshot are cached. Running the same query twice is instant.

If you rebuild the snapshot, cache is cleared.

---

## Troubleshooting

**"No results found"**:
- Did you spell the symbol name correctly?
- Is it in your snapshot? Run `summary` to verify
- Try broadening the search (remove module prefix)

**"Query timed out"**:
- You're traversing too deep (reduce --depth)
- Results are too large (increase --limit or filter)
- The snapshot is very large (this is expected)

**"Permission denied"**:
- Can you read the snapshot files?
- Do you have the right `--snapshot` path?

---

## Further Reading

- **Using queries in practice**: [query-your-repo.md](../03-workflows/query-your-repo.md)
- **Understanding results**: [evidence-retrieval.md](./evidence-retrieval.md)
- **Full query language spec**: ADR-0009

---

*Last updated: 2026-05-25*
```

- [ ] **Step 2: Verify structure and count**

```bash
wc -w docs/getting-started/02-core-features/querying.md
grep "^###" docs/getting-started/02-core-features/querying.md | wc -l
```

Expected: ~2800-3200 words, 8+ section headers (one per tool)

- [ ] **Step 3: Commit**

```bash
git add docs/getting-started/02-core-features/querying.md
git commit -m "docs: Add querying feature doc with 8-tool reference"
```

---

### Task 8: Create coverage-metrics.md

**Files:**
- Create: `docs/getting-started/02-core-features/coverage-metrics.md`

- [ ] **Step 1: Write coverage-metrics.md (1500 words, 4 parts)**

```markdown
# Coverage Metrics

*20-minute read.*

## Part 1: What is Coverage?

Coverage ≠ code coverage (line/branch coverage from tests).

**SuperContext coverage** measures extraction quality:
- Did we find all functions? (entity coverage)
- Did we find all imports? (relation coverage)
- Are facts canonical or inferred? (derivation distribution)

**Example**: "We found 87% of Python functions but only 40% of Flask endpoints."

This tells you:
- Where extractors are working well
- Where gaps exist
- Where to improve next

---

## Part 2: How Metrics Work

Coverage is computed from `coverage.jsonl` in your snapshot.

Each record shows:

```json
{
  "entity_type": "CodeFunction",
  "repo": "flask",
  "language": "python",
  "found": 342,
  "expected": 392,
  "coverage_percent": 87.2,
  "derivation_distribution": {
    "deterministic_static": 340,
    "inferred_llm": 2
  }
}
```

**Fields**:
- `entity_type`: Type of thing we measured (Function, Module, Service, etc.)
- `found`: How many we extracted
- `expected`: How many should exist (computed heuristically)
- `coverage_percent`: found / expected
- `derivation_distribution`: Breakdown by confidence level

---

## Part 3: Using Coverage Reports

### Generate Metrics

```bash
supercontext-coverage-metrics --snapshot ./data/kg_runs/flask
```

Output: `./data/kg_runs/flask/metrics.jsonl`

### Generate Report

```bash
supercontext-coverage-report \
  --snapshot ./data/kg_runs/flask \
  --out ./docs/evaluation/runs/flask-2026-05-25 \
  --run-id flask-example-1 \
  --tenant example-org \
  --expected-repos 1
```

Outputs:
- `coverage-run.json` (data)
- `coverage-run.md` (human-readable report)

### Read the Markdown Report

Example `coverage-run.md`:

```markdown
# Coverage Report: flask-example-1

Generated: 2026-05-25  
Repository: flask  
Expected Repos: 1  

## Summary

| Metric | Value |
|--------|-------|
| Entities Found | 342 |
| Entity Types | 5 |
| Relations Found | 1200 |
| Canonical Facts | 94% |
| Average Coverage | 76% |

## Entity Coverage

| Type | Found | Expected | Coverage |
|------|-------|----------|----------|
| CodeFunction | 342 | 392 | 87% |
| CodeModule | 50 | 50 | 100% |
| Endpoint | 20 | 50 | 40% |
| Service | 1 | 1 | 100% |
| EventChannel | 2 | 2 | 100% |

## Gaps Analysis

**Low coverage (< 70%)**:
- Endpoints (40%): Flask decorator extraction could improve
- Recommendation: Enhance Flask extractor for route parameters

**Gap details**:
- Missing: Routes with variable paths (e.g., /users/<id>)
- Reason: Current regex doesn't handle decorators with arguments
- Fix: Upgrade to AST-based extraction

---
```

---

## Part 4: Contributing to Metrics

### Improve an Extractor

If coverage shows a gap (e.g., "only 40% of endpoints found"), improve the extractor:

1. Run coverage, identify the gap
2. Manually inspect code — find examples of the missing pattern
3. Enhance extractor to find that pattern
4. Re-run coverage
5. Verify improvement

---

[Full doc continues with more examples and troubleshooting]
```

Write the full 1500-word coverage-metrics.md with all sections and examples.

- [ ] **Step 2: Verify word count**

```bash
wc -w docs/getting-started/02-core-features/coverage-metrics.md
```

Expected: ~1400-1600 words

- [ ] **Step 3: Commit**

```bash
git add docs/getting-started/02-core-features/coverage-metrics.md
git commit -m "docs: Add coverage-metrics feature doc"
```

---

### Task 9: Create mcp-integration.md and evidence-retrieval.md

**Files:**
- Create: `docs/getting-started/02-core-features/mcp-integration.md`
- Create: `docs/getting-started/02-core-features/evidence-retrieval.md`

- [ ] **Step 1: Write mcp-integration.md (1500 words)**

Create a complete doc covering MCP protocol, local server setup, registration, and tool usage.

- [ ] **Step 2: Write evidence-retrieval.md (1500 words)**

Create a complete doc covering evidence concepts, Mode A vs Mode B, verification, and sourcing.

- [ ] **Step 3: Commit both**

```bash
git add docs/getting-started/02-core-features/mcp-integration.md
git add docs/getting-started/02-core-features/evidence-retrieval.md
git commit -m "docs: Add mcp-integration and evidence-retrieval feature docs"
```

---

## Phase 4: Workflow Docs (8-10 hours)

### Task 10-13: Create workflow docs (4 tasks)

Each workflow doc is 800-1500 words, task-focused, with all commands and expected output.

**Task 10**: Create `03-workflows/setup-and-first-kg.md`
**Task 11**: Create `03-workflows/query-your-repo.md`
**Task 12**: Create `03-workflows/evaluate-coverage.md`
**Task 13**: Create `03-workflows/extend-with-custom-extractor.md`

For each:
- [ ] Write the workflow doc with step-by-step instructions
- [ ] Include all command invocations and expected output
- [ ] Add troubleshooting section
- [ ] Link to relevant feature docs
- [ ] Commit individually

---

## Phase 5: Examples (10-12 hours)

### Task 14: Create examples structure and README

**Files**:
- Create: `docs/getting-started/examples/README.md`
- Create: `docs/getting-started/examples/real-repos/README.md`

- [ ] **Step 1: Write examples/README.md**

Explain how to use examples, prerequisites, which repos are used, how to run them.

- [ ] **Step 2: Write real-repos/README.md**

Document which real repos (flask, react) we use and why, plus setup instructions.

- [ ] **Step 3: Commit**

```bash
git commit -m "docs: Add examples directory structure and README"
```

---

### Task 15-21: Create example scripts (7 tasks)

**Task 15**: `01-build/build-kg-single-repo.sh` + `build-kg-multi-repo.sh`
**Task 16**: `02-query/query-common-patterns.sh` + `query-with-jq.sh`
**Task 17**: `02-query/find-impact.py`
**Task 18**: `03-coverage/coverage-full-pipeline.sh` + `coverage-compare.sh`
**Task 19**: `04-extend/custom-extractor-template.py`
**Task 20**: `04-extend/flask-routes-extractor.py` + `extractor-test-template.py`
**Task 21**: `05-mcp/start-mcp-server.sh` + `test-mcp-tool.py` + `real-repos/*.sh`

For each script:
- [ ] Write self-documenting bash/python
- [ ] Add comments explaining each section
- [ ] Test that it runs (or verify it would with manual inspection)
- [ ] Commit in logical groups

---

## Phase 6: Integration & Testing

### Task 22: Verify all links and cross-references

- [ ] **Step 1: Check all markdown links**

```bash
# Search for broken links
grep -r "\[.*\](.*\.md)" docs/getting-started/ | \
  grep -o '\(../\|./\)[^)]*' | sort | uniq > /tmp/links.txt

# Verify each file exists
while read link; do 
  if [ ! -f "docs/getting-started/$link" ]; then 
    echo "BROKEN: $link"
  fi
done < /tmp/links.txt
```

- [ ] **Step 2: Verify all ADR references**

```bash
# Check all ADR references are valid (ADR-0001 through ADR-0009 exist)
grep -r "ADR-" docs/getting-started/ | grep -o "ADR-[0-9]\{4\}" | sort | uniq
```

- [ ] **Step 3: Commit**

```bash
git commit -m "docs: Verify all internal links and ADR references"
```

---

### Task 23: Final review and polish

- [ ] **Step 1: Check for consistent formatting**

```bash
# Verify all headers are consistent (##, ###, etc.)
# Verify code blocks have language tags
# Verify examples are properly formatted
grep -r "^\`\`\`$" docs/getting-started/ && echo "Found unmarked code blocks"
```

- [ ] **Step 2: Verify all docs follow the pattern**

Each feature doc should have:
- Part 1: What is it? (concept)
- Part 2: How does it work? (architecture)
- Part 3: How do I use it? (practical)
- Part 4: How do I extend it? (advanced)

Check structure:
```bash
for doc in docs/getting-started/02-core-features/*.md; do
  echo "=== $(basename $doc) ==="
  grep "^## Part\|^## What\|^## How\|^## Using" "$doc" | wc -l
done
```

- [ ] **Step 3: Commit**

```bash
git commit -m "docs: Polish formatting and verify doc structure"
```

---

### Task 24: Create a summary and next steps

- [ ] **Step 1: Update main README.md to reference Getting Started**

Add a section to the main README linking to Getting Started.

- [ ] **Step 2: Verify all examples are runnable**

Spot-check 3 example scripts to ensure they work or have clear instructions.

- [ ] **Step 3: Create a completion checklist**

Document what was created:
```markdown
# Getting Started Documentation - Completion Checklist

✅ Folder structure created (11 directories)
✅ Entry point docs (README.md + GLOSSARY.md)
✅ Concept docs (2 files)
✅ Feature docs (5 files)
✅ Workflow docs (4 files)
✅ Example docs & scripts (15 files)
✅ All internal links verified
✅ All ADR references valid
✅ Formatting polish complete

Total: 28 files, ~22,000 words
```

- [ ] **Step 4: Final commit**

```bash
git commit -m "docs: Getting Started documentation complete

- 28 files across concepts, features, workflows, and examples
- Dual learning paths for users and extenders
- 8 runnable examples on real repositories (flask, react)
- Cross-linked with glossary and ADR references
- Complete feature coverage: KG, querying, coverage, MCP, evidence

Ready for onboarding new engineers."
```

---

## Summary

This plan creates a comprehensive, interconnected onboarding documentation suite:

- **Total files**: 28 (docs + examples + scripts)
- **Total content**: ~22,000 words
- **Estimated duration**: 35-40 hours
- **Structure**: Feature-centric with learning paths
- **Quality gates**: Links verified, formatting consistent, all examples testable

Each task is designed to be independent and completable in 15-30 minutes, enabling parallel work or incremental completion.

---

## Execution Options

Plan is ready. Two execution paths available:

**Option 1: Subagent-Driven (recommended)**
- Fresh subagent per task
- Review between tasks
- Fast iteration
- Requires: superpowers:subagent-driven-development

**Option 2: Inline Execution**
- Execute tasks in this session
- Batch execution with checkpoints
- Requires: superpowers:executing-plans

Which would you prefer?
