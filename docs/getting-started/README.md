# Getting Started with SuperContext

**Last updated**: 2026-05-25

Welcome to SuperContext! This guide will help you understand what SuperContext is, learn how to use it, and integrate it into your development workflow.

## What is SuperContext?

SuperContext is a typed cross-service knowledge graph for AI coding agents in microservice organizations. It solves the **change safety** problem: when an AI agent edits service A, SuperContext tells it which services B–Z will break before the diff is written.

Whether you're building microservices, managing multiple codebases, or want your AI coding tools to understand system dependencies, SuperContext provides:

- **Dependency Intelligence**: Understand how services and code modules depend on each other
- **Change Impact Analysis**: Know what breaks when you change something
- **Agentic Reasoning**: Power AI coding agents with architectural context
- **Cross-repo Navigation**: Find callers, callees, and deployment blockers across service boundaries

---

## Quick Navigation

| Document | Purpose | Read Time | Best For |
|----------|---------|-----------|----------|
| [What is SuperContext](./01-concepts/what-is-supercontext.md) | Core concepts and value proposition | 5 min | Everyone |
| [Architecture Overview](./01-concepts/architecture-overview.md) | System design, layers, and components | 10 min | Implementers, architects |
| [Knowledge Graph Explained](./02-core-features/knowledge-graph.md) | How the KG works, entity types, relations | 8 min | Users, extenders |
| [MCP Integration](./02-core-features/mcp-integration.md) | Using SuperContext via MCP protocol | 7 min | Tool builders |
| [Setup and First KG](./03-workflows/setup-and-first-kg.md) | Install and build your first knowledge graph | 15 min | First-time users |
| [Query Your Repo](./03-workflows/query-your-repo.md) | Run queries against the KG | 12 min | Hands-on learning |
| [Evaluate Coverage](./03-workflows/evaluate-coverage.md) | Assess KG completeness and gaps | 10 min | QA, validation |
| [Glossary](./GLOSSARY.md) | Terms and abbreviations | Reference | Everyone |

---

## Three Learning Paths

### Path 1: Using SuperContext (30-45 minutes)

Get up and running with SuperContext quickly. Perfect if you want to **build and query a knowledge graph** for your codebase.

1. **[What is SuperContext](./01-concepts/what-is-supercontext.md)** (5 min)
   - Understand the core problem (change safety) and solution

2. **[Setup and First KG](./03-workflows/setup-and-first-kg.md)** (15 min)
   - Install dependencies, build your first knowledge graph snapshot

3. **[Query Your Repo](./03-workflows/query-your-repo.md)** (12 min)
   - Learn query syntax, run find-callers, find-callees, blast-radius queries

4. **[Evaluate Coverage](./03-workflows/evaluate-coverage.md)** (10 min)
   - Understand what your KG covers and identify gaps

**After this path**: You'll have a working KG and know how to extract dependency intelligence from your codebase.

---

### Path 2: Extending SuperContext (2-3 hours)

Deep dive into how SuperContext works and how to customize it. Perfect if you want to **build tools on top of SuperContext** or **extend it to new languages or frameworks**.

1. **[What is SuperContext](./01-concepts/what-is-supercontext.md)** (5 min)
   - Foundations

2. **[Architecture Overview](./01-concepts/architecture-overview.md)** (10 min)
   - Understand layers, storage, and ontology

3. **[Knowledge Graph Explained](./02-core-features/knowledge-graph.md)** (8 min)
   - Entity types, relation types, evidence model, promotion rules

4. **[MCP Integration](./02-core-features/mcp-integration.md)** (7 min)
   - Public protocol, tool definitions, and how to build MCP clients

5. **Advanced Topics** (see BACKLOG.md for upcoming docs on):
   - Custom extractors for new languages
   - Writing promotion rules
   - Multi-tenant configuration
   - Hosting the MCP server

**After this path**: You'll understand SuperContext's architecture, can extend it, and can integrate it into custom tools.

---

### Path 3: Quick Learning (10 minutes)

Just want a quick overview? Start here.

1. **[What is SuperContext](./01-concepts/what-is-supercontext.md)** (5 min)
   - Read the value prop

