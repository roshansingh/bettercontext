# ADR-0006: Define the Product 1 Canonical Ontology and Fact Metadata Envelope

- **Status:** Accepted
- **Date:** 2026-04-30
- **Deciders:** Roshan Singh, Maruti Agarwal
- **Supersedes:** ADR-0004 follow-up item "Research and finalize the exact canonical entities and relations for v1"
- **Superseded by:** —

---

## Context

ADR-0004 closed the graph-building posture: Product 1 uses a strict canonical typed graph with a separate candidate / enrichment sidecar.

That ADR intentionally left the exact ontology open. We have now completed the ontology prior-art research, debate, and synthesis:

- `debates/2-2026-04-30-define-the-final-v1-canonical-ontology-f.md`
- `docs/ontology/ONTOLOGY-RECOMMENDATION.md`

This ADR closes the Product 1 v1 ontology and the shared Entity + Fact + Evidence metadata envelope.

## Decision

**Use the ontology defined in `docs/ontology/ONTOLOGY-RECOMMENDATION.md` as the binding Product 1 v1 canonical ontology.**

The binding shape is:

- **10 canonical node types:** `Service`, `Repo`, `Endpoint`, `Schema`, `EventChannel`, `EventMessage`, `Deployable`, `Deployment`, `Environment`, `Owner`
- **15 canonical relation types:** `OWNS`, `DEFINED_IN`, `IMPLEMENTS`, `PROVIDES_API`, `CONSUMES_API`, `CALLS`, `PRODUCES`, `CONSUMES`, `USES_SCHEMA`, `CARRIES`, `RUNS_SERVICE`, `RUNS_IN`, `INSTANCE_OF`, `DEPENDS_ON`, `EVOLVES_TO`
- **Per-node identity tuples**, all tenant-scoped
- **Per-kind URNs**, tenant-scoped by connection/session context
- **Entity + Fact + Evidence rows** as the source of truth
- **AGE nodes and edges as projections**, not the source of truth
- **Evidence-level `valid_from` / `valid_to`**
- **Qualified facts** via `facts.qualifier` for role-bearing relations such as `USES_SCHEMA`
- **Five derivation classes:** `authoritative_declared`, `manual_override`, `deterministic_static`, `runtime_observed`, `inferred_llm`
- **Coverage sidecar table** for known-empty / unknown / stale / partial coverage behavior
- **Per-edge promotion rules** and entity promotion rules
- **Explicit v1 deferrals** for non-MVP platform families such as docs, tickets, incidents, databases, feature flags, code symbols, and broad enterprise knowledge

## Relationship to Prior ADRs

### ADR-0003

ADR-0003 remains the storage decision: PostgreSQL + Apache AGE is the initial graph storage layer.

This ADR refines ADR-0003's schema sketch:

- normalized `entities`, `facts`, `evidence`, and `coverage` tables are the canonical source of truth
- AGE nodes and edges are materialized projections
- edge validity is derived from active evidence rows at projection time
- `valid_from` / `valid_to` live on `evidence`, not as independently managed edge-level source-of-truth columns

If ADR-0003 is read as implying edge-level `valid_from` / `valid_to` are primary storage fields, this ADR supersedes that detail.

### ADR-0004

ADR-0004 remains the graph-building posture: canonical typed graph plus candidate / enrichment sidecar.

This ADR closes ADR-0004's open ontology follow-up. Candidate entities and facts use the same Entity + Fact + Evidence shape with `canonical_status='candidate'`.

### ADR-0005

ADR-0005 remains the evidence retrieval decision.

Source-code-backed entity or fact evidence must carry `bytes_ref` compatible with ADR-0005 Mode A:

`repo + commit_sha + path + line_start + line_end`

## Implementation Guardrails

- Product surfaces query canonical entities/facts by default.
- Candidate entities/facts are visible only through explicit candidate / enrichment paths.
- No entity or fact becomes canonical from `inferred_llm` evidence alone.
- `DEPENDS_ON` is always derived from lower-level facts and must carry derivation evidence with source fact IDs and rule version.
- `USES_SCHEMA` must use `facts.qualifier.role`, not an overloaded evidence field.
- Hash-backed URNs are stable machine IDs; UI surfaces must render human-readable identity tuples as the primary display text.
- URNs are tenant-scoped, not globally unique across tenants.
- Partial coverage policy is owned by tool contracts. Safety-critical / completeness-sensitive tools must refuse unless missing scope is irrelevant to the requested answer.
- High-precision static `CALLS` promotion depends on the typed-client extractor allowlist in `docs/graph-building/TYPED-CLIENT-EXTRACTOR-ALLOWLIST.md`.

## Consequences

### Positive

- Product 1 has a concrete ontology ready for implementation.
- The graph can answer the eight Product 1 MCP tools without pulling in deferred platform scope.
- Provenance, freshness, confidence, promotion, and coverage are modeled consistently for both nodes and edges.
- Later platform entities can be added without replacing the Entity + Fact + Evidence substrate.

### Negative

- The model is more explicit than a simple property graph. Implementation must manage canonical storage and AGE projection separately.
- Tool contracts still need to define partial-coverage behavior per tool.
- The typed-client extractor allowlist must be maintained as implementation evolves.

### Neutral

- This ADR does not choose every source connector implementation.
- This ADR does not define the MCP tool schemas in full.
- This ADR does not make broad language indexing, SCIP, GraphRAG, docs, tickets, incidents, databases, or feature flags part of Product 1 v1.

