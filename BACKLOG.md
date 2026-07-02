# SuperContext Backlog

Status: living index of deferred work and open follow-ups across the project.
Last updated: 2026-06-20.

This file is the single place to scan "what's deferred and why." Per-ADR open-follow-up sections are the authoritative source; this is the index. Refresh when ADRs change.

Format: `Item | Source | Trigger to revisit`.

---

## Next ADRs

| Item | Source | Trigger to revisit |
|---|---|---|
| Tool Query Contract ADR — semantics for the 8 MCP tools, partial coverage rules, pagination, refusal metadata | Architecture backlog | Before Source Connector ADR; needed to drive what facts the engine must serve |
| Source Connector + Extractor ADR — exact v1 inputs, parser stack, typed-client allowlist ownership | Architecture backlog | After Tool Query Contract ADR locks the fact requirements |
| Deployment / Auth / Tenancy ADR — SaaS vs self-hosted boundary, tenant isolation, SSO/SCIM, secrets, audit | Architecture backlog | Parallel to Source Connector ADR |
| Testing / Evaluation ADR — golden graphs, evidence replay, graph/evidence merge tests, p95 benchmarks | Architecture backlog | After Tool Query Contract ADR locks the semantics to test against |
| AGE Projection / Materialization Runtime ADR — projection cadence, incremental vs full rebuild, bulk-write strategy | Architecture backlog | Before scale benchmarks force the choice |

## Implementation discipline (cross-cutting)

