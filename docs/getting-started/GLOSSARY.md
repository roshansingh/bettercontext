# Glossary — SuperContext Key Terms

**Last updated**: 2026-05-25

Quick reference for SuperContext terminology. Each term links to its full documentation. Terms are organized by concept area.

---

## Core Concepts

**Artifact**
A generated output from the KG build process: a snapshot directory containing `entities.jsonl`, `facts.jsonl`, `evidence.jsonl`, `coverage.jsonl`, and `manifest.json`. (See [Setup and First KG](./03-workflows/setup-and-first-kg.md))

**Canonical**
Status of an entity or fact that is authoritative and safe for product decisions. Canonical facts come from deterministic extractors, manual overrides, or promoted candidates. (See [Knowledge Graph Explained](./02-core-features/knowledge-graph.md) and `adr/0006-canonical-ontology-and-fact-metadata-envelope.md`)

**Candidate**
An entity or fact with `canonical_status='candidate'` — inferred by LLM, extracted with lower confidence, or pending promotion. Candidate data is available but not surfaced by default in queries. (See `adr/0004-canonical-graph-plus-candidate-enrichment-sidecar.md`)

**Change Safety**
The core SuperContext value: the ability to predict before merging what other services will break when you change a service, endpoint, or schema. (See [What is SuperContext](./01-concepts/what-is-supercontext.md))

**Coverage**
A sidecar table tracking the completeness and instrumentation status of the knowledge graph. Records whether a scope (repo, language, path prefix) is fully indexed, partially indexed, uninstrumented, or stale. (See `docs/ontology/ONTOLOGY-RECOMMENDATION.md` and `COVERAGE-METRICS.md`)

**Derivation Class**
One of five confidence levels for how facts were derived: `authoritative_declared`, `manual_override`, `deterministic_static`, `runtime_observed`, or `inferred_llm`. Determines whether a fact can be promoted to canonical. (See `adr/0006-canonical-ontology-and-fact-metadata-envelope.md`)

**Entity**
A node in the knowledge graph: a real-world thing such as a `Service`, `Repo`, `Endpoint`, `Schema`, `EventChannel`, `Deployment`, `Environment`, or `Owner`. (See [Knowledge Graph Explained](./02-core-features/knowledge-graph.md))

**Evidence**
Source code bytes or metadata proving an entity or fact exists. Carries `bytes_ref` (repo, commit_sha, path, line_start, line_end) for code-backed claims per ADR-0005 Mode A. Multiple evidence rows can back one canonical entity or fact. (See `adr/0005-modular-evidence-retrieval-with-coordinate-fetch-and-selective-ladder.md`)

**Extractor**
A language-specific parser (Python AST or TypeScript compiler API) that analyzes source code and emits entities and facts into the knowledge graph. (See `docs/graph-building/GRAPH-BUILDING-RECOMMENDATION.md`)

**Fact**
An edge (relation) in the knowledge graph connecting two entities: e.g., "Service A calls Endpoint B" or "Repository defines Schema C". Facts have type (CALLS, PRODUCES, CONSUMES, etc.) and optional qualifiers. (See [Knowledge Graph Explained](./02-core-features/knowledge-graph.md))

**Knowledge Graph (KG)**
A typed, queryable graph of code entities (services, repos, endpoints, schemas) and their relationships (calls, produces, consumes, depends-on). Enables change-safety analysis. (See [Knowledge Graph Explained](./02-core-features/knowledge-graph.md))

**MCP (Model Context Protocol)**
SuperContext's public interface for tools and agents. Exposes eight query tools (`search_services`, `get_service_brief`, `find_callers`, `find_callees`, `get_event_consumers`, `get_event_producers`, `blast_radius`, `deploy_blockers_for`) via streamable HTTP + OAuth 2.1. (See [MCP Integration](./02-core-features/mcp-integration.md) and `adr/0002-mcp-protocol-for-external-surface.md`)

**Ontology**
The canonical schema of the knowledge graph: 10 entity types, 15 relation types, identity tuples, promotion rules, and evidence structure. Binding specification in `docs/ontology/ONTOLOGY-RECOMMENDATION.md` and `adr/0006-canonical-ontology-and-fact-metadata-envelope.md`. (See [Architecture Overview](./01-concepts/architecture-overview.md))

