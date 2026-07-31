# Getting Started Documentation Design

**Date**: 2026-05-25  
**Scope**: Create comprehensive onboarding documentation for new engineers joining SuperContext project  
**Status**: APPROVED

## Problem Statement

SuperContext is an early-stage knowledge graph product with a sophisticated architecture (11 ADRs, multiple implementation layers). New engineers need:

1. Clear explanation of what SuperContext solves and how
2. Step-by-step guides to accomplish common tasks (build a KG, query it, extend extractors)
3. Both "how to use" and "how to extend" information for all audiences
4. Runnable examples on real repositories to make learning concrete

Current state: README.md exists but focuses on CLI quickstart. Architecture lives in ADRs but lacks beginner-friendly framing.

## Design Goals

- **Beginner-friendly**: Start with high-level concepts, progressively deepen
- **Dual-path**: Support both users (run SuperContext) and extenders (add features) equally
- **Practical**: Include runnable examples, real repository references, code samples
- **Self-contained**: Each feature is documented completely (concept → architecture → usage → extension)
- **Navigable**: Clear entry point, recommended learning paths, glossary for quick reference

## Audience & Scope

### Primary Audiences
1. **Users**: Engineers running SuperContext on their repos, writing queries, understanding coverage
2. **Extenders**: Engineers adding extractors, writing custom queries, modifying the system

### Assumptions
- Readers are competent engineers, but SuperContext is new to them
- Readers have Python 3.11+, Node.js, and git installed
- Readers are familiar with basic Git and CLI workflows

## Architecture & Structure

### Folder Organization

```
docs/getting-started/
├── README.md                          # Entry point with learning paths
├── GLOSSARY.md                        # Quick reference for concepts
│
├── 01-concepts/                       # Foundation layer (no code)
│   ├── what-is-supercontext.md       # Value prop, use cases
│   └── architecture-overview.md       # Layers, components, data flow
│
├── 02-core-features/                 # Main feature docs (What → How → Extend)
│   ├── knowledge-graph.md            # KG concept, building, extractors
│   ├── querying.md                   # 8 query tools, real examples
│   ├── coverage-metrics.md           # Coverage concept, reports, contributing
│   ├── mcp-integration.md            # MCP protocol, local server
│   └── evidence-retrieval.md         # Evidence modes, verification
│
├── 03-workflows/                      # Task-oriented end-to-end guides
│   ├── setup-and-first-kg.md         # Install → init → build
│   ├── query-your-repo.md            # Build → query → interpret results
│   ├── evaluate-coverage.md          # Build → coverage metrics → analysis
│   └── extend-with-custom-extractor.md # Design → implement → test → integrate
│
└── examples/                          # Runnable scripts & templates
    ├── README.md                      # How to use examples
    ├── 01-build/
    │   ├── build-kg-single-repo.sh
    │   └── build-kg-multi-repo.sh
    ├── 02-query/
    │   ├── query-common-patterns.sh
    │   ├── query-with-jq.sh
    │   └── find-impact.py
    ├── 03-coverage/
    │   ├── coverage-full-pipeline.sh
    │   └── coverage-compare.sh
    ├── 04-extend/
    │   ├── custom-extractor-template.py
    │   ├── flask-routes-extractor.py
    │   ├── extractor-test-template.py
    │   └── fixture-repo-setup.sh
    ├── 05-mcp/
    │   ├── start-mcp-server.sh
    │   └── test-mcp-tool.py
    └── real-repos/
        ├── README.md
        ├── setup-flask.sh
        ├── setup-react.sh
        └── setup-microservice-example.sh
```

### Learning Paths

**Path 1: Using SuperContext** (for users)
1. `README.md` — Entry point
2. `01-concepts/what-is-supercontext.md` — Understand the value
3. `02-core-features/knowledge-graph.md` (first 2 sections) — What is a KG?
4. `03-workflows/setup-and-first-kg.md` — Build your first snapshot
5. `03-workflows/query-your-repo.md` — Run queries
6. `02-core-features/coverage-metrics.md` — Understand coverage
7. `02-core-features/mcp-integration.md` — Integrate with IDE

