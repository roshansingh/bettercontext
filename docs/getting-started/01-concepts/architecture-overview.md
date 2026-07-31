# Architecture Overview

**A 15-minute read.** SuperContext is built as a three-layer system: **Ingestion** (extract facts from code), **Storage** (persist typed graph data), and **Querying** (answer dependency questions). This document explains each layer, the data model underneath, and how evidence grounds every claim.

For deeper context on design decisions, see [ADR-0001](../../../adr/0001-claude-agent-sdk-for-internal-runtime.md) through [ADR-0006](../../../adr/0006-canonical-ontology-and-fact-metadata-envelope.md).

---

## The Three Layers

### Layer 1: Ingestion

**Job:** Read codebases, extract facts, emit verifiable evidence.

Ingestion analyzes your repositories using **language-specific extractors**. Each extractor parses the abstract syntax tree (AST) or compiler API to discover:

- **Functions and modules**: Every function, class, method, and module boundary
- **Dependencies**: Which modules import which packages, and which functions call other functions
- **APIs and events**: HTTP routes, gRPC services, event topics, and their consumers
- **Data flows**: Message shapes, schema usage, and event producers

#### Extractors

SuperContext ships with deterministic extractors for **Python** and **TypeScript/JavaScript**. Each extractor is built on the language's native tooling:

- **Python**: AST-based extraction at `source.kg.languages.python.extractors.ast_extractor`. Discovers modules, functions, imports, and calls by walking the syntax tree.
- **TypeScript/JavaScript**: Compiler API extraction at `source.kg.languages.typescript.extractors.compiler_api_extractor`. Uses the TypeScript compiler to resolve symbols and track dependencies across files.

Other languages (Java, Go, Rust) are not yet supported; attempting to ingest them emits a **coverage** record with `state='uninstrumented'` so you know what the graph doesn't cover.

#### Evidence

Every extracted fact carries **evidence**: the commit SHA, file path, and line numbers. This means:

```json
{
  "bytes_ref": {
    "repo": "https://github.com/org/service-a",
    "commit_sha": "fc058ef51a8c952aee6945d46e1e9585d11ce145",
    "path": "src/payments.py",
    "line_start": 42,
    "line_end": 48
  }
}
```

Evidence is immutable and commit-pinned. If you ask "who calls `process_payment()`?" the answer includes the exact line where that call lives, and you can retrieve those bytes by commit hash.

#### Input/Output

**Input**: A file path to a Git repository (local or SSH).

**Output**: Five JSONL files written to `data/kg_runs/<snapshot-name>/`:
- `entities.jsonl` — Objects in the graph (functions, modules, services, endpoints)
- `facts.jsonl` — Relations between entities (calls, imports, hosts)
- `evidence.jsonl` — Proof that each fact is real (commit hash, file, line range)
- `coverage.jsonl` — Gaps in extraction (languages not supported, repos not seen)
- `manifest.json` — Metadata about the run (timestamp, repos, entity/fact counts)

#### Current Coverage

| Language | Status | Notes |
|----------|--------|-------|
| Python | ✅ Supported | Full AST coverage: imports, calls, definitions |
| TypeScript / JavaScript | ✅ Supported | Compiler API coverage: imports, calls, exports |
| Java | ❌ Not supported | Emits `uninstrumented` coverage record |
| Go | ❌ Not supported | Emits `uninstrumented` coverage record |
| Rust | ❌ Not supported | Emits `uninstrumented` coverage record |

### Layer 2: Storage

**Job:** Persist typed facts, entities, and evidence in queryable form.

The storage layer saves ingestion results as a **typed knowledge graph**. The current implementation uses **JSONL files** (local snapshots); the final production system uses **PostgreSQL + Apache AGE**.

#### The Five Core Files

Every knowledge graph snapshot contains five files:

1. **entities.jsonl**: Entities (nodes)
   ```json
   {
     "entity_id": "ent_b3bf56389d163a1cd43a50ca",
     "kind": "CodeSymbol",
     "identity": {
       "tenant_id": "default",
       "name": "process_payment",
       "module": "payments"
     },
     "urn": "supercontext://symbol/a1b2c3d4e5f6",
     "canonical_status": "canonical",
     "properties": {
       "commit_sha": "fc058ef51a8c952aee6945d46e1e9585d11ce145",
       "path": "src/payments.py",
       "line_start": 42,
       "line_end": 48
     }
   }
   ```