**Snapshot**
A point-in-time artifact containing the built knowledge graph for one or more repositories. Written to `data/kg_runs/<name>/` and contains entities, facts, evidence, coverage, and manifest files. (See [Setup and First KG](./03-workflows/setup-and-first-kg.md))

**Tenant**
One customer organization. Provides hard boundary for multi-tenancy: each tenant has isolated service, repo, endpoint, and owner namespaces. URNs and identity tuples are tenant-scoped. (See `docs/ontology/ONTOLOGY-RECOMMENDATION.md`)

**URN (Uniform Resource Name)**
Stable machine identifier for an entity, scoped to tenant. Format varies by kind: `supercontext://service/{namespace}/{slug}`, `supercontext://endpoint/{protocol}/{hash}`, etc. Not globally unique across tenants. (See `docs/ontology/ONTOLOGY-RECOMMENDATION.md`)

---

## Query and Analysis

**Blast Radius**
Impact analysis query: shows all downstream code, services, or operations affected by changing a given symbol, endpoint, or service. Answers "what breaks if I change X?" (See [Query Your Repo](./03-workflows/query-your-repo.md))

**Call Graph**
Directed graph of function/method calls within and across repositories. Backing structure for `find-callers`, `find-callees`, and `blast-radius` queries. (See [Query Your Repo](./03-workflows/query-your-repo.md))

**Cross-Repo Link**
Fact that connects entities across repository boundaries: e.g., Service A in repo X calls Service B in repo Y. Enables multi-service and multi-repo change-safety analysis. (See [Query Your Repo](./03-workflows/query-your-repo.md))

**Query**
A deterministic, parameterized operation over the knowledge graph. SuperContext CLI exposes queries like `find-callers <symbol>`, `blast-radius <symbol> --depth N`, `dependency-info <package>`, etc. (See [Query Your Repo](./03-workflows/query-your-repo.md))

---

## Implementation Details

**Bytes Ref**
A coordinate tuple `(repo, commit_sha, path, line_start, line_end)` pinning evidence to exact source code bytes. Required for ADR-0005 Mode A evidence retrieval. Enables "click through to the proof" without re-parsing. (See `adr/0005-modular-evidence-retrieval-with-coordinate-fetch-and-selective-ladder.md`)

**Derivation**
The process and audit trail explaining how a fact was derived. Tracks source fact IDs, rule version, and confidence level. Required for promoted facts (e.g., derived `DEPENDS_ON`). (See `adr/0006-canonical-ontology-and-fact-metadata-envelope.md`)

**Fixture**
A test repository with known code patterns, service boundaries, and expected facts used to validate extractor behavior. Ensures extractors work across varied real-world codebases. (See `docs/evaluation/PRODUCT-QUERY-SET.md`)

**JSONL (JSON Lines)**
Line-delimited JSON format used for snapshot storage: each line is one entity, fact, evidence, or coverage record. Local storage format; Postgres + AGE is the target production schema. (See [Setup and First KG](./03-workflows/setup-and-first-kg.md))

**Mode A (Evidence Retrieval)**
ADR-0005 strategy: commit-pinned bytes fetched on-demand via `go-git`/`pygit2`. Always-on for facts surfaced in queries. Deterministic and auditable. (See `adr/0005-modular-evidence-retrieval-with-coordinate-fetch-and-selective-ladder.md`)

**Mode B (Evidence Retrieval)**
ADR-0005 strategy: selective ladder falling back through ripgrep → AST grep → Claude Explorer subagent when source bytes are unavailable or ambiguous. Used for enrichment and candidate facts. (See `adr/0005-modular-evidence-retrieval-with-coordinate-fetch-and-selective-ladder.md`)

---

## Architecture Decisions (ADRs)

**ADR-0001: Claude Agent SDK for Internal Runtime**
Internal runtime for both ingestion (Layer A) and server-side reasoning (Layer B) uses Claude Agent SDK; Layer C is the IDE. (See `adr/0001-claude-agent-sdk-for-internal-runtime.md`)

**ADR-0002: MCP Protocol for External Surface**
Public protocol is MCP with eight tools: `search_services`, `get_service_brief`, `find_callers`, `find_callees`, `get_event_consumers`, `get_event_producers`, `blast_radius`, `deploy_blockers_for`. Streamable HTTP + OAuth 2.1. (See `adr/0002-mcp-protocol-for-external-surface.md`)