**Path 2: Extending SuperContext** (for extenders)
1. `README.md` — Entry point
2. `01-concepts/architecture-overview.md` — Understand architecture
3. `02-core-features/knowledge-graph.md` (sections 3-4) — Extractors deep-dive
4. `03-workflows/extend-with-custom-extractor.md` — Write an extractor
5. `02-core-features/evidence-retrieval.md` — Evidence backing
6. `02-core-features/querying.md` (extension section) — Custom queries (optional)

**Path 3: Quick Learning** (for those skimming)
1. `README.md` — Overview
2. `GLOSSARY.md` — Definitions
3. Jump to specific feature doc as needed

---

## Document Specifications

### Entry Point: README.md (500 words)

**Structure**:
- Welcome: What SuperContext is, key use cases
- Visual decision tree showing learning paths
- Quick navigation table (all docs + read time)
- "What's next after Getting Started" → Links to ADRs, contributing, eval

---

### Reference: GLOSSARY.md (300 words)

**Structure**:
- Alphabetical list of 20-25 core terms:
  - Entity, Fact, Evidence, Knowledge Graph, Coverage, Extractor, Evidence Mode A/B, Derivation class, Tenant, URN, JSONL, MCP, etc.
- Each term: 1-2 sentence definition + link to full doc
- Mini-cheatsheet: Common commands with brief explanations

---

### Concept Docs: `01-concepts/`

#### **what-is-supercontext.md** (1000 words)
**Sections**:
1. The problem: "How do I know what breaks when I edit service A?"
2. The solution: Typed knowledge graph of code facts
3. Key concepts: Entities, facts, evidence (no deep dive)
4. Use cases: Change safety, dependency analysis, coverage understanding
5. High-level architecture: Layers without implementation details

**Code samples**: None (conceptual only)

---

#### **architecture-overview.md** (1500 words)
**Sections**:
1. Layer 1: Ingestion (extractors pull facts from code)
2. Layer 2: Storage (JSONL snapshot with entities/facts/evidence)
3. Layer 3: Querying (8 tools + custom queries)
4. Data model: Entity → Fact → Evidence relationship
5. Derivation tiers: authoritative → LLM-inferred (diagram)
6. Flow diagram: Repo → Extract → Store → Query

**Code samples**: 
- Example JSON entity/fact/evidence objects (30 lines)
- Diagram showing data flow

**ADR callouts**: ADR-0001 through ADR-0006 (with links)

---

### Feature Docs: `02-core-features/`

Each follows this 4-part structure:

#### **knowledge-graph.md** (2500 words)

**Part 1: What is a Knowledge Graph?** (400 words)
- Problem: How do you track dependencies in microservices?
- Solution: Typed graph of facts extracted from code
- Example: "Flask uses sqlalchemy" → Entity relationship
- Why it matters: Feeds all queries and coverage metrics

**Part 2: How SuperContext Builds One** (700 words)
- Extractor concept: Tool that walks code (AST, config) and emits facts
- Extraction flow: Parse → Traverse → Emit entities/facts/evidence
- Current coverage: Python (functions, imports, config) + TypeScript (functions, imports, endpoints)
- Code references: `source/kg/extractors/` folder structure
- Diagram: Extractor pipeline

**Part 3: Using the KG** (600 words)
- Build commands: `supercontext-build-kg`, `supercontext-build-multi-kg`
- Snapshot structure: 5 JSONL files (entities, facts, evidence, coverage, manifest)
- Querying: Brief mention (full coverage in querying.md)
- Example: Build flask, inspect snapshot files

**Part 4: Writing a Custom Extractor** (800 words)
- When to write one: You've identified a gap in coverage
- Extractor anatomy: Class structure, required methods, how it integrates
- Concrete example: Minimal Flask route extractor (50 lines)
- Evidence backing: Why every fact needs `bytes_ref`
- Testing: How to test an extractor in isolation
- Checklist: Before committing