| Item | Source | Trigger to revisit |
|---|---|---|
| Loud refusal at ingestion — when Layer A encounters a source scope with no allowlisted extractor, emit `coverage` row with `state='uninstrumented'` for the extractor's declared scope, such as repo, service, language/framework, or path prefix. Never silent-skip | This document, design discussion 2026-05-01 | Land as a hard rule in Source Connector + Extractor ADR |
| `.supercontext/config.json` per-repo customer steering (identity overrides, manual relations, coverage hints, deferred-extractor opt-out) | claude-deepwiki-analysis.md §3 | Source Connector + Extractor ADR |
| Typed-client allowlist seeding — pick first design partner's stack, allowlist their typed clients (TS/JS, Go, Java/Kotlin per PRD) | TYPED-CLIENT-EXTRACTOR-ALLOWLIST.md "Initial Entries" | When first design partner signed |
| MCP install one-liner pattern documented (mirror `claude mcp add -s user -t http supercontext https://...`) | claude-deepwiki-analysis.md §2 | Tool Query Contract ADR or MCP install docs |
| Mermaid in PR-bot blast-radius comments | claude-deepwiki-analysis.md §7 | Future PR-bot ADR |
| Move private validation fixtures out of product source — `source/kg/product/validation_report.py` and `scenario_plans.py` currently include private corpus strings such as `api.shopagain.io`, `/api/token`, `la-prod-email`, `la-prod-campaign-messages`, `prod_shopagain_wsgi.py`, `mercury_api`, and `mercury_campaign_messages`; replace in-tree defaults with a public reference corpus and keep private checks under `examples/private-goldset/` | SOURCE-OSS-READINESS-PLAN.md PR-A; PR-18/PR-19 reviews | Before OSS publication or before adding more private smoke checks |
| Classify KG coverage rows currently captured as `unknown` in count baselines by adding `scope_ref.reason` at emission sites and regenerating `tests/baselines/kg_counts/*.json` | Debate 8 PR-0 baseline review | Before treating T2 count-baseline drift as a strict OSS-readiness gate |
| Per-repo import-root metadata resolution — Python import normalization currently reads `importlib.metadata.packages_distributions()` from the runner environment, not the target repo's venv/lockfile; Node builtin normalization reads the runner `node` on `PATH` plus fallback inventory | Debate 8 PR-C review | Before hosted analysis claims target-repo environment fidelity |
| Namespace-package subpath ownership for Python imports — `google.*` style namespace packages fail closed when multiple declared distributions share one import root; resolve with package file metadata or target-env module ownership | Debate 8 PR-C review | When namespace package imports become product-critical |
| Package-style TypeScript config inheritance — the TS resolver follows local and absolute `tsconfig` / `jsconfig` `extends` chains, but intentionally leaves npm package-style bases such as `@tsconfig/node18/tsconfig.json` unresolved because they require package export and install-tree resolution | ts-config-import-resolution pre-PR review, 2026-06-20 | When package-based TS presets leave alias or `baseUrl` imports classified as unknown |
| CODEOWNERS extractor for ownership facts — emit canonical `OWNS` unless ADR-0006 later ratifies an `OWNED_BY` inverse; requires extractor support and predicate emission policy | Debate 19 PR-11 parked follow-up | When deploy-blocker semantics or PR-bot ownership routing becomes a product surface |
| `USES_SCHEMA` qualifier with version chains — current schema-use modeling is partial for deploy-blocker semantics | Debate 19 PR-11 parked follow-up | When `deploy_blockers_for` lands meaningfully |
| Evidence `valid_from` / `valid_to` envelope — current `Evidence` dataclass omits the ADR-0006 time-validity fields | Debate 19 PR-11 parked follow-up | When time-window-based `M_freshness` reporting becomes needed |
| `CALLS` grain elevation from `CodeSymbol → CodeSymbol` to `Service → Endpoint` for cross-service semantics | Debate 19 PR-11 parked follow-up | When inter-service blast-radius queries land |
| Kafka producer/consumer detectors for `PRODUCES_EVENT` / `CONSUMES_EVENT` outside boto3 | Debate 19 PR-11 parked follow-up | When org-level Kafka deployments need cross-service event tracing |
| Terraform broader IaC predicates — `DEPLOYS_VIA_CONFIG`, `PROVIDES_RESOURCE`, `DEPENDS_ON_MODULE` require ADR-0006 ratification before extractor emission | Debate 19 PR-11 parked follow-up | When Helm, Kubernetes, Terraform module, or Pulumi semantics become product-critical |
| Per-file incremental extraction — `build_kg --incremental --since-commit <sha>` rebuilds only files changed in the diff | Debate 19 PR-11 parked follow-up | When fleet repos exceed sizes where full per-repo rebuild on each commit becomes the bottleneck |
| Org dashboard / metric run history — persist and visualize `metrics.jsonl` across runs | Debate 19 PR-11 parked follow-up | When customer-facing coverage reporting becomes a feature requirement |
| Cross-file imported-constant JS/TS endpoint resolution — deferred after historical Debate 1 validation; remaining LatticeAI 23-repo gaps were dominated by dynamic templates, env-host provenance, and helper-call targets, not imported constants | Historical validation summary; source report removed during docs cleanup | Revisit after a focused debate on dynamic route/template params and env-host/base-client provenance |
| Endpoint route-parameter canonicalization — JS/TS client dynamic templates now emit `{param}` paths while backend frameworks may emit `:param`; unify route parameter syntax before relying on path equality between `CALLS_ENDPOINT` and `EXPOSES_ENDPOINT` | Debate 2 PR-1 Claude review; current `normalize_endpoint_path` only strips quotes and prepends `/` | Before service/client endpoint linking or blast-radius logic depends on client/server path equality |
| gRPC proto server-vs-client attribution — `config-grpc-proto` emits EXPOSES_ENDPOINT for every `.proto` `service` regardless of whether the repo generates the server or a client stub; distinguishing needs build wiring outside the proto (.NET `<Protobuf GrpcServices="Client">`, generated base-class subclassing) | Run 9 (`docs/evaluation/polyglot-extraction-maturity.md`); `source/kg/file_formats/grpc_proto/proto_endpoints.py` | When split client/server gRPC repos make client-copy over-attribution material to blast-radius |
| gRPC client `CALLS_ENDPOINT` extraction — `.proto` service declarations surface EXPOSES_ENDPOINT, but generated/hand-written gRPC client invocations are not yet linked to those endpoints | Run 9 maturity note | When cross-service gRPC blast-radius / client-server endpoint linking is needed |
| MCP workflow composition tools contract — local MCP now exposes `planning_context` and `review_context` as experimental host-agent workflow tools beyond ADR-0002's eight primitive tools; decide whether to ratify, hide behind skills, or move to another surface | PR #108 Claude review; ADR-0002 Implementation Status | Tool Query Contract ADR |
| Host-agent MCP skill adoption evaluation — installed Codex/Claude Code `supercontext-mcp` skills must be tested against real planning, coding, and review prompts to verify agents call SuperContext before broad search and cite returned evidence | ADR-0002 post-merge review notes; `docs/mcp/HOST_SKILL_EVALUATION.md` | Before design-partner rollout or positioning the installer as the default agent workflow |
| MCP tool coverage/refusal metadata — `coverage_warnings` and `unsupported_scopes` are present but sparse, so agents cannot always distinguish "no risk found" from "not instrumented" | ADR-0002 post-merge review notes; PR #108 MCP workflow integration | Tool Query Contract ADR or before safety-sensitive `blast_radius` / review claims |
| Expand `review_context` impact safely — add endpoint, event, package, deploy, and runtime impact only where the KG has supported facts and refusal metadata; avoid invented review findings | ADR-0002 post-merge review notes; historical MCP workflow seed removed during docs cleanup | Before treating `review_context` as a complete PR-review product surface |
| `diff_anchor` lead-kind stamping — plan listed `diff_anchor` as a `review_leads` lead kind; success criteria for the attribution branch do not require it and `diff_anchors` already carries coordinate-bearing rows at the top level | review-hypotheses-attribution final-fix review, 2026-07-01 | When hypothesis `supporting_lead_ids` must cite anchor-level (not symbol-level) leads |
| Registry/resolver completeness detection — "new DSL function registered in every converter path" requires comparing emitted-name sets between base and head; a delta question not answerable from a single-snapshot KG | bug-family-detectors plan §"Honest v1 scope decision"; deferred from Q1/Q2 | graph-diff Phase 2 |
| Fail-open optional-gate detection — "provenance absent → editable" requires branch-semantics understanding of optional guards; needs signature/shape facts to identify the guard shape | bug-family-detectors plan §"Honest v1 scope decision"; deferred from Q1/Q2 | After signature/shape facts (sharpening Idea 2) land |
| Production-compatible MCP host setup — local server still lacks auth, streamable HTTP, resource auto-attach, host compatibility tests, and hosted/self-hosted registration flows | ADR-0002 post-merge review notes; Deployment/Auth/Tenancy ADR backlog | Before exposing SuperContext MCP outside local loopback development |