2. **facts.jsonl**: Relations (edges)
   ```json
   {
     "fact_id": "fact_01f468a339f230affa1aadb0",
     "subject_id": "ent_6f0d96151b110889167a0175",
     "predicate": "CALLS",
     "object_id": "ent_b3bf56389d163a1cd43a50ca",
     "canonical_status": "canonical",
     "qualifier": {}
   }
   ```

3. **evidence.jsonl**: Proof
   ```json
   {
     "evidence_id": "ev_2b85f161b9be4c8ed4eaf92a",
     "target_id": "fact_01f468a339f230affa1aadb0",
     "target_type": "fact",
     "source_system": "python_ast_extractor",
     "derivation_class": "deterministic_static",
     "bytes_ref": {
       "repo": "https://github.com/org/service-a",
       "commit_sha": "fc058ef51a8c952aee6945d46e1e9585d11ce145",
       "path": "src/invoice_gen.py",
       "line_start": 15,
       "line_end": 15
     },
     "confidence": 1.0,
     "ingested_at": "2026-05-25T10:30:00+00:00"
   }
   ```

4. **coverage.jsonl**: What we didn't see
   ```json
   {
     "scope_ref": {
       "repo": "https://github.com/org/rust-service",
       "language": "rust"
     },
     "state": "uninstrumented",
     "source_system": "ingestion",
     "checked_at": "2026-05-25T10:30:00+00:00"
   }
   ```

5. **manifest.json**: Run metadata
   ```json
   {
     "run_id": "kg_run_2026-05-25_103000",
     "timestamp": "2026-05-25T10:30:00+00:00",
     "repos_ingested": 3,
     "entity_count": 1250,
     "fact_count": 4800,
     "evidence_count": 4800,
     "coverage_records": 5
   }
   ```

#### Derivation Tiers (Confidence Levels)

Every fact and entity carries a **derivation class** that indicates how confident we are in it:

1. **authoritative_declared** — Explicit declarations (manifest files, config files, schemas). Always canonical.
2. **manual_override** — Human corrections to the graph. Always canonical.
3. **deterministic_static** — Code extraction (AST parsing, import analysis). Always canonical in current implementation.
4. **runtime_observed** — Traces from running code. Requires promotion rules before canonical.
5. **inferred_llm** — LLM-inferred facts. Requires promotion rules before canonical.

#### Canonical vs. Candidate

Every entity and fact is marked either **canonical** or **candidate**:

- **Canonical**: High-confidence facts that appear in query results by default.
- **Candidate**: Inferred or enriched facts that are visible only on explicit request.

In the current local implementation, extractors emit only canonical facts (since they are deterministic). When multi-source evidence or LLM inference enter the graph, promotion rules determine when candidate facts become canonical.

#### Entity Types (10 Canonical)

The knowledge graph understands these node types:

| Type | Example | Purpose |
|------|---------|---------|
| **Service** | `pricing-service` | A deployed unit or microservice |
| **Repo** | `github.com/org/pricing-service` | A Git repository |
| **Endpoint** | `POST /api/v1/charges` | An HTTP or gRPC route |
| **EventChannel** | `payments.order_created` | A message topic or queue |
| **EventMessage** | `OrderCreatedEvent` | A message schema or type |
| **Schema** | OpenAPI spec, protobuf | A data structure or interface definition |
| **Deployable** | A container, artifact | An artifact that can be deployed |
| **Deployment** | A running instance | A live service instance in an environment |
| **Environment** | `production`, `staging` | A deployment target |
| **Owner** | A team or person | Responsible party for a service |

#### Relation Types (15 Canonical)

The graph tracks these connection types:

| Relation | Subject → Object | Purpose |
|----------|------------------|---------|
| **OWNS** | Owner → Service | Team owns service |
| **DEFINED_IN** | Entity → Repo | Entity lives in repo |
| **IMPLEMENTS** | Service → Endpoint | Service exposes endpoint |
| **PROVIDES_API** | Service → EventChannel | Service publishes to topic |
| **CONSUMES_API** | Service → EventChannel | Service subscribes to topic |
| **CALLS** | Service/Symbol → Service/Symbol | Direct invocation |
| **PRODUCES** | Service → EventMessage | Service emits message |
| **CONSUMES** | Service → EventMessage | Service consumes message |
| **USES_SCHEMA** | Service/Endpoint → Schema | Uses data structure |
| **CARRIES** | EventMessage → Schema | Message contains schema |
| **RUNS_SERVICE** | Deployment → Service | Deployment hosts service |
| **RUNS_IN** | Deployment → Environment | Deployment lives in environment |
| **INSTANCE_OF** | Deployment → Deployable | Deployment instantiates artifact |
| **DEPENDS_ON** | Service → Service | Transitive dependency |
| **EVOLVES_TO** | Schema → Schema | Schema migration path |

For full definitions and motivation, see [ADR-0006](../../../adr/0006-canonical-ontology-and-fact-metadata-envelope.md).

### Layer 3: Querying

**Job:** Answer dependency questions in milliseconds using the stored graph.

The query engine exposes **eight standard tools** that cover the most common change-safety questions. These tools are available as command-line commands and as an **MCP server** for IDE and agent integration.

#### The Eight Tools

| Tool | Query | Returns |
|------|-------|---------|
| **search-services** | Find services by name or property | Service names, owners, endpoints |
| **get-service-brief** | What does this service do? | Service info, dependencies, endpoints, hosted events |
| **find-callers** | Who calls this function? | Symbols/services that invoke the target |
| **find-callees** | What does this function call? | Downstream symbols/services |
| **blast-radius** | What breaks if I change this? | Transitive dependents (direct + indirect) |
| **get-event-producers** | Who publishes to this topic? | Services and symbols that emit to the channel |
| **get-event-consumers** | Who subscribes to this topic? | Services and symbols that consume from the channel |
| **deploy-blockers-for** | What must deploy first? | Services that must be deployed before this one |

#### Query Engine Flow

Every query follows the same pattern:

1. **Parse** — Normalize the query (symbol name, service slug, etc.)
2. **Filter** — Load matching entities from the graph
3. **Traverse** — Walk relations to collect related entities
4. **Rank** — Sort results by relevance or dependency order
5. **Gather Evidence** — Fetch commit-pinned bytes for each fact (Mode A or Mode B)
6. **Return** — Format for MCP, CLI, or IDE

Example: **find-callers** for a Python function
```text
Query: "find-callers src.payments.process_payment"
→ Parse: { kind: "CodeSymbol", name: "process_payment", module: "payments" }
→ Filter: Load entity_id for that symbol
→ Traverse: Find all CALLS facts where object_id matches
→ Gather Evidence: Fetch bytes for each caller (file + line range)
→ Return: [ { symbol: "...", file: "...", line: N }, ... ]
```

---

## Data Model

Every fact in the knowledge graph has this structure:

```json
{
  "fact_id": "fact_01f468a339f230affa1aadb0",
  "subject_id": "ent_6f0d96151b110889167a0175",
  "predicate": "CALLS",
  "object_id": "ent_b3bf56389d163a1cd43a50ca",
  "canonical_status": "canonical",
  "derivation_class": "deterministic_static",
  "qualifier": {
    "role": "required",
    "context": "production"
  },
  "evidence": [
    {
      "evidence_id": "ev_2b85f161b9be4c8ed4eaf92a",
      "source_system": "python_ast_extractor",
      "derivation_class": "deterministic_static",
      "bytes_ref": {
        "repo": "https://github.com/org/service-a",
        "commit_sha": "fc058ef51a8c952aee6945d46e1e9585d11ce145",
        "path": "src/invoice_gen.py",
        "line_start": 15,
        "line_end": 15
      },
      "confidence": 1.0,
      "ingested_at": "2026-05-25T10:30:00+00:00"
    }
  ]
}
```

#### Field Descriptions