**Code samples**:
- Minimal extractor (template form, 50 lines)
- Flask routes extractor (working example, 80 lines)
- Test fixtures (3-4 test cases)
- Reference: `source/kg/extractors/python/extractor.py` actual code

**ADR callouts**: ADR-0006 (ontology), ADR-0005 (evidence)

---

#### **querying.md** (3000 words)

**Part 1: What are Queries?** (400 words)
- Problem: How do you ask questions about your KG?
- Solution: 8 standardized tools, extensible query language
- The 8 tools overview (one sentence each):
  - `find-callers`: Who calls this function?
  - `find-callees`: What does this call?
  - `blast-radius`: Full impact of changing this
  - `search-services`: Find services by name
  - `get-service-brief`: What does this service do?
  - `get-event-consumers`: Who consumes this event?
  - `get-event-producers`: Who publishes this event?
  - `deploy-blockers-for`: What prevents deployment?

**Part 2: How Querying Works** (700 words)
- Query engine architecture: Query parser → Fact filter → Traversal → Results
- Traversal rules: Follow CALLS relations, IMPORTS, SERVICE_HOSTS, etc.
- Performance notes: Depth limits, result limits, caching
- Derivation filtering: How canonical/candidate tiers affect results
- Diagram: Query execution flow

**Part 3: Using Queries** (1000 words)
- Command syntax: `supercontext-query-kg --snapshot <path> <query> [args]`
- Output formats: Table (default), JSON, CSV
- For each of 8 tools:
  - Syntax
  - Real example (flask or react)
  - Sample output (actual text)
  - How to read results
- Example: "Who calls the authentication middleware?" → actual command + output

**Part 4: Writing Custom Queries** (900 words)
- Query language overview: DSL for fact traversal
- Extending query engine: Adding new traversals
- Example: Custom query "find all services that import pandas"
- Testing: How to test your query
- Performance: Optimization techniques

**Code samples**:
- All 8 query commands with real output on flask/react
- Custom query implementation (50 lines)
- Test fixtures

**ADR callouts**: ADR-0002 (MCP tools), ADR-0009 (reverse dependencies)

---

#### **coverage-metrics.md** (1500 words)

**Part 1: What is Coverage?** (300 words)
- Coverage ≠ code coverage
- What we measure: Entity discovery, relation type coverage, derivation tier distribution
- Why it matters: Tells you "did we extract everything we should have?"
- Example: "We found 85% of Python functions but only 40% of config-based facts"

**Part 2: How Metrics Work** (500 words)
- Coverage model: Entities by type, relations by type, derivation tiers
- The `coverage.jsonl` file: Schema and examples
- Metrics CLI: `supercontext-coverage-metrics`
- Metric configuration: What metrics are computed (in `source/kg/metrics/config.yaml`)
- Diagram: Coverage computation flow

**Part 3: Using Coverage Reports** (400 words)
- Build a coverage report: `supercontext-coverage-report --snapshot <path>`
- Output files: `coverage-run.json` (data) + `coverage-run.md` (report)
- Reading the markdown report: Charts, entity breakdown, gap analysis
- Interpreting JSON: Filtering, aggregating by repo/language/entity-type
- Example: Real report walkthrough (sample output)

**Part 4: Contributing to Metrics** (300 words)
- Adding new coverage checks: Extend metric config
- Creating custom reports: Filtering + aggregation patterns
- Improving extractor coverage: Use gaps to identify missing extractors

**Code samples**:
- Sample `coverage-run.json` snippet
- Sample `coverage-run.md` report
- Metric config YAML (shortened)
- Example: Filtering JSON with jq

**ADR callouts**: ADR-0006 §Coverage tier

---

#### **mcp-integration.md** (1500 words)

**Part 1: What is MCP?** (300 words)
- MCP = Model Context Protocol
- Why SuperContext uses it: Standard interface for AI agents to query KG
- 8 tools SuperContext exports (mapped to CLI queries)
- Benefits: IDE integration, agent-native interface

