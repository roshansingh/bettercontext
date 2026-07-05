---
name: supercontext-mcp
description: Use when planning, implementing, reviewing code, or analyzing SuperContext A/B trace reports with a SuperContext MCP server available. For broad planning, architecture, dependency, or impact questions, call planning_context before broad repo exploration when the task mentions a service, repo, symbol, package, endpoint, event channel, domain, file path, or changed files/ranges. Use exact primitive tools for exact caller, callee, service brief, or event producer/consumer questions. Use the trace-evaluation guidance for ab-report.md, ab-report.json, deltas.jsonl, or LangSmith run analysis.
---

# SuperContext MCP

Use SuperContext as the starting point for planning, coding, and review. MCP tool results are a source-inspection head start, not a complete or final answer: first read the graph packet to choose the best files, repos, services, domains, and symbols to inspect, then continue with ordinary source reads for claims the packet cannot prove or whose risk requires verification. MCP tool results still spend context tokens, so prefer one bounded workflow call before narrow follow-ups, and prefer exact primitive tools when the user asks an exact graph question.

## Head-Start Boundary

SuperContext is an evidence router, not an answer oracle. It is never a replacement for source inspection, semantic search, runtime/config review, or the agent's own judgement. Never assert that SuperContext alone fully resolved the user's question. `answerability.status: answerable` means the packet has relevant KG evidence inside the current graph scope; it does not mean the repository, runtime, deploy, ownership, security, or downstream impact question is globally answered. For non-trivial planning, review, impact, runtime, deploy, ownership, or safety questions, final answers should separate MCP head start, source-verified claims, candidate or unlinked leads, coverage gaps or unknowns, and next source/config/runtime inspection. Answer the user's question first; keep extra MCP rows as inspection leads unless the user asks for exploratory coverage. In final answers, use scoped labels such as `KG-backed rows`, `source-verified`, `candidate lead`, `coverage gap`, and `unknown`, and preserve those boundaries even when the packet is useful.

## Setup Check

Use an already registered SuperContext MCP endpoint if present. If no endpoint is registered and the user wants local setup, choose one command:

```bash
# Build or refresh the repo-local KG snapshot.
supercontext-init

# Build or refresh the snapshot and start the local MCP server.
supercontext-init --serve
```

For org-wide context, start the org snapshot endpoint instead:

```bash
supercontext org serve --org <org>
```

An org snapshot uses the same planning_context and review_context tools. The snapshot scope is wider, but the MCP surface is unchanged; pass `repo` anchors explicitly, using either `repo` or `owner/repo` when the target repo is known.

Register the printed local HTTP `/mcp` URL in Claude Code. Keep the server loopback-bound unless the user intentionally accepts an unauthenticated public bind.

## Common Packet Contract

For every MCP result, read `answerability`, `proven_facts`, `candidate_leads`, `coverage_gaps`, and `inspection_areas` before deciding what to inspect next. Read `packet_contract` when present; extreme budget fallback packets may omit static legends to preserve evidence. `proven_facts` points to KG-backed/static fields and their counts; cite the underlying detailed rows or file/line evidence, not just the index. `candidate_leads` contains plausible but unverified leads such as import-only consumers, unlinked runtime evidence, ambiguous candidates, or inferred guidance. `coverage_gaps` lists what the KG could not prove. If `output_budget` is present, treat it as truncation guidance: `output_budget.truncated_sections` names the sampled arrays (planning packets also carry `output_budget.omitted_counts`/`backfilled_counts`); use those plus `inspection_areas` and narrower anchors to recover omitted detail when the task needs it.

Use SuperContext as a head start for source inspection: do not reread every proven row when the packet already covers it, but do inspect uncovered `inspection_areas` when task quality depends on broader coverage. If the packet is compacted or too large, prioritize the most task-relevant `inspection_areas`; still name the other concrete refs/search terms as follow-up areas when they matter. Count-only omissions are not enough for final claims; follow any provided refs/search terms or call a narrower anchor when omitted categories are relevant. Do not claim candidate leads, missing gaps, or unsupported scopes as facts until source inspection verifies them.

## Evidence Gates