## Implementation Status (as of 2026-05-16)

A first implementation slice in `source/` runs the ontology shape locally against Python and TypeScript/JavaScript repositories. The slice is intentionally narrower than the binding spec; the divergences below are tracked in `BACKLOG.md` and are not amendments to this ADR.

- **Substrate.** The current local harness writes JSONL files (`entities.jsonl`, `facts.jsonl`, `evidence.jsonl`, `coverage.jsonl`, `manifest.json`) in `data/kg_runs/`. Postgres + Apache AGE per ADR-0003 not yet wired.
- **Tenancy.** Tenant IDs resolve from explicit CLI/config input, then `SUPERCONTEXT_TENANT_ID`, then default to `"default"`. Full multi-tenant isolation is not implemented.
- **Extractors.** Deterministic Python AST extraction lives at `source.kg.languages.python.extractors.ast_extractor`; deterministic TypeScript/JavaScript compiler-API extraction lives at `source.kg.languages.typescript.extractors.compiler_api_extractor`. No catalog, manifest, contract-spec, or trace ingestion yet.
- **Code-level entity types introduced.** `CodeModule`, `CodeSymbol`, `ExternalPackage`, and `ExternalSymbol` are emitted by the current extractors. ADR-0006 §"Deferred families" listed `CodeSymbol` / `CodeOccurrence` as deferred. The implementation needs code-level entities to drive function-level `find_callers`, `find_callees`, runtime builtin-call, and `modules-importing` queries. **Status pending decision** — promote to canonical, keep candidate-only enrichment, or model as a sub-layer below the canonical 10. Tracked in `BACKLOG.md`.
- **`IMPORTS` relation introduced.** `CodeModule → ExternalPackage`. Not in the canonical 15. Same pending decision as above.
- **Authz support predicates introduced.** `HANDLES_ENDPOINT`, `DEFINES_AUTHZ_POLICY`, `APPLIES_AUTHZ_POLICY`, and `USES_AUTHZ_CHECK` are emitted as parser-backed support facts for endpoint authorization packets. They are not canonical Product 1 predicates; promotion or demotion remains pending with the code-level entity decision. Tracked in `BACKLOG.md`.
- **`code_risk_signal` support predicate introduced.** Emitted by the async-lifecycle (TS/JS) and swallowed-exception (Python) detectors as deterministic-static parser-backed support facts. Not a canonical Product 1 predicate; canonical status pending the same code-level entity decision.
- **`CALLS` grain.** Spec: `Service → Endpoint` (operation-level, cross-service). Current implementation: `CodeSymbol → CodeSymbol` (function-level, intra-repo). Both useful; the roll-up rule from function-level to Service-level CALLS for multi-service blast-radius is undefined. Tracked in `BACKLOG.md`.
- **Query surfaces.** The local CLI now exposes `lookup-symbol`, `symbols-in-file`, `evidence-for-call`, `who-imports`, `modules-importing-both`, `top-internal-dependencies`, `top-fan-in-symbols`, and `dependency-path` over the JSONL substrate. These are evaluation surfaces, not final MCP contracts.
- **URN scheme.** Spec §3: per-kind human-readable patterns (e.g., `supercontext://service/{namespace}/{slug}`); opaque hash only for kinds with unsafe URL characters (`Endpoint`, `EventChannel`). Current implementation uses `supercontext://{kind}/{stable_hash}` for **all** kinds. To be honored when MCP / UI surfaces ship.
- **Evidence `valid_from` / `valid_to`.** Spec mandates both columns on every evidence row. Current evidence rows have only `ingested_at`. Add when bitemporal or freshness queries land.
- **Promotion rules.** Spec §6: candidate→canonical via per-edge thresholds. Current extractor adapters still default `canonical_status='canonical'`, while query surfaces now respect `canonical_status` when candidate/unlinked rows are emitted. Full multi-source promotion rules are still pending.
- **Coverage row shape.** Spec §7 fields include `subject_id`, `last_seen_at`, `window_start`, `window_end`. Current coverage rows collapse to `tenant_id, predicate, scope_ref, state, source_system, checked_at`. Restore the full shape when Tool Query Contract ADR locks coverage semantics.
- **Polyglot ingestion.** Current extraction handles Python and TypeScript/JavaScript deeply, plus limited unsupported-language coverage rows for inventoried source languages and known stacks. Arbitrary unknown extensions still do not get full loud-refusal coverage.

The current local slice is sufficient to validate the ontology shape against real codebases (summarized in `docs/evaluation/CANONICAL-VALIDATION-REPORT.md`). Each gap above carries a revisit trigger in `BACKLOG.md`.

## References

- `docs/ontology/ONTOLOGY-RECOMMENDATION.md`
- `debates/2-2026-04-30-define-the-final-v1-canonical-ontology-f.md`
- `docs/graph-building/TYPED-CLIENT-EXTRACTOR-ALLOWLIST.md`
- ADR-0003: `adr/0003-postgres-age-as-initial-graph-storage.md`
- ADR-0004: `adr/0004-canonical-graph-plus-candidate-enrichment-sidecar.md`
- ADR-0005: `adr/0005-modular-evidence-retrieval-with-coordinate-fetch-and-selective-ladder.md`
- `PRD.md` §6.1, §6.2, §7, §9
- `PLATFORM-PRD.md` §8, §10, §11