**Part 2: How the Local Server Works** (500 words)
- Architecture: Local JSON-RPC server, agents connect via HTTP
- Startup: `supercontext-init --serve`
- What runs: Server listening on localhost:8000 (or configured port)
- Tool registration: Agents discover available tools via MCP discovery
- Flow diagram: Agent → MCP client → SuperContext server → Snapshot

**Part 3: Using the MCP Server** (400 words)
- Registration in Claude Code: Add MCP endpoint to settings
- Registration in Codex: Similar process with plugin system
- Calling tools: Example agent prompts that trigger tools
- Tool signatures: Each tool's arguments and response format
- Example: "Find callers of authenticate()" → Tool call → Results back in agent context

**Part 4: Extending with Custom Tools** (300 words)
- Adding new MCP tools: Where to add in codebase
- Tool implementation: Wrapper around query engine
- Hosting custom tools: Running alongside SuperContext server
- Security: MCP auth considerations

**Code samples**:
- MCP server startup command
- Example tool signature (JSON)
- Python code for calling MCP tool
- Agent prompt example that triggers tool use

**ADR callouts**: ADR-0002 (MCP protocol specification)

---

#### **evidence-retrieval.md** (1500 words)

**Part 1: What is Evidence?** (300 words)
- Problem: How do we know facts are true?
- Solution: Every fact is backed by code evidence (bytes_ref)
- Why it matters: Enables verification, supports reasoning about certainty
- Example: "Function A calls B" backed by exact line number in exact commit

**Part 2: Mode A: Commit-Pinned Retrieval** (500 words)
- What it is: Direct byte retrieval via git history
- How it works: `bytes_ref = {repo, commit_sha, path, line_start, line_end}`
- Advantages: Always available, immutable, cryptographically verifiable
- Implementation: `go-git` / `pygit2` retrieval
- Use case: Facts that must be verified, compliance audits
- Example: bytes_ref structure, actual code retrieval

**Part 3: Mode B: Selective Ladder Retrieval** (500 words)
- What it is: On-demand retrieval: ripgrep → AST → Claude
- When to use: Facts that can be re-derived, time-sensitive
- Cost tradeoff: Slower, more expensive, but flexible
- Implementation: Ladder in `source/kg/integrations/evidence/`
- Example: Verifying a dynamic import fact

**Part 4: Verification** (200 words)
- How to verify evidence: Run verification tool on fact
- Interpreting verification results: Match/mismatch/expired
- Understanding `bytes_ref`: Repo, commit, path, line range
- Sourcing facts back to code: From query result → bytes_ref → actual code

**Code samples**:
- bytes_ref object structure (JSON)
- Evidence verification command
- Example evidence object with full detail
- Mode A vs Mode B comparison table

**ADR callouts**: ADR-0005 (evidence retrieval modes)

---

### Workflow Docs: `03-workflows/`

Task-oriented guides. Each assumes concepts are known; focus is on "how do I do X?"

#### **setup-and-first-kg.md** (800 words)

**Steps**:
1. Verify prerequisites (Python 3.11, Node.js, git)
2. Install: `curl` script, verify with `--help`
3. Choose a repo (recommend flask or react with reasoning)
4. Clone repo
5. Run `supercontext-init` (dry-run first)
6. Build: `supercontext-build-kg --repo <path> --out <snapshot>`
7. Verify: `ls` snapshot files, run `summary` query
8. Troubleshoot: Common errors and fixes

**Code samples**:
- All command invocations with expected output
- Error messages and solutions
- Snapshot file listing

**Time estimate**: 15 minutes total

---

#### **query-your-repo.md** (1200 words)

**Steps**:
1. Verify you have a snapshot (from setup workflow)
2. Query basics: Command structure, output formats
3. For each of 8 queries:
   - What it answers
   - Syntax
   - Real example on flask/react
   - Sample output
   - How to interpret results
4. Combining queries: Common patterns
5. Troubleshooting: No results, unexpected results