Before finalizing, split the user request into named answer categories and mark each category as KG-backed, source-verified, candidate, contradicted, unknown, or out-of-scope. Treat requested answer categories as coverage obligations to answer or mark unknown, not permission to expand into every packet row. If an MCP result is ambiguous, retry one exact `disambiguation.retry_arguments` candidate or returned path/qualified name before interpreting empty rows. If a result is partial, not_found, not_answerable, or unsupported_by_current_kg, treat it as an anchor or coverage gap, not a final refusal; use your normal search/read tools at least once before refusing or saying unknown. If a not_found result carries a top-level `coordinate_mismatch` (answerability `missing_fact_families` includes `correct_coordinate`), the symbol exists at a different path/line: retry one `coordinate_mismatch.retry_arguments` entry rather than treating it as a missing symbol or an empty result. If a packet spills to a saved file, use a narrower MCP anchor or returned source refs/search terms instead of making jq/file archaeology the main workflow. For count/list/impact answers, verify that the final count matches the detailed evidence or inspected source rows. Final answers must separate KG-backed facts, source-verified facts, candidates, contradictions, and unknowns.

## Planning

Call `planning_context` before broad search for broad planning, architecture, dependency, or impact questions when the user task names or implies a service, repo, symbol, package, endpoint, event channel, domain, or file path.

Use structured anchors when known:

- `service`, `repo`, `symbol`, `path`, `line`
- `package`, `endpoint`, `event_channel`, `domain`

Read `summary`, `inventory`, `entry_points`, `related_facts`, `source_coordinates`, and the common packet fields before deciding what to inspect next.

- Repo/service identity questions (what service is this, its name/slug/namespace/owning repo): use `search_services` or the matched Service entity identity and repo link. Treat packaging metadata such as a `pyproject.toml` package name as a candidate naming lead, not source-verified proof of the service name.
- Dependency questions: check `related_facts.dependency_importers`.
- Ownership questions: check `ownership_context.answer_packet`; package authors and maintainers are not service owners unless explicit CODEOWNERS/catalog/owner metadata proves ownership.
- Endpoint authorization/security questions: check top-level `authz_surface`, `related_facts.authz_surface`, or `get_service_brief.authz_surface`. Read `review_leads`, `applied_policies`, `in_method_checks`, `inspection_areas`, `inspection_index`, and `unsupported_scopes`; do not treat a missing policy as proven public access.
- Service planning: check `service_operational_surfaces.evidence_partition`, `service_operational_surfaces.deploy_link_facts` / known `DEPLOYS_VIA_CONFIG`, `candidate_or_unlinked_deploy_links`, `deploy_runtime_units`, and `deploy_order_guidance`. Keep `known_linked`, `unlinked_evidence`, and `missing_contracts` separate. Do not promote unlinked domain or deploy leads into deploy proof.
- Source inspection: use `inspection_areas`, `runtime_architecture.answer_packet.investigation_brief.recommended_source_checks`, and `source_coordinates` for targeted reads instead of starting with broad grep.

The primary `limit` controls top-level result rows. Nested packets such as `entry_points`, `related_facts`, and `source_coordinates` are intentionally capped by the returned `summary.section_limit` to keep planning context compact. Fleet planning packets use a compact output cap; anchored planning packets allow more detail but are still bounded. For runtime architecture questions, read `runtime_architecture.answer_packet.investigation_brief` first. If `runtime_architecture.summary.answer_packet_mode` is `investigation_brief_only`, the planning anchor was ambiguous or unresolved; treat the runtime packet as source-inspection context and retry with narrower `repo`, `service`, `domain`, or `endpoint` anchors before making runtime-map or count claims. Otherwise, use `runtime_anchors`, `known_routes`, `unlinked_runtime_leads`, `deploy_units`, `unlinked_deploy_leads`, `consumer_links`, and `recommended_source_checks` as the investigation plan, then inspect files or call `planning_context` again with narrower anchors for omitted detail. In the final answer, include verified `unlinked_runtime_leads` such as API Gateway hostnames, private IPs, static-site CNAME domains, or other infrastructure references as referenced runtime targets with a caveat that they are source leads rather than proven route mappings; apply the same caveat to `unlinked_deploy_leads`. Do not call planning or runtime packets a full architecture map; summary counts are inventory facts inside the indexed KG scope, not proof that no other runtime path exists. Before finalizing, compare the answer against every category named by the user request. If any requested category is missing, partially covered, or only present as a missing fact family, do a targeted follow-up source read/search or a narrower SuperContext call for that category, then explicitly mark it found or unknown. `runtime_architecture.summary.client_endpoint_call_count` is path-shape-scoped candidate fact count, so inspect `endpoint_consumer_missing_method_drop_count` before treating it as usable consumer evidence.