## Per-ADR open follow-ups

### ADR-0003 (Postgres + AGE storage)

| Item | Trigger to revisit |
|---|---|
| AGE bulk-edge-insert performance benchmark on 500-service / 100k-edge fixture | Before first design-partner load test |
| AGE 4-hop blast_radius p95 < 500 ms validation | Before first design-partner load test |
| openCypher coverage gap audit (AGE vs Neo4j) for our 8 tools | Before locking Tool Query Contract ADR |
| AGE upgrade story (1.5 → 1.7), PG 18 + AGE 1.7 production fitness | Before any version upgrade |
| Dgraph ownership stability post-Istari acquisition | Q3 2026 review |
| Bitemporal modeling depth — `valid_from`/`valid_to` columns vs XTDB swap | When `oncall_context_for(since=1h)`-style tools become hot |

### ADR-0005 (Evidence retrieval)

| Item | Trigger to revisit |
|---|---|
| ripgrep p95 benchmark across multi-repo enterprise fixtures | Before deciding Zoekt adapter timing |
| First Zoekt adapter boundary definition | When ripgrep p95 fails at target scale |
| Targeted ast-grep / tree-sitter framework patterns for first design partner | When first design partner signed |
| Default agentic exploration budgets from measured latency / token data (DeepWiki uses a 5-iteration ceiling as one data point) | After first instrumented design-partner runs; DeepWiki's 5-iteration ceiling is input data, not a binding default |
| Semble future fuzzy-search research | Phase 2+ if alias / discovery gap forces it |

### ADR-0006 (Ontology)