**Code samples**:
- All 8 query commands with real output
- Interpretation examples
- Common follow-up patterns

**Time estimate**: 30 minutes to work through all 8

---

#### **evaluate-coverage.md** (1000 words)

**Steps**:
1. Why coverage matters: Tells you what you're missing
2. Build coverage metrics: `supercontext-coverage-metrics`
3. Generate report: `supercontext-coverage-report`
4. Read the markdown report: Sections and what they mean
5. Examine JSON: Filtering by type, repo, language
6. Identify gaps: Common missing patterns (dynamic imports, configs, etc.)
7. Decide: When to write extractors vs. accepting gaps

**Code samples**:
- Command invocations
- Sample report output (shortened)
- JSON filtering with jq
- Gap analysis template

**Time estimate**: 20 minutes

---

#### **extend-with-custom-extractor.md** (1500 words)

**Steps**:
1. Decide what to extract: Identify a gap, pick a pattern
2. Plan: Will it be AST-based or config-based?
3. Anatomy of an extractor: File structure, entry point
4. Write the extractor: Concrete walkthrough (Flask routes example)
   - Setup: Imports, class definition
   - Visitor: AST traversal
   - Emission: Creating entities/facts/evidence
   - Error handling: Graceful failures
5. Test locally: Unit tests, fixtures
6. Integrate: Add to build_kg, run full build
7. Verify: Check coverage report for improvement
8. Checklist: Before committing

**Code samples**:
- Full Flask routes extractor (100 lines, heavily commented)
- Test fixtures (3-4 test cases)
- Integration example
- Pre-commit checklist template

**Time estimate**: 60-90 minutes for first extractor

---

### Examples Folder: `/examples/`

**`README.md`** (300 words)
- What examples are included
- How to run them (prerequisites, setup)
- Which repo to use (flask vs. react vs. microservice pair)
- How examples relate to workflow docs

---

**`01-build/` Scripts**

**`build-kg-single-repo.sh`**:
- Clone flask (or user repo)
- Run build with verbose output
- Verify snapshot
- Show summary
- ~3 min runtime

**`build-kg-multi-repo.sh`**:
- Setup 2 fixture services
- Build multi-repo snapshot
- Show inter-service links
- ~5 min runtime

---

**`02-query/` Scripts**

**`query-common-patterns.sh`**:
- Run all 8 queries on snapshot
- Format output nicely
- Demonstrate each tool
- ~2 min runtime

**`query-with-jq.sh`**:
- Show how to filter JSON output
- Common jq patterns
- Example: "Find all services importing pandas"

**`find-impact.py`**:
- Programmatic snapshot analysis
- Direct JSONL reading
- Blast-radius computation
- Output: "If you change X, these services break"

---

**`03-coverage/` Scripts**

**`coverage-full-pipeline.sh`**:
- Build KG (if needed)
- Run metrics
- Generate report
- Open markdown
- ~5-10 min runtime

**`coverage-compare.sh`**:
- Compare coverage across versions
- Show deltas
- Useful for tracking improvement

---

**`04-extend/` Scripts**

**`custom-extractor-template.py`**:
- Starter code (100 lines)
- All required methods stubbed
- Comments on each section
- Copy, fill in, done

**`flask-routes-extractor.py`**:
- Full working example (120 lines)
- Extracts Flask `@app.route()` decorators
- AST-based traversal
- Well-commented

**`extractor-test-template.py`**:
- Test structure (50 lines)
- Positive, negative, edge case tests
- Fixture loading pattern

**`fixture-repo-setup.sh`**:
- Create minimal Flask app
- Run supercontext-init on it
- Ready for extractor testing

---

**`05-mcp/` Scripts**

**`start-mcp-server.sh`**:
- Check snapshot exists (build if needed)
- Start server
- Show registration instructions
- Wait for ready

**`test-mcp-tool.py`**:
- Connect to MCP server
- Call a tool
- Pretty-print result

---

**`real-repos/` Setup Scripts**