If `answerability.status` is `answerable`, use the returned graph context as the primary head start, then verify what the task's risk requires before finalizing; `answerable` means relevant KG evidence exists in scope, not that the question is fully answered. If it is `partial`, state the missing fact families and use returned follow-ups or targeted source reads. If the result is `ambiguous`, use `next_actions` or returned candidates to refine. If the result is `unsupported_by_current_kg`, `not_found`, or `not_answerable`, state what SuperContext could not prove and fall back to normal repo search/read tools.

## Coding

Use SuperContext for exact graph questions while editing:

- `find_callers` before changing a known symbol with downstream users.
- `reverse_impact` for reverse dependency and caller-impact analysis from a resolved symbol anchor; use it when you need transitive upstream callers, entry-point leads, or `inspection_areas` instead of manually chaining repeated `find_callers` calls.
- `find_callees` to understand immediate dependencies of a changed symbol.
- `blast_radius` only for static downstream CALLS closure from an exact symbol.
- `get_service_brief` for a concise service fact sheet when no broader planning or impact context is needed; read `operational_surfaces.evidence_partition`, `operational_surfaces.deploy_link_facts`, and `operational_surfaces.candidate_or_unlinked_deploy_links`, and do not promote `unlinked_evidence` into deploy/runtime proof.
- `get_event_consumers` and `get_event_producers` for exact known async channel impact.

Still read the relevant source files before editing. If `reverse_impact` is ambiguous, use `candidate_impact_previews` and `disambiguation.retry_arguments` to choose an exact anchor. Treat `reverse_impact.terminal_import_consumer_leads` as source-inspection leads, not runtime-call proof. Keep returned static CALLS rows, terminal import leads, and omitted inspection refs in separate counts; do not roll terminal leads into a `total affected` claim unless the label explicitly says they are unverified inspection leads. Use common `inspection_areas` first, and `reverse_impact.source_inspection_areas` as the tool-specific detail, to inspect tests, scripts, notebooks, entry points, and import-only modules outside the returned CALLS graph. Do not treat SuperContext as a replacement for code inspection.

When the user gives only an unqualified symbol name, call the symbol tool with that name first so SuperContext can surface all candidates. Add `path`/`line` only when the user supplied that location or a prior SuperContext result returned it as a disambiguation candidate; do not use a first source-search hit as the anchor.

For ambiguous symbol-impact results, do not aggregate all candidates unless the user asks for all matches or exploratory impact. Use `candidate_impact_previews` as ranking hints, then retry one exact candidate when the intended edit site is clear; otherwise report the ambiguity and ask for `path`/`line`.

## Review

Before reviewing a diff, call `review_context` first with the minimal derived-entry shape whenever you are inside the head checkout:

- `repo_path`
- `base_ref`

Use the legacy explicit shape only when the harness already supplied it: `repo`, `changed_files`, `changed_ranges`, `base_snapshot`, `base_checkout`, and `head_checkout`. Read `entry_resolution` first; derivation failures and `no_changes_detected` are explicit statuses, not empty evidence. Derived mode writes reusable caches under `repo_path/.supercontext/`, or KG snapshots under `snapshot_store` / `SUPERCONTEXT_SNAPSHOT_STORE` when configured, and adds `.supercontext/` to the repo-local `.git/info/exclude`. The compact parent packet remains capped at 15K chars, and attached internal `followup_packets` may bring the combined response up to 20K chars.