| Item | Trigger to revisit |
|---|---|
| Per-relation runtime freshness window defaults — currently 10/14/30 days, expect adjustment | After first design partner trace-data cadence known |
| Probabilistic entity resolution (Splink) for alias reconciliation | Phase 2+ when deterministic Alias table conflicts grow |
| Conflict-resolution policy when two evidence rows assert mutually exclusive mappings (beyond `manual_override` + candidate status) | When such conflicts surface in production |
| Schema compatibility policy (BACKWARD/FORWARD/FULL/transitive) for `EVOLVES_TO` lineage | When schema-evolution campaigns become a Product 1 feature, likely Phase 2 |
| Code-level entity types (`CodeModule`, `CodeSymbol`, `ExternalPackage`, `ExternalSymbol`) status — promote to canonical, keep candidate-only enrichment, or model as a sub-layer below the canonical ontology | When multi-language extraction beyond Python AST forces a consistent decision |
| `IMPORTS` relation status — add to canonical 15, keep code-layer-only, or treat as candidate enrichment | Same trigger as code-level entity types |
| Authz support predicates (`HANDLES_ENDPOINT`, `DEFINES_AUTHZ_POLICY`, `APPLIES_AUTHZ_POLICY`, `USES_AUTHZ_CHECK`) status — keep as review-packet support facts or promote into a canonical authorization ontology | When endpoint authorization becomes cross-repo operational truth instead of bounded security-review context |
| `CALLS` grain roll-up rule — function-level (`CodeSymbol → CodeSymbol`) vs operation-level (`Service → Endpoint`); how to aggregate for multi-service blast_radius | When ingestion crosses repo boundaries and cross-service `find_callers` is needed |
| Support-tier framework facts in `support_facts.jsonl` — current Django/DRF/Celery facts (`DECLARES_FIELD`, `RELATES_TO_MODEL`, `SERIALIZES_MODEL`, `HANDLES_MODEL`, `TASK_USES_MODEL`) are retrieval support, not canonical predicates; promotion requires a multi-framework ontology ADR | When framework structure needs to become cross-repo operational truth instead of bounded review-context support |
| URN human-readable scheme (ADR-0006 §3) not yet honored — current URNs use `supercontext://{kind}/{stable_hash}` for all kinds | Before MCP / UI surfaces ship |
| Evidence-level `valid_from` / `valid_to` columns missing in the current evidence envelope | When bitemporal or freshness-window queries land |
| Promotion rules not enforced in the current ingestion path (all entities/facts default canonical) | When multi-source or `inferred_llm` evidence enters the pipeline |
| Coverage row shape is currently simplified (missing `subject_id`, `last_seen_at`, `window_start`, `window_end`) | When Tool Query Contract ADR locks coverage semantics |
| Current storage is JSONL, not Postgres + AGE (ADR-0003) | When multi-tenant or query-volume requirements force the migration |
| Loud refusal at ingestion is not fully wired for unsupported languages/frameworks | When unsupported-language or unsupported-framework coverage becomes a product gate |

## Phase 2 / 3 candidates

| Item | Phase | Source |
|---|---|---|
| Cross-tenant federation for cross-org service graphs (post-merger transitional + permanently federated subsidiaries) | Phase 3 | PLATFORM-PRD.md §11; ONTOLOGY-RECOMMENDATION.md §11 |
| `get_service_wiki(service_id)` MCP tool generating DeepWiki-style narrative from typed graph + evidence | Phase 2/3 | claude-deepwiki-analysis.md §6 |
| Free hosted MCP for OSS repos as viral acquisition channel (mirror DeepWiki's `mcp.deepwiki.com` model) | GTM strategy, not technical | claude-deepwiki-analysis.md §5 |
| GraphRAG-style enrichment in candidate / sidecar layer (prose / docs / runbooks / incidents) | Phase 2+ | ADR-0004 candidate / enrichment sidecar |
| SCIP / language-indexer integration for symbol-level evidence | Phase 2+ | ADR-0005 §"Explicitly out of v1" |
| Database / FeatureFlag node types | Phase 2+ | ONTOLOGY-RECOMMENDATION.md §8 deferred families |
| Document / Ticket / Decision / Runbook / Incident / File node types | Phase 3 | ONTOLOGY-RECOMMENDATION.md §8 |
| GATES / MIGRATES_WITH / SHARES_DB_WITH / IMPACTED_BY relations | Phase 2+ | ONTOLOGY-RECOMMENDATION.md §8 |

## Open product / strategy questions

| Item | Source | Owner |
|---|---|---|
| Naming finalization — settled on `SuperContext`; GitHub repo rename pending | PRD.md §14 #1 | Roshan |
| First-language priority — TS/JS, Go, or Java/Kotlin (drives extractor allowlist seeding) | PRD.md §14 #2 | First design partner |
| Tracing source for MVP — Datadog vs Tempo vs Jaeger (or all three) | PRD.md §14 #3 | First design partner |
| Pricing model — per-seat vs per-service vs platform-team flat fee | PRD.md §14 #4 | After 2 design partners |
| Self-host vs SaaS-first | PRD.md §14 #5 | Strategic call |
| Buyer entry point — platform team vs single feature team | PRD.md §14 #6 | GTM motion |
| Training-data policy — pre-commit "no model training on customer code" or stay flexible | PRD.md §14 #7 | Legal + GTM |

---

## How to use this file

- **Adding an item:** include source ADR/doc + the trigger condition that should bring it back into scope.
- **Closing an item:** delete the row. The originating ADR's open-follow-up section is the audit trail.
- **Refreshing:** every time an ADR is updated or a new debate converges, sync this file. Drift is the failure mode.
- **Not in scope:** day-to-day implementation tasks, bugs, sprint planning. This is for decisions and capabilities deferred at the architectural level.