- **fact_id**: Unique identifier for this fact
- **subject_id**: Entity ID of the actor (who performs the action)
- **predicate**: Type of relation (CALLS, IMPORTS, HOSTS, etc.)
- **object_id**: Entity ID of the target (what is affected)
- **canonical_status**: `"canonical"` (default, visible) or `"candidate"` (inferred, opt-in)
- **derivation_class**: How we know this fact (see Derivation Tiers above)
- **qualifier**: Optional role or context (e.g., `role: "optional"` for weak dependencies)
- **evidence**: Array of `evidence_id`s linking to the proof file

#### Evidence Structure

An evidence record proves that a fact exists:

```json
{
  "evidence_id": "ev_2b85f161b9be4c8ed4eaf92a",
  "target_id": "fact_01f468a339f230affa1aadb0",
  "target_type": "fact",
  "source_system": "python_ast_extractor",
  "source_ref": {
    "commit_sha": "fc058ef51a8c952aee6945d46e1e9585d11ce145",
    "repo_path": "/Users/roshan/work/code/org/service-a"
  },
  "bytes_ref": {
    "repo": "https://github.com/org/service-a",
    "commit_sha": "fc058ef51a8c952aee6945d46e1e9585d11ce145",
    "path": "src/invoice_gen.py",
    "line_start": 15,
    "line_end": 15
  },
  "confidence": 1.0,
  "derivation_class": "deterministic_static",
  "ingested_at": "2026-05-25T10:30:00+00:00"
}
```

The `bytes_ref` triple `(commit_sha, path, line_start, line_end)` allows **Mode A evidence retrieval**: fetch the exact bytes from Git using the commit hash, so the claim is always verifiable.

---

## Evidence: Two Modes

SuperContext retrieves evidence in two modes, each serving different needs.

### Mode A: Commit-Pinned Coordinate Fetch

**When**: Always for surfaced facts, safety-critical claims, and blast-radius queries.

**How**: Fetch raw bytes from Git using the immutable coordinate:
```text
repo + commit_sha + path + line_start + line_end
```

**Why**: Answers are verifiable. Click a result and see the exact line of code that established the dependency. No guessing, no staleness, no drift from HEAD.

**Example**:
```text
Query Result:
  "find-callers fetch_user"
  → symbol: "auth_service.fetch_user"
    called_by: "payment_service.charge_order"
    at: "https://github.com/org/payment-service/blob/fc058ef/src/billing.py#L42"
    bytes: (from commit fc058ef, file src/billing.py, line 42)
```

### Mode B: Selective Retrieval Ladder

**When**: Searching for evidence, cross-repo traces, or filling gaps in the graph.

**How**: Use a three-rung ladder, stopping at the first result:

1. **Lexical search** (ripgrep)
   - Fast, exact string/symbol matching across the codebase
   - Used for: function names, API routes, event topic names, config keys

2. **Structural search** (ast-grep over tree-sitter)
   - Pattern-based AST matching
   - Used for: framework patterns, specific language syntax (decorators, annotations)
   - Narrowly applied, not default coverage

3. **Agentic exploration** (Claude Agent SDK)
   - Bounded AI reasoning with Glob, Grep, and Read tools
   - Used for: ambiguous names, cross-repo reasoning, uninstrumented languages
   - Requires explicit budget limits

**Why**: Balances speed (lexical first) with accuracy (AST when needed) and reasoning (agent for hard cases).

---

## Flow Diagram

Here's how a code change flows through SuperContext:

