# What is SuperContext?

**A five-minute read.** SuperContext is a typed knowledge graph that answers "What will break when I change this?" for microservice organizations. For a deeper dive into design decisions, see [architecture-overview.md](./architecture-overview.md).

## The Problem

Imagine you work at a company running 50+ interdependent microservices. One engineer submits a PR that changes a critical data structure. Another needs to refactor a function. A third is adding a new feature. The question is always the same: **"What will break when I make this change?"**

Today, answering that question is painful:

- **Manual searching**: You grep through codebases, check imports, trace function calls across repos. This is slow and error-prone.
- **Incomplete answers**: You miss dependencies you didn't think to look for. A service depends on your API in a way you never documented. A job queue consumer expects a specific message shape. An internal library is used by fifteen services you forgot about.
- **Production debugging**: You find out what broke when the alerts fire. The cost is real: customer impact, incident response, post-mortems.

The real cost isn't the time you spend searching—it's the refactorings you **don't** do because the blast radius is unknown. It's the features that ship slower because integration is risky. It's the onboarding time for new engineers who don't know the dependency landscape.

**SuperContext solves this** by building a knowledge graph of your services, APIs, functions, imports, and data flows—then answering dependency questions in seconds with verifiable evidence.

## How It Works: The Three Layers

SuperContext operates as a three-layer system:

### Layer 1: Extraction

**Extractors** analyze your code and pull structured facts from it. They parse ASTs (abstract syntax trees), config files, type definitions, and API schemas to discover:
- Functions and modules within each service
- Imports and dependencies between them
- API endpoints and their handlers
- Event channels and message consumers
- Data flows and service interactions

Extractors are language and framework aware. Today, SuperContext includes extractors for Python and TypeScript/JavaScript. Each extractor reports not just *what* it found, but *where*—the exact commit hash, file path, and line numbers so evidence is always verifiable.

### Layer 2: Storage

**The knowledge graph** stores facts as a typed, directed graph. Every fact has three parts:

- **Entities**: The things in your codebase (functions, modules, services, endpoints, event topics)
- **Relations**: How they connect (function A calls function B; service X hosts endpoint Y; topic Z publishes to consumer W)
- **Evidence**: The commit hash, file, and line numbers proving the fact is real

The graph is stored deterministically and versioned. When you commit code, the graph updates to match.

### Layer 3: Querying

**Eight standard tools** query the graph:

```
Repo Code
    ↓
Extractors (parse AST, config, schemas)
    ↓
Knowledge Graph (typed facts + evidence)
    ↓
Query Engine (find-callers, blast-radius, etc.)
    ↓
MCP Server (AI-ready, human-readable)
```

These tools answer the most common questions:
- **find-callers**: Who calls this function?
- **find-callees**: What does this function call?
- **blast-radius**: If I change this symbol, what breaks? (transitive)
- **get-service-brief**: What does this service do, and what does it depend on?
- **search-services**: Find services by name or property
- **get-event-producers**: Who publishes to this topic?
- **get-event-consumers**: Who subscribes to this topic?
- **deploy-blockers-for**: What must be deployed first for this service to work?

The query engine is available as an **MCP server** for AI agents and as command-line tools for human exploration.

## Key Concepts

### Entities

An **entity** is a first-class thing in your system:

- **CodeSymbol**: A function, method, class, or type (e.g., `payments.charge()`)
- **CodeModule**: A file or package boundary (e.g., `payments/invoice_gen.py`)
- **Service**: A deployed unit (e.g., `pricing-service`)
- **Endpoint**: An HTTP or gRPC route (e.g., `POST /api/v1/charges`)
- **EventChannel**: A topic or queue (e.g., `payments.order_created`)
- **ExternalPackage**: A library your code depends on (e.g., `requests`)

### Facts

A **fact** describes a relationship between entities:

- **CALLS**: Function A calls function B
- **IMPORTS**: Module A imports module B
- **HOSTS**: Service A hosts endpoint B
- **PUBLISHES**: Service A publishes to event channel B
- **CONSUMES**: Service A consumes from event channel B
- **DEPENDS_ON**: Service A depends on service B

### Evidence

Every fact includes **evidence**: the commit hash, file path, and line range where the fact was observed. This means when you ask "who calls this function?" the answer isn't just a list of names—it's a list with proof. Click to see the exact code that established the relationship.

### Coverage

**Coverage** metrics tell you how complete the graph is. Did the extractor see all your code? Are there services or languages we don't yet understand? Coverage metrics surface uninstrumented repos and languages so you know when the graph has gaps.

## Why SuperContext Matters

### For Engineers

- **Change safety**: Before refactoring, see the real blast radius. Know which services will break, where they're deployed, and in what order to roll out changes.
- **Refactoring confidence**: Rename a function. Delete dead code. Split a module. Do it with certainty, not fear.
- **Faster onboarding**: New team members see the dependency landscape immediately. No more "ask someone who knows the system."

### For AI Agents

- **Richer context**: When an AI agent is asked to change code, give it the dependency graph. It generates safer, more complete solutions.
- **Safer refactoring**: Agents can explore blast radius before proposing changes, avoiding surprises.
- **Accelerated code generation**: Rather than searching through codebases or running tests to find dependencies, the agent queries the graph in milliseconds.

## Current Status

### Implemented

- ✅ JSONL-based knowledge graph storage
- ✅ Extractors for Python and TypeScript/JavaScript
- ✅ Query tools: find-callers, find-callees, blast-radius, search-services, get-service-brief, get-event-producers, get-event-consumers
- ✅ MCP server for IDE and agent integration
- ✅ Evidence retrieval with file/line references
- ✅ Coverage metrics for extraction quality

### In Progress

- 🔄 Postgres + Apache AGE backend for production deployments
- 🔄 PR bot for real-time change-safety analysis
- 🔄 Multi-tenant support and organization-level insights

### Not Yet Included

- 🚫 Java, Go, or Rust extractors
- 🚫 GraphQL or gRPC schema extraction
- 🚫 Kubernetes or infrastructure as code analysis
- 🚫 Runtime traces and dynamic call graphs

## What's Next

You can go three directions from here:

1. **Start using SuperContext**: [Set up and build your first knowledge graph](../03-workflows/setup-and-first-kg.md)
2. **Understand the architecture**: [How SuperContext is designed and why](./architecture-overview.md)
3. **Extend SuperContext**: [Write extractors or add new tools](../02-core-features/knowledge-graph.md)

---

*Last updated: 2026-05-25*