**ADR-0003: PostgreSQL + Apache AGE as Graph Storage**
Storage is PostgreSQL with Apache AGE for graph queries. Postgres tables = source of truth; AGE = projection. (See `adr/0003-postgres-age-as-initial-graph-storage.md`)

**ADR-0004: Canonical Graph + Candidate Enrichment Sidecar**
Two-tier graph: canonical (high-trust, deterministic, authoritative) + candidate sidecar (LLM-inferred, prose-derived, ambiguous). (See `adr/0004-canonical-graph-plus-candidate-enrichment-sidecar.md`)

**ADR-0005: Modular Evidence Retrieval (Mode A + Mode B)**
Evidence retrieval: Mode A (commit-pinned bytes via go-git/pygit2, always-on) + Mode B (selective ladder: ripgrep → ast-grep → Claude Explorer, for enrichment). (See `adr/0005-modular-evidence-retrieval-with-coordinate-fetch-and-selective-ladder.md`)

**ADR-0006: Canonical Ontology and Fact Metadata Envelope**
Binding ontology: 10 canonical node types, 15 relation types, per-node identity tuples, PROV-O Entity + Fact + Evidence shape, 5 derivation classes, per-entity/per-edge promotion rules, and sidecar `coverage` table. (See `adr/0006-canonical-ontology-and-fact-metadata-envelope.md`)

---

## Quick Commands Reference

| Task | Command | Description |
|------|---------|-------------|
| Build KG | `python -m source.scripts.build_kg --repo /path/to/repo --out data/kg_runs/my-repo` | Build knowledge graph from a single repository |
| Build multi-repo KG | `python -m source.scripts.build_multi_kg --repo /path/to/repo-1 --repo /path/to/repo-2 --out data/kg_runs/multi-repo` | Build KG from multiple repositories for fleet analysis |
| KG summary | `python -m source.scripts.query_kg --snapshot data/kg_runs/my-repo summary` | View entity and fact counts |
| Find callers | `python -m source.scripts.query_kg --snapshot data/kg_runs/my-repo find-callers module.function --limit 5` | Find all code locations calling a function |
| Find callees | `python -m source.scripts.query_kg --snapshot data/kg_runs/my-repo find-callees module.function --limit 5` | Find functions called by a symbol |
| Blast radius | `python -m source.scripts.query_kg --snapshot data/kg_runs/my-repo blast-radius module.function --depth 2` | Find downstream impact of changing a symbol |
| Module imports | `python -m source.scripts.query_kg --snapshot data/kg_runs/my-repo modules-importing package-name --limit 5` | Find modules that import a package |
| Top dependencies | `python -m source.scripts.query_kg --snapshot data/kg_runs/my-repo top-dependencies --limit 10` | Find most-used external packages |
| Dependency info | `python -m source.scripts.query_kg --snapshot data/kg_runs/my-repo dependency-info package-name` | Get version and usage details for a package |
| Coverage metrics | `python -m source.scripts.coverage_metrics --snapshot data/kg_runs/my-repo --expected-repos 1` | Compute KG coverage metrics |
| Coverage report | `python -m source.scripts.coverage_report --snapshot data/kg_runs/my-repo --out docs/evaluation/runs/my-run --run-id my-run-2026-05-25 --tenant my-org --expected-repos 1 --metric-config source/kg/metrics/config.yaml` | Generate detailed coverage report |
| Start MCP server | `python -m source.mcp.server --snapshot data/kg_runs/my-repo` | Start local MCP server for development |

---

## See Also

- **[What is SuperContext](./01-concepts/what-is-supercontext.md)** — Value proposition and core concepts
- **[Architecture Overview](./01-concepts/architecture-overview.md)** — System design and layers
- **[Knowledge Graph Explained](./02-core-features/knowledge-graph.md)** — Entity types, relations, and evidence model
- **[MCP Integration](./02-core-features/mcp-integration.md)** — Using the public protocol
- **[Setup and First KG](./03-workflows/setup-and-first-kg.md)** — Installation and first build
- **[Query Your Repo](./03-workflows/query-your-repo.md)** — Query syntax and examples
- **[Evaluate Coverage](./03-workflows/evaluate-coverage.md)** — Coverage assessment
- **[ADR Index](../../adr/README.md)** — All architecture decisions
- **[PRODUCT-QUERY-SET.md](../../docs/evaluation/PRODUCT-QUERY-SET.md)** — Acceptance corpus