Read `review_lead_status` and `review_answer_packet.top_diff_anchors` / `diff_anchors` first as the PR "you are here" markers, then read `repo_resolution`, `review_leads`, `changed_surface`, `impact`, `runtime_surfaces`, `framework_impact`, `application_impact`, `surface_status`, `source_coordinates`, `answerability`, `proven_facts`, `candidate_leads`, `coverage_gaps`, `inspection_areas`, and `unsupported_review_scopes` before deciding what to inspect next. If `review_lead_status.coverage_status` is `low_coverage`, fall back to direct source review and use `review_leads.source_coordinates` / `diff_anchors` only as inspection coordinates. If `repo_resolution.status` is `ambiguous` or `unresolved`, treat missing anchors as a repo-scope mismatch risk and inspect the changed files directly. If `review_answer_packet.packet_mode` is `diff_anchor_only`, the tool found changed file/range anchors but no changed symbols or direct impact edges; inspect `diff_anchors` / `source_coordinates` directly, treat retained proven rows as context rather than changed-symbol impact, and pass `include_unlinked_leads=true` only when broad unlinked namespace/name leads are worth the extra context. If `changed_ranges` were not supplied, `review_answer_packet.top_changed_symbols` is empty and `review_answer_packet.changed_file_symbol_inventory` contains changed-file symbol inventory; top-level `changed_symbols` is a compatibility changed-file symbol inventory field, not proof that every listed function changed. Say `symbols in changed files` or inspect the diff before saying a function was touched. Use `surface_status` to distinguish `inventory_context`, `unlinked_lead`, and missing requested surfaces; `inventory_context` rows are source-inspection leads, not proof that the named surface is affected by the change. Use `application_impact.same_repo_surfaces` for app-level API/model/serializer/worker/scheduled-job context, `application_impact.runtime_facts` for app-scoped typed runtime facts, and `application_impact.cross_repo_name_leads` only as unlinked source-inspection leads, not as proven impact. If `review_hypotheses` is present, treat each row as a candidate investigation path. Do not report the hypothesis as a finding without inspecting the cited source. Preserve `hypothesis_id` and `supporting_lead_ids` in review notes when possible so evaluation can attribute accepted findings to SuperContext. Known event, endpoint, or contract rows do not prove deploy or safety readiness when unresolved consumers, missing fact families, unsupported scopes, or coverage gaps remain; separate known rows from the safety refusal. Use `inspection_areas` and `source_coordinates` for targeted diff/source reads. Drill into primitive tools only for concrete follow-up findings or missing details.

If `review_quality_status.review_readiness` is `needs_followup`, inspect `followup_packets` when present; otherwise use `review_quality_status.inspection_areas` and `suggested_followups` as the next narrowed source-inspection plan. If a finding uses SuperContext, cite the row's `hypothesis_id` and `label`; otherwise state `no_supercontext_hypothesis_used`.

## Trace Evaluation

When analyzing SuperContext A/B traces, `ab-report.md`, `ab-report.json`, `deltas.jsonl`, or LangSmith runs, evaluate in this order:

1. Correctness or quality verdict first.
2. Evidence and citation quality.
3. MCP tool timing and `mcp_tools_called`.
4. Non-MCP tool-call count and repeated source-search behavior.
5. Token, dollar, and wall-time deltas.
6. "Where MCP Hurts" rows.
7. Skill-compliance signal: whether SuperContext was used early for planning, coding, or review tasks.

Do not claim value from lower tokens, fewer tool calls, lower cost, or faster wall time when `quality_verdict` is `ungraded`, when `mcp_on` quality is worse, or when `cost_status` is `unavailable`.

## Output Rules

- Keep final answers focused on scoped findings, evidence, and unknowns; do not include internal progress commentary.
- Cite returned evidence rows or file/line coordinates when making a claim.
- Do not paste raw evidence packets wholesale.
- Do not invent endpoint, event, deploy, or runtime impact when the current KG does not return it.
- Do not state how code communicates — the specific client API, SDK call, wire format, or delivery mechanism — unless the packet or an inspected source line proves it. The event/endpoint packets identify channels and participants, not the API used to reach them.
- If `coverage_warnings` or `unsupported_scopes` are present, mention them in the answer or review.