2. **[Quick Commands Reference](#quick-commands-reference)** (below, 3 min)
   - See what SuperContext can do

3. **[Glossary](./GLOSSARY.md)** (2 min)
   - Reference terms as needed

---

## Quick Commands Reference

### Build a Knowledge Graph

```bash
# From a local Python or TypeScript repository
python -m source.scripts.build_kg --repo /path/to/repo --out data/kg_runs/my-repo

# From multiple repositories (fleet view)
python -m source.scripts.build_multi_kg \
  --repo /path/to/repo-1 \
  --repo /path/to/repo-2 \
  --out data/kg_runs/multi-repo
```

### Query the Knowledge Graph

```bash
# View KG summary
python -m source.scripts.query_kg --snapshot data/kg_runs/my-repo summary

# Find all callers of a function
python -m source.scripts.query_kg --snapshot data/kg_runs/my-repo \
  find-callers module.function --limit 5

# Find callees (functions called by a symbol)
python -m source.scripts.query_kg --snapshot data/kg_runs/my-repo \
  find-callees module.function --limit 5

# View blast radius (impact of changing a symbol)
python -m source.scripts.query_kg --snapshot data/kg_runs/my-repo \
  blast-radius module.function --depth 2

# Find modules importing a package
python -m source.scripts.query_kg --snapshot data/kg_runs/my-repo \
  modules-importing package-name --limit 5

# Get top-level dependencies
python -m source.scripts.query_kg --snapshot data/kg_runs/my-repo \
  top-dependencies --limit 10

# Get dependency info for a specific package
python -m source.scripts.query_kg --snapshot data/kg_runs/my-repo \
  dependency-info package-name
```

### Evaluate Coverage

```bash
# Compute metrics for your KG
python -m source.scripts.coverage_metrics \
  --snapshot data/kg_runs/my-repo \
  --expected-repos 1

# Generate a coverage report
python -m source.scripts.coverage_report \
  --snapshot data/kg_runs/my-repo \
  --out docs/evaluation/runs/my-run \
  --run-id my-run-2026-05-25 \
  --tenant my-org \
  --expected-repos 1 \
  --metric-config source/kg/metrics/config.yaml
```

### Run the MCP Server

```bash
# Start the MCP server (for development)
python -m source.mcp.server --snapshot data/kg_runs/my-repo
```

---

## Running Examples

### Example 1: Find All Callers of a Function

After building your first KG (see [Setup and First KG](./03-workflows/setup-and-first-kg.md)):

```bash
# Assuming you built a KG and know the function name
python -m source.scripts.query_kg --snapshot data/kg_runs/my-repo \
  find-callers mymodule.myfunction
```

**Output**: A list of all code locations that call `mymodule.myfunction`, with file paths and line numbers.

---

### Example 2: Understand Change Impact

Find everything that would be affected by modifying a function:

```bash
python -m source.scripts.query_kg --snapshot data/kg_runs/my-repo \
  blast-radius mymodule.myfunction --depth 3
```

**Output**: A tree of call chains showing what breaks when you change that function.

---

### Example 3: Check Service Dependencies

If your repo contains multiple services:

```bash
python -m source.scripts.query_kg --snapshot data/kg_runs/my-repo \
  find-callees some-service.handler
```

**Output**: All external services or modules that `some-service.handler` depends on.

---

## What's Next?

- **[Architecture Decisions (ADRs)](../../../adr/)** — The binding spec for design decisions
- **[Evaluation and Query Corpus](../../docs/evaluation/PRODUCT-QUERY-SET.md)** — Acceptance criteria and test cases
- **[BACKLOG.md](../../BACKLOG.md)** — Tracked deferrals and future work

---

## Glossary

See [GLOSSARY.md](./GLOSSARY.md) for definitions of key terms:

- **Change Safety** — The ability to predict before merging what other services will break
- **Knowledge Graph (KG)** — A typed, queryable graph of code entities (functions, modules, services) and their relationships
- **Entity** — A node in the KG (e.g., a function, module, service, or external package)
- **Fact** — A relation between entities (e.g., "function A calls function B")
- **Evidence** — The source code bytes proving a fact exists
- **Extractor** — A language-specific parser that builds the KG from source code
- **MCP** — The Model Context Protocol; SuperContext's public interface for tools and agents

---

## Questions?

If you have questions while using SuperContext:

1. **Check the [Glossary](./GLOSSARY.md)** for term definitions
2. **Review the [Architecture Overview](./01-concepts/architecture-overview.md)** for system design questions
3. **See [BACKLOG.md](../../BACKLOG.md)** for known gaps and limitations
4. **Open an issue** in the repository

---

## How This Documentation is Organized

```
docs/getting-started/       ← You are here
├── README.md              ← Start here
├── 01-concepts/           ← What, why, and how
├── 02-core-features/      ← Capabilities and tools
├── 03-workflows/          ← Step-by-step guides
└── GLOSSARY.md            ← Terminology
```

Each section builds on the previous. Start with your learning path above, and reference other sections as needed.

---

**Ready to begin?** Start with [Path 1: Using SuperContext](#path-1-using-supercontext) if you want to get hands-on right away.