**`setup-flask.sh`**, **`setup-react.sh`**, **`setup-microservice-example.sh`**:
- Clone, init, and optionally build for each example
- Environment setup
- Verification steps

---

## Cross-Document Navigation

### Linking Strategy

1. **Entry points**:
   - README → learning paths → first doc in path
   - GLOSSARY → concepts doc or feature doc where term is explained

2. **Within feature docs**:
   - Feature doc section 3 (usage) → links to workflow
   - Feature doc section 4 (extension) → links to examples and to related feature docs

3. **Within workflows**:
   - Workflow → links to relevant feature doc for theory
   - Workflow → links to examples that show practical implementation

4. **ADR references**:
   - Every feature doc has "Further Reading" section linking to ADRs

### Example Navigation Paths

**User path**:
- README → `setup-and-first-kg.md` → `query-your-repo.md` → `coverage-metrics.md` → `mcp-integration.md`
- Each step links back to relevant feature docs for details

**Extender path**:
- README → `architecture-overview.md` → `knowledge-graph.md` → `extend-with-custom-extractor.md`
- Reference examples and other feature docs (evidence, querying) as needed

---

## Implementation Details

### Content Guidelines

**Concept docs** (01-concepts/):
- No code (or minimal example objects)
- Focus on "why" and "what"
- Use diagrams, flow charts
- Keep to <2000 words

**Feature docs** (02-core-features/):
- 1500-3000 words each
- 4-part structure: What → How → Usage → Extension
- Code samples: 3-5 per doc, realistic and tested
- Diagram where helpful (data structures, flows)
- Every code sample has context (what file it's in, how to run it)

**Workflow docs** (03-workflows/):
- Task-focused, step-by-step
- 800-1500 words each
- All commands shown with expected output
- Troubleshooting section
- 1-2 linked to examples

**Examples** (examples/):
- Bash scripts are self-documenting with comments
- Python scripts have docstrings
- Templates have TODOs for user customization
- Real repos preferred; fixture repos as fallback
- Each script <= 200 lines (readability)

### Code Sample Policy

- **Every code sample must be tested or drawn from actual codebase**
- **Simplify for readability** (but don't oversimplify to the point of misleading)
- **Reference actual file paths** so reader can see full context
- **Include output** when non-obvious (command output, error messages, etc.)

### Commit Strategy

- Commit design doc: `design: Add Getting Started documentation design`
- Commit examples: As part of implementation (separate commit)
- Commit content: One commit per major section or workflow (keep commits navigable)

---

## Success Criteria

A new engineer can:
1. **By end of setup workflow**: Have a built KG, understand what's in it
2. **By end of querying workflow**: Run 3+ queries correctly, interpret results
3. **By end of coverage workflow**: Understand what coverage means, read a report
4. **By end of extension workflow**: Write a working extractor, test it, integrate it
5. **Throughout**: Navigate to any concept, find working examples, understand ADR relevance

---

## Out of Scope

- Product roadmap (belongs in BACKLOG.md)
- Full ADR text (link to existing ADRs instead)
- Installation troubleshooting beyond common cases (separate troubleshooting guide)
- Comparative analysis with other tools (marketing, not onboarding)
- Internal implementation details not relevant to users/extenders

---

## Timeline & Ownership

- **Design approval**: This document
- **Implementation**: Brainstorming skill → writing-plans → execution
- **Review**: User reviews on-disk docs before merge
- **Commit**: One logical commit per workflow section + examples

---

## Appendix: File Counts & Estimates

| Component | Files | Total Words | Est. Creation |
|-----------|-------|------------|---|
| README + GLOSSARY | 2 | 1000 | 2 hours |
| Concept docs | 2 | 2500 | 3 hours |
| Core feature docs | 5 | 10,500 | 12 hours |
| Workflow docs | 4 | 5000 | 8 hours |
| Examples (docs + scripts) | 15 | 3000 (docs) | 10 hours |
| **Total** | **28** | **~22,000** | **~35 hours** |

(Includes writing, testing, code samples, diagrams)