```
┌─────────────────────────────────────────────────────────────────┐
│ Layer 1: INGESTION                                              │
├─────────────────────────────────────────────────────────────────┤
│                                                                   │
│  Git Repos  →  [Language-Specific Extractors]  →  Facts+Evidence│
│                 • Python AST                                     │
│                 • TypeScript Compiler API                        │
│                 • (Java, Go, Rust not yet)                       │
│                                                                   │
│  Output: entities.jsonl, facts.jsonl, evidence.jsonl             │
│                                                                   │
└─────────────────────────────────────────────────────────────────┘
                           ↓
┌─────────────────────────────────────────────────────────────────┐
│ Layer 2: STORAGE                                                │
├─────────────────────────────────────────────────────────────────┤
│                                                                   │
│  ┌────────────────┬─────────────┬──────────────┐                 │
│  │ Entities       │ Facts       │ Evidence     │                 │
│  │ (10 types)     │ (15 types)  │ (with bytes) │                 │
│  │ canonical-only │ canonical + │ commit-      │                 │
│  │ or candidate   │ candidate   │ pinned       │                 │
│  └────────────────┴─────────────┴──────────────┘                 │
│                                                                   │
│  Coverage: { state, scope, language }                            │
│  Manifest: { run_id, timestamp, entity_count, ... }             │
│                                                                   │
│  Storage: JSONL files (local) or Postgres+AGE (production)      │
│                                                                   │
└─────────────────────────────────────────────────────────────────┘
                           ↓
┌─────────────────────────────────────────────────────────────────┐
│ Layer 3: QUERYING                                               │
├─────────────────────────────────────────────────────────────────┤
│                                                                   │
│  Eight Standard Tools                                            │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │ search-services, get-service-brief, find-callers,        │   │
│  │ find-callees, blast-radius, get-event-producers,         │   │
│  │ get-event-consumers, deploy-blockers-for                 │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                   │
│  Query Engine:                                                   │
│    Parse → Filter → Traverse → Rank → Gather Evidence → Return  │
│                                                                   │
│  Evidence Retrieval:                                             │
│    Mode A (commit-pinned, always)                                │
│    Mode B (selective ladder: ripgrep → AST → Claude agent)       │
│                                                                   │
└─────────────────────────────────────────────────────────────────┘
                           ↓
┌─────────────────────────────────────────────────────────────────┐
│ Layer 4: INTEGRATION                                            │
├─────────────────────────────────────────────────────────────────┤
│                                                                   │
│  MCP Server → IDEs, editors, agent frameworks                    │
│  CLI Tools  → Local exploration and debugging                    │
│  HTTP API   → Custom integrations                                │
│                                                                   │
└─────────────────────────────────────────────────────────────────┘
```

---

## Design Principles

SuperContext is built on five core principles:

1. **Deterministic-first**: Extract facts via AST parsing and compiler analysis before using LLM inference. Deterministic output is always canonical.

2. **Evidence-backed**: Every fact carries commit-pinned evidence. If we can't cite it, we don't claim it.

3. **Tenant-scoped**: Graph entities and relations are scoped to a tenant. Multi-tenant isolation is enforced at query and storage time.

4. **Queryable**: The graph answers a fixed set of eight queries well rather than trying to answer every possible question. This keeps the system focused and predictable.

5. **Extensible**: New entity types, relation types, and queries can be added without replacing the core Entity + Fact + Evidence substrate.

---

## How This Relates to ADRs

The SuperContext architecture is guided by architecture decision records (ADRs). Here's the key ones:

- **[ADR-0001](../../../adr/0001-claude-agent-sdk-for-internal-runtime.md)**: Internal runtime uses Claude Agent SDK for both ingestion and reasoning.
- **[ADR-0002](../../../adr/0002-mcp-protocol-for-external-surface.md)**: Public protocol is MCP with eight tools; supports OAuth 2.1 and streamable HTTP.
- **[ADR-0003](../../../adr/0003-postgres-age-as-initial-graph-storage.md)**: Storage is PostgreSQL + Apache AGE (current implementation uses JSONL as intermediate).
- **[ADR-0004](../../../adr/0004-canonical-graph-plus-candidate-enrichment-sidecar.md)**: Graph has canonical high-trust facts + candidate enrichment sidecar.
- **[ADR-0005](../../../adr/0005-modular-evidence-retrieval-with-coordinate-fetch-and-selective-ladder.md)**: Evidence retrieval in two modes: commit-pinned (Mode A) + selective ladder (Mode B).
- **[ADR-0006](../../../adr/0006-canonical-ontology-and-fact-metadata-envelope.md)**: 10 entity types + 15 relation types, all tenant-scoped. Five derivation classes drive confidence.

---

## What's Next

- **[Set up and build your first knowledge graph](../03-workflows/setup-and-first-kg.md)** — Hands-on guide to using SuperContext with your codebase.
- **[Knowledge Graph Explained](../02-core-features/knowledge-graph.md)** — Deep dive into building and querying the graph.
- **[ADRs](../../../adr/)** — Full architectural decisions and design debates.

---

*Last updated: 2026-05-25*
