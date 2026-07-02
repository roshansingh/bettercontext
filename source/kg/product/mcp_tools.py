from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from source.kg.core.display import display_entity
from source.kg.core.models import JsonObject, canonical_json
from source.kg.file_formats._shared.common import normalize_endpoint_path_shape
from source.kg.product.application_impact import application_impact_packet
from source.kg.product.engine_version import engine_version
from source.kg.product.authz_surface import authz_surface_packet
from source.kg.product.framework_impact import framework_impact_packet
from source.kg.product.ownership_context import ownership_context_packet
from source.kg.product.output_budget import (
    AUTHZ_COMPACT_LIST_KEYS,
    COMPACT_AUTHZ_INSPECTION_REF_LIMIT,
    PLANNING_CONTEXT_ANCHORED_MAX_CHARS,
    enforce_planning_context_budget,
    enforce_review_context_budget,
    enforce_reverse_impact_budget,
    enforce_service_brief_budget,
)
from source.kg.product.review_attribution import (
    add_review_lead_ids,
    review_available_counts,
    review_lead_counts,
)
from source.kg.product.review_hypotheses import review_hypotheses_for_context
from source.kg.product.runtime_architecture import ENDPOINT_PATH_SHAPE_MATCH_BASIS, runtime_architecture_packet
from source.kg.query.call_site import call_site_from_qualifier
from source.kg.query.snapshot import KgSnapshot


TOOL_NAMES = (
    "search_services",
    "get_service_brief",
    "find_callers",
    "reverse_impact",
    "find_callees",
    "get_event_consumers",
    "get_event_producers",
    "blast_radius",
    "deploy_blockers_for",
)

PLANNING_CONTEXT_SECTION_LIMIT = 5
_PLANNING_CONTEXT_ANCHOR_FIELDS = (
    "repo",
    "path",
    "symbol",
    "service",
    "package",
    "endpoint",
    "event_channel",
    "domain",
)
REVIEW_CONTEXT_DETAIL_LIMIT = 25
REVIEW_CONTEXT_UNKNOWN_SURFACE_CAP = 8
REVIEW_CONTEXT_SURFACES = (
    "ui_screens",
    "scheduled_jobs",
    "sqs_consumers",
    "delivery_workers",
    "tracking_paths",
    "api_surfaces",
    "models",
    "serializers",
)
REVIEW_CONTEXT_SURFACE_ALIASES = {
    "ui": "ui_screens",
    "screen": "ui_screens",
    "screens": "ui_screens",
    "ui_screen": "ui_screens",
    "ui_screens": "ui_screens",
    "scheduled_job": "scheduled_jobs",
    "scheduled_jobs": "scheduled_jobs",
    "job": "scheduled_jobs",
    "jobs": "scheduled_jobs",
    "sqs": "sqs_consumers",
    "queue": "sqs_consumers",
    "queue_consumers": "sqs_consumers",
    "sqs_consumer": "sqs_consumers",
    "sqs_consumers": "sqs_consumers",
    "worker": "delivery_workers",
    "workers": "delivery_workers",
    "delivery_worker": "delivery_workers",
    "delivery_workers": "delivery_workers",
    "tracking": "tracking_paths",
    "tracking_path": "tracking_paths",
    "tracking_paths": "tracking_paths",
    "api": "api_surfaces",
    "apis": "api_surfaces",
    "api_surface": "api_surfaces",
    "api_surfaces": "api_surfaces",
    "api_contract": "api_surfaces",
    "api_contracts": "api_surfaces",
    "contract": "api_surfaces",
    "contracts": "api_surfaces",
    "model": "models",
    "models": "models",
    "schema": "serializers",
    "schemas": "serializers",
    "data_schema": "serializers",
    "data_schemas": "serializers",
    "serializer": "serializers",
    "serializers": "serializers",
}
REVIEW_CONTEXT_BUILTIN_SECTION_ALIASES = frozenset(
    {
        "caller",
        "callers",
        "callee",
        "callees",
        "call_graph",
        "reverse_impact",
        "reverse_callers",
        "symbol_impact",
        "transitive_callers",
        "service",
        "services",
        "deploy",
        "deploys",
        "deployable",
        "deployables",
        "deployment",
        "deployments",
        "owner",
        "owners",
        "ownership",
        "maintainer",
        "maintainers",
    }
)
REVIEW_CONTEXT_OWNERSHIP_SECTION_ALIASES = frozenset(
    {
        "owner",
        "owners",
        "ownership",
        "maintainer",
        "maintainers",
    }
)
PLANNING_CONTEXT_NO_OVERLAP_ACTION = (
    "No deterministic planning anchor combination overlapped after applying the supplied filters. "
    "Try a broader primary anchor or remove one narrowing field."
)
OPERATIONAL_KNOWN_LINKED = "known_linked"
OPERATIONAL_UNLINKED_EVIDENCE = "unlinked_evidence"
OPERATIONAL_MISSING_CONTRACTS = "missing_contracts"
OPERATIONAL_EVIDENCE_BUCKETS = (
    OPERATIONAL_KNOWN_LINKED,
    OPERATIONAL_UNLINKED_EVIDENCE,
    OPERATIONAL_MISSING_CONTRACTS,
)
OPERATIONAL_BUCKET_DESCRIPTIONS = {
    OPERATIONAL_KNOWN_LINKED: (
        "Operational rows connected to the service by exact KG identity or exact repo identity. "
        "These are evidence candidates, not deploy-blocker proof."
    ),
    OPERATIONAL_UNLINKED_EVIDENCE: (
        "Fleet operational config rows not linked to this service by the current KG. "
        "Use only as source leads; do not attribute them to the service without separate verification."
    ),
    OPERATIONAL_MISSING_CONTRACTS: (
        "Deploy/runtime contracts the current KG cannot prove. These gaps must stay explicit in answers."
    ),
}


@dataclass(frozen=True)
class McpTool:
    name: str
    description: str
    input_schema: JsonObject
    handler: Callable[[KgSnapshot, JsonObject], JsonObject]


def tool_definitions() -> list[JsonObject]:
    return [
        {
            "name": tool.name,
            "description": _with_common_output_contract(tool.description),
            "inputSchema": tool.input_schema,
        }
        for tool in _TOOLS.values()
    ]


def _with_common_output_contract(description: str) -> str:
    return (
        f"{description} "
        "Returns common packet contract fields: `packet_contract` when present, `answerability`, "
        "`proven_facts`, `candidate_leads`, `coverage_gaps`, and `inspection_areas`. "
        "Results are a head start for your own investigation, not a complete or final answer."
    )


def _with_default_tool_metadata(payload: JsonObject, *, tool_name: str) -> JsonObject:
    normalized = {
        **payload,
        "coverage_warnings": payload.get("coverage_warnings", []),
        "unsupported_scopes": payload.get("unsupported_scopes", []),
        "next_actions": payload.get("next_actions", []),
    }
    proven_facts = _normalized_proven_facts(normalized)
    candidate_leads = _normalized_candidate_leads(normalized)
    if "answerability" not in normalized:
        normalized["answerability"] = _default_answerability(
            normalized,
            proven_facts=proven_facts,
            candidate_leads=candidate_leads,
        )
    if "proven_facts" not in normalized:
        normalized["proven_facts"] = proven_facts
    if "candidate_leads" not in normalized:
        normalized["candidate_leads"] = candidate_leads
    if "coverage_gaps" not in normalized:
        normalized["coverage_gaps"] = _normalized_coverage_gaps(normalized)
    # Inspection areas are normalized and augmented instead of preserved verbatim
    # so every tool exposes the same follow-up shape. Handler-authored
    # next_actions are mirrored here; default guidance remains in next_actions
    # to avoid duplicating prose in budget-bound packets.
    normalized["inspection_areas"] = _normalized_inspection_areas(normalized, candidate_leads=candidate_leads)
    normalized["next_actions"] = _normalized_next_actions(normalized, candidate_leads=candidate_leads)
    normalized.setdefault("packet_contract", _packet_contract(tool_name))
    return normalized


def call_tool(kg: KgSnapshot, name: str, arguments: JsonObject | None = None) -> JsonObject:
    if name not in _TOOLS:
        raise ValueError(f"Unsupported MCP tool: {name}")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise ValueError("MCP tool arguments must be a JSON object")
    tool = _TOOLS[name]
    _validate_declared_arguments(tool, arguments)
    result = _with_default_tool_metadata(tool.handler(kg, arguments), tool_name=name)
    payload = {
        **result,
        "tool": name,
    }
    if name == "planning_context":
        query = _optional_string(arguments, "query")
        line = _optional_int(arguments, "line")
        anchors = _planning_context_anchors(arguments)
        if _is_planning_context_fleet_request(query=query, line=line, anchors=anchors) or not _planning_context_has_resolved_anchor(payload):
            return enforce_planning_context_budget(payload)
        return enforce_planning_context_budget(
            payload,
            max_chars=PLANNING_CONTEXT_ANCHORED_MAX_CHARS,
            preserve_planning_sections=True,
        )
    if name == "review_context":
        payload.setdefault("output_budget", {})["engine_version"] = engine_version()
        return enforce_review_context_budget(payload)
    if name == "reverse_impact":
        return enforce_reverse_impact_budget(payload)
    if name == "get_service_brief":
        return enforce_service_brief_budget(payload)
    return payload


def _packet_contract(tool_name: str) -> JsonObject:
    return {
        "tool": tool_name,
        "positioning": (
            "SuperContext returns a source-inspection head start, not a complete or final answer: it routes you to "
            "evidence to verify and finish the answer yourself. Use proven_facts as KG-backed evidence, "
            "candidate_leads as leads to verify, coverage_gaps as limits on what can be claimed, and "
            "inspection_areas as the bounded follow-up plan."
        ),
        "common_fields": {
            "proven_facts": "KG-backed rows or field pointers that can be cited after checking evidence boundaries.",
            "candidate_leads": "Plausible but not fully proven rows such as import-only, unlinked, ambiguous, or inferred leads.",
            "coverage_gaps": "Missing, unsupported, truncated, parse-error, or otherwise unproven scopes.",
            "inspection_areas": "Concrete source/config/search follow-ups for facts outside the indexed packet.",
            "answerability": "Whether the packet can answer directly or is partial/ambiguous/unsupported.",
        },
        "claim_rule": (
            "Do not treat candidate_leads or coverage_gaps as proven facts. If the final answer relies on them, "
            "inspect source or state the caveat."
        ),
    }


def _default_answerability(
    payload: JsonObject,
    *,
    proven_facts: JsonObject,
    candidate_leads: JsonObject,
) -> JsonObject:
    status = str(payload.get("status") or "unknown")
    missing: list[str] = []
    if status == "unknown":
        if proven_facts.get("status") == "found":
            answerability_status = "partial" if _candidate_leads_require_verification(candidate_leads) else "answerable"
        elif candidate_leads.get("status") == "found" or payload.get("coverage_warnings") or payload.get("unsupported_scopes"):
            answerability_status = "partial"
        else:
            answerability_status = "not_answerable"
    elif status == "found":
        answerability_status = "partial" if _candidate_leads_require_verification(candidate_leads) else "answerable"
    elif status == "partial":
        answerability_status = "partial"
    elif status == "ambiguous":
        answerability_status = "not_answerable"
        missing = ["ambiguous_anchor"]
    elif status == "not_found":
        answerability_status = "not_answerable"
        missing = ["requested_fact"]
    elif status == "unsupported_by_current_kg":
        answerability_status = "not_answerable"
        missing = ["unsupported_contract"]
    else:
        answerability_status = "partial"
        missing = ["unknown_status"]
    return {
        "status": answerability_status,
        "missing_fact_families": missing,
        "recommended_source_checks": [],
    }


def _normalized_next_actions(payload: JsonObject, *, candidate_leads: JsonObject) -> list[str]:
    actions = [str(action) for action in _as_list(payload.get("next_actions")) if str(action).strip()]
    answerability = payload.get("answerability") if isinstance(payload.get("answerability"), dict) else {}
    answerability_status = str(answerability.get("status") or "")
    missing_families = {str(family) for family in _as_list(answerability.get("missing_fact_families"))}
    if answerability_status == "ambiguous" or missing_families.intersection(
        {"ambiguous_anchor", "unambiguous_primary_anchor"}
    ):
        actions.append(
            "Retry one exact disambiguation.retry_arguments candidate or returned path/qualified name before interpreting empty result rows."
        )
    elif (
        answerability_status.startswith("partial")
        or answerability_status in {"not_answerable", "not_found", "unsupported_by_current_kg"}
    ):
        actions.append(
            "Treat this packet as a head start plus coverage boundary, not a final answer; use your normal search/read tools at least once before refusing or saying unknown."
        )
    if candidate_leads.get("status") == "found":
        actions.append(
            "Verify candidate_leads with source or config evidence before promoting them to final facts; otherwise report them as candidates."
        )
    return _dedupe_strings(actions)


def _normalized_proven_facts(payload: JsonObject) -> JsonObject:
    sources = []
    for field in _PROVEN_FACT_FIELDS:
        value = payload.get(field)
        count = _field_count(value, allow_singleton_dict=field in _SINGLETON_PROVEN_FACT_FIELDS)
        if count:
            sources.append({"field": field, "count": count})
    return {
        "status": "found" if sources else "empty",
        "sources": sources,
        "claim_boundary": (
            "Rows in these fields are KG-backed/static evidence. Verify relevant source coordinates before "
            "using them for code-change, runtime, or deploy claims."
        ),
    }


def _normalized_candidate_leads(payload: JsonObject) -> JsonObject:
    sources = []
    for field in _CANDIDATE_LEAD_FIELDS:
        value = payload.get(field)
        count = _field_count(value)
        if count:
            sources.append({"field": field, "count": count, "lead_kind": _candidate_lead_kind(field)})
    for field, nested_path in _NESTED_CANDIDATE_LEAD_FIELDS:
        value = _nested_get(payload, nested_path)
        count = _field_count(value)
        if count:
            sources.append({"field": field, "count": count, "lead_kind": _candidate_lead_kind(field)})
    return {
        "status": "found" if sources else "empty",
        "sources": sources,
        "claim_boundary": (
            "Candidate leads are search and inspection leads only. They are not proof of runtime execution, "
            "ownership, deploy order, or direct impact until verified."
        ),
    }


def _candidate_leads_require_verification(leads: JsonObject) -> bool:
    for source in _as_list(leads.get("sources")):
        if not isinstance(source, dict):
            continue
        # Ambiguous candidate rows guide disambiguation, but they do not by themselves
        # downgrade a found packet. Import-only, unlinked, inferred, and truncated rows do.
        if source.get("lead_kind") in {
            "import_only_source_lead",
            "unlinked_source_lead",
            "inference_or_guidance",
            "truncated_source_inspection_lead",
        }:
            return True
    return False


def _normalized_coverage_gaps(payload: JsonObject) -> list[JsonObject]:
    gaps: list[JsonObject] = []
    for warning in _as_list(payload.get("coverage_warnings")):
        gaps.append({"trigger": "coverage_warning", "detail": warning})
    for scope in _as_list(payload.get("unsupported_scopes")):
        gaps.append({"trigger": "unsupported_scope", "detail": scope})
    answerability = payload.get("answerability")
    if isinstance(answerability, dict):
        for family in _as_list(answerability.get("missing_fact_families")):
            gaps.append({"trigger": "missing_fact_family", "fact_family": family})
        for item in _as_list(answerability.get("cannot_prove")):
            gaps.append({"trigger": "cannot_prove", "detail": item})
    output_budget = payload.get("output_budget")
    if isinstance(output_budget, dict):
        if output_budget.get("truncated") is True:
            gaps.append(
                {
                    "trigger": "truncated_output",
                    "detail": {
                        "truncated_sections": output_budget.get("truncated_sections", []),
                        "omitted_counts": output_budget.get("omitted_counts", {}),
                    },
                }
            )
    if payload.get("status") in {"not_found", "unsupported_by_current_kg", "ambiguous"}:
        gaps.append({"trigger": str(payload.get("status")), "detail": "Packet status is not fully answerable."})
    return _dedupe_json_rows(gaps)


def _normalized_inspection_areas(payload: JsonObject, *, candidate_leads: JsonObject | None = None) -> list[JsonObject]:
    rows: list[JsonObject] = []
    for row in _as_list(payload.get("inspection_areas")):
        if isinstance(row, dict):
            rows.append(_normalize_inspection_area(row, default_trigger="tool_specific"))
    for row in _as_list(payload.get("source_inspection_areas")):
        if isinstance(row, dict):
            rows.append(_normalize_inspection_area(row, default_trigger="source_inspection_area"))

    answerability = payload.get("answerability")
    if isinstance(answerability, dict):
        for check in _as_list(answerability.get("recommended_source_checks")):
            rows.append(_inspection_area_from_text(check, trigger="answerability_recommended_source_check"))
        for check in _as_list(answerability.get("recommended_followups")):
            rows.append(_inspection_area_from_text(check, trigger="answerability_recommended_followup"))
    for action in _as_list(payload.get("next_actions")):
        rows.append(_inspection_area_from_text(action, trigger="next_action"))

    for field, nested_path in _NESTED_INSPECTION_AREA_FIELDS:
        value = _nested_get(payload, nested_path)
        if isinstance(value, list):
            for row in value:
                if isinstance(row, dict):
                    rows.append(_normalize_inspection_area(row, default_trigger=field))
                else:
                    rows.append(_inspection_area_from_text(row, trigger=field))
        elif isinstance(value, dict):
            rows.append(_normalize_inspection_area(value, default_trigger=field))

    if candidate_leads is None:
        candidate_leads = _normalized_candidate_leads(payload)
    if candidate_leads.get("status") == "found":
        rows.append(
            {
                "area": "candidate_leads",
                "reason": "Candidate leads require source verification before final claims.",
                "trigger": "candidate_leads_present",
                "inspection_refs": [],
                "search_terms": [],
            }
        )
    return _dedupe_json_rows([row for row in rows if row])


def _inspection_area_from_text(value: object, *, trigger: str) -> JsonObject:
    text = str(value).strip()
    if not text:
        return {}
    return {
        "area": trigger,
        "reason": text,
        "trigger": trigger,
        "inspection_refs": [],
        "search_terms": [],
    }


def _normalize_inspection_area(row: JsonObject, *, default_trigger: str) -> JsonObject:
    raw_search_terms = row.get("search_terms")
    raw_inspection_refs = row.get("inspection_refs")
    if isinstance(raw_search_terms, list):
        search_terms = raw_search_terms
    elif isinstance(raw_search_terms, (str, int, float)):
        search_terms = [raw_search_terms]
    else:
        search_terms = []
    if isinstance(raw_inspection_refs, list):
        inspection_refs = raw_inspection_refs
    elif isinstance(raw_inspection_refs, dict):
        inspection_refs = [raw_inspection_refs]
    elif "inspection_refs" in row:
        inspection_refs = []
    else:
        inspection_refs = _inspection_refs_from_path_hints(row.get("path_hints"), repos=row.get("repos"))
    normalized = {
        **row,
        "area": str(row.get("area") or default_trigger),
        "reason": str(row.get("reason") or row.get("scope_hint") or ""),
        "trigger": str(row.get("trigger") or default_trigger),
        "inspection_refs": inspection_refs,
        "search_terms": [str(item) for item in search_terms if isinstance(item, (str, int, float))],
    }
    return normalized


def _inspection_refs_from_path_hints(path_hints: object, *, repos: object) -> list[JsonObject]:
    paths = [item for item in _as_list(path_hints) if isinstance(item, str) and item.strip()]
    repo_values = [item for item in _as_list(repos) if isinstance(item, str) and item.strip()]
    refs = []
    for path in paths:
        refs.append({"path": path, "repo": repo_values[0] if len(repo_values) == 1 else None})
    return refs


def _field_count(value: object, *, allow_singleton_dict: bool = False) -> int:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        for key in (
            "returned_count",
            "lead_count",
            "count",
            "symbol_count",
            "caller_count",
            "callee_count",
            "edge_count",
            "consumer_count",
            "producer_count",
            "affected_symbol_count",
        ):
            raw = value.get(key)
            if isinstance(raw, int) and not isinstance(raw, bool):
                rows_key = _COUNT_FIELD_ROWS.get(key)
                rows = value.get(rows_key) if rows_key else None
                if isinstance(rows, list):
                    return min(raw, len(rows))
                return raw
        for key in (
            "leads",
            "rows",
            "items",
            "candidates",
            "consumers",
            "producers",
            "services",
            "symbols",
            "practical_deploy_order",
            "proven_endpoint_consumers",
        ):
            rows = value.get(key)
            if isinstance(rows, list):
                return len(rows)
        status = value.get("status")
        if isinstance(status, str):
            normalized_status = status.strip().lower()
            if (
                normalized_status in {"empty", "not_found", "not_answerable", "not_computed", "unsupported_by_current_kg"}
                or normalized_status.startswith("no_")
            ):
                return 0
        if allow_singleton_dict and value:
            return 1
    return 0


_COUNT_FIELD_ROWS = {
    "returned_count": "rows",
    "lead_count": "leads",
    "symbol_count": "symbols",
    "caller_count": "callers",
    "callee_count": "callees",
    "edge_count": "edges",
    "consumer_count": "consumers",
    "producer_count": "producers",
    "affected_symbol_count": "affected_symbols",
}


def _nested_get(payload: JsonObject, path: tuple[str, ...]) -> object:
    current: object = payload
    for part in path:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _as_list(value: object) -> list[object]:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [value]


def _dedupe_json_rows(rows: list[JsonObject]) -> list[JsonObject]:
    seen: set[str] = set()
    deduped = []
    for row in rows:
        key = canonical_json(row)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped = []
    for value in values:
        normalized = value.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return deduped


# Exact field -> lead-kind mapping for every registered candidate-lead field/nested label.
# Explicit (not substring heuristics) so a new field — e.g. call_site_leads — is classified
# deliberately rather than falling through, and so a field name containing an unrelated
# keyword can't be misclassified.
_CANDIDATE_LEAD_KIND: dict[str, str] = {
    "candidates": "candidate_match",
    "candidate_impact_previews": "candidate_match",
    "call_site_leads": "non_callable_call_site_lead",
    "import_consumer_leads": "import_only_source_lead",
    "terminal_import_consumer_leads": "import_only_source_lead",
    "truncated_terminal_symbols": "truncated_source_inspection_lead",
    "deploy_order_guidance": "inference_or_guidance",
    "unlinked_domain_route_samples": "unlinked_source_lead",
    "candidate_or_unlinked_deploy_links": "unlinked_source_lead",
    "endpoint_consumers": "endpoint_consumer_candidate",
    "candidate_or_unlinked_event_channels": "unlinked_source_lead",
    "operational_surfaces.evidence_partition.unlinked_evidence": "unlinked_source_lead",
    "service_operational_surfaces.evidence_partition.unlinked_evidence": "unlinked_source_lead",
    "operational_surfaces.candidate_or_unlinked_deploy_links": "unlinked_source_lead",
    "service_operational_surfaces.candidate_or_unlinked_deploy_links": "unlinked_source_lead",
    "runtime_surfaces.candidate_or_unlinked_event_channels": "unlinked_source_lead",
    "related_facts.candidate_or_unlinked_event_channels": "unlinked_source_lead",
    "related_facts.service_brief.candidate_or_unlinked_event_channels": "unlinked_source_lead",
    "review_answer_packet.runtime.candidate_or_unlinked_event_channels": "unlinked_source_lead",
    "application_impact.cross_repo_name_leads": "cross_repo_name_lead",
    "runtime_architecture.answer_packet.unlinked_runtime_leads": "unlinked_source_lead",
    "runtime_architecture.answer_packet.unlinked_deploy_leads": "unlinked_source_lead",
    "runtime_architecture.answer_packet.investigation_brief.unlinked_deploy_leads": "unlinked_source_lead",
}


def _candidate_lead_kind(field: str) -> str:
    return _CANDIDATE_LEAD_KIND.get(field, "candidate_lead")


_PROVEN_FACT_FIELDS = (
    "services",
    "service",
    "symbols",
    "dependencies",
    "endpoints",
    "event_channels",
    "domains",
    "callers",
    "callees",
    "edges",
    "roots",
    "tiers",
    "affected_symbols",
    "consumers",
    "producers",
    "deploy_mappings",
    "deploy_runtime_units",
    "changed_symbols",
    "changed_file_symbols",
    "direct_callers",
    "direct_callees",
    "transitive_callers",
    "repo_dependencies",
)

_SINGLETON_PROVEN_FACT_FIELDS = ("service",)

_CANDIDATE_LEAD_FIELDS = (
    "candidates",
    "candidate_impact_previews",
    "call_site_leads",
    "import_consumer_leads",
    "terminal_import_consumer_leads",
    "truncated_terminal_symbols",
    "endpoint_consumers",
    "candidate_or_unlinked_event_channels",
    "candidate_or_unlinked_deploy_links",
    "deploy_order_guidance",
    "unlinked_domain_route_samples",
)

_NESTED_CANDIDATE_LEAD_FIELDS = (
    ("operational_surfaces.evidence_partition.unlinked_evidence", ("operational_surfaces", "evidence_partition", "unlinked_evidence")),
    ("service_operational_surfaces.evidence_partition.unlinked_evidence", ("service_operational_surfaces", "evidence_partition", "unlinked_evidence")),
    ("operational_surfaces.candidate_or_unlinked_deploy_links", ("operational_surfaces", "candidate_or_unlinked_deploy_links")),
    ("service_operational_surfaces.candidate_or_unlinked_deploy_links", ("service_operational_surfaces", "candidate_or_unlinked_deploy_links")),
    ("runtime_surfaces.candidate_or_unlinked_event_channels", ("runtime_surfaces", "candidate_or_unlinked_event_channels")),
    ("related_facts.candidate_or_unlinked_event_channels", ("related_facts", "candidate_or_unlinked_event_channels")),
    (
        "related_facts.service_brief.candidate_or_unlinked_event_channels",
        ("related_facts", "service_brief", "candidate_or_unlinked_event_channels"),
    ),
    (
        "review_answer_packet.runtime.candidate_or_unlinked_event_channels",
        ("review_answer_packet", "runtime", "candidate_or_unlinked_event_channels"),
    ),
    ("application_impact.cross_repo_name_leads", ("application_impact", "cross_repo_name_leads")),
    ("runtime_architecture.answer_packet.unlinked_runtime_leads", ("runtime_architecture", "answer_packet", "unlinked_runtime_leads")),
    ("runtime_architecture.answer_packet.unlinked_deploy_leads", ("runtime_architecture", "answer_packet", "unlinked_deploy_leads")),
    (
        "runtime_architecture.answer_packet.investigation_brief.unlinked_deploy_leads",
        ("runtime_architecture", "answer_packet", "investigation_brief", "unlinked_deploy_leads"),
    ),
)

_NESTED_INSPECTION_AREA_FIELDS = (
    ("authz_surface.inspection_areas", ("authz_surface", "inspection_areas")),
    ("related_facts.authz_surface.inspection_areas", ("related_facts", "authz_surface", "inspection_areas")),
    ("runtime_architecture.answer_packet.investigation_brief.recommended_source_checks", ("runtime_architecture", "answer_packet", "investigation_brief", "recommended_source_checks")),
    ("review_answer_packet.inspection_areas", ("review_answer_packet", "inspection_areas")),
)


def _search_services(kg: KgSnapshot, arguments: JsonObject) -> JsonObject:
    query = _optional_string(arguments, "query")
    limit = _limit(arguments)
    services = _matching_services(kg, query)[:limit]
    return {
        "status": "found" if services else "not_found",
        "query": query,
        "returned_count": len(services),
        "services": [_service_row(kg, service) for service in services],
    }


def _get_service_brief(kg: KgSnapshot, arguments: JsonObject) -> JsonObject:
    service_query = _required_string(arguments, "service")
    limit = _limit(arguments)
    matches = _matching_services(kg, service_query)
    if not matches:
        return {"status": "not_found", "query": service_query, "service": None}
    if len(matches) > 1:
        return {
            "status": "ambiguous",
            "query": service_query,
            "candidates": [_service_row(kg, service) for service in matches[:limit]],
            "candidate_count": len(matches),
        }

    service = matches[0]
    service_id = service["entity_id"]
    service_row = _service_row(kg, service)
    related = _facts_touching_entity(kg, service_id)
    endpoints = _planning_context_dedupe_rows(
        [row for row in related if row.get("predicate") in {"EXPOSES_ENDPOINT", "CALLS_ENDPOINT", "DOCUMENTS_ENDPOINT"}]
    )
    events = _planning_context_dedupe_rows(
        [row for row in related if _is_known_event_row(row)]
    )
    candidate_events = _planning_context_dedupe_rows(
        [row for row in related if _is_candidate_or_unlinked_event_row(row)]
    )
    deploy_mappings = _planning_context_dedupe_rows(
        [
            row
            for row in related
            if row.get("predicate") == "ROUTES_DOMAIN_TO_DEPLOY" or _is_known_deploy_row(row)
        ]
    )
    endpoint_consumer_packet = _endpoint_consumer_packet_for_service(kg, service, limit=limit)
    operational_surfaces = _service_operational_surfaces(kg, service, limit=limit)
    service_repo = _service_repo(service)
    authz_surface = authz_surface_packet(
        kg,
        repo=service_repo,
        limit=limit,
        allow_fleet=False,
    )
    missing_fact_families = []
    next_actions = []
    if not deploy_mappings:
        missing_fact_families.append("deploy_mapping")
        next_actions.append(
            "No deploy-mapping facts are linked to this service; inspect deployment manifests or CI/CD config before making production/staging deployment claims."
        )
    if endpoint_consumer_packet["summary"]["consumer_fact_count"] > 0:
        next_actions.append(
            "endpoint_consumers are static path-shape-matched CALLS_ENDPOINT facts; verify host/env resolution before treating them as runtime dependencies."
        )
    coverage_warnings = _uninstrumented_language_coverage_warnings(
        _uninstrumented_language_entries(kg, repo=service_repo),
        repo=service_repo,
    )
    return {
        "status": "found",
        "service": service_row,
        "summary": {
            "endpoint_fact_count": len(endpoints),
            "event_fact_count": len(events),
            "candidate_or_unlinked_event_fact_count": len(candidate_events),
            "deploy_mapping_count": len(deploy_mappings),
            "endpoint_consumer_fact_count": endpoint_consumer_packet["summary"]["consumer_fact_count"],
            "endpoint_consumer_service_count": endpoint_consumer_packet["summary"]["consumer_service_count"],
            "domain_route_candidate_count": operational_surfaces["summary"]["domain_route_candidate_count"],
            "deploy_target_candidate_count": operational_surfaces["summary"]["deploy_target_candidate_count"],
            "authz_endpoint_handler_count": authz_surface["summary"]["endpoint_handler_count"],
            "authz_missing_or_unknown_count": authz_surface["summary"]["missing_or_unknown_authz_count"],
        },
        "endpoints": endpoints[:limit],
        "event_channels": events[:limit],
        "candidate_or_unlinked_event_channels": candidate_events[:limit],
        "deploy_mappings": deploy_mappings[:limit],
        "endpoint_consumers": endpoint_consumer_packet,
        "operational_surfaces": operational_surfaces,
        "authz_surface": authz_surface,
        "claim_contract": _service_brief_claim_contract(),
        "coverage_warnings": coverage_warnings,
        "answerability": {
            "status": "partial" if missing_fact_families else "answerable",
            "missing_fact_families": missing_fact_families,
            "recommended_followups": next_actions,
        },
        "next_actions": next_actions,
    }


def _service_brief_claim_contract() -> JsonObject:
    return {
        "scope": "indexed static service, endpoint, event, deploy, and operational facts",
        "known_rows": [
            "endpoints",
            "event_channels",
            "deploy_mappings",
            "endpoint_consumers",
            "operational_surfaces.evidence_partition.known_linked",
        ],
        "candidate_or_unlinked_rows": [
            "candidate_or_unlinked_event_channels",
            "operational_surfaces.candidate_or_unlinked_deploy_links",
            "operational_surfaces.evidence_partition.unlinked_evidence",
            "operational_surfaces.evidence_partition.unlinked_evidence.deploy_link_samples",
            "endpoint_consumers with unresolved host/env assumptions",
        ],
        "safety_rule": (
            "Known endpoint, event, deploy, or consumer rows do not prove deploy safety, runtime health, "
            "schema compatibility, authorization completeness, or absence of unindexed consumers."
        ),
        "required_caveat": (
            "For deploy order, safe-deploy, runtime routing, authorization, schema, or production environment claims, "
            "inspect source/config/operational evidence beyond this static service brief."
        ),
    }


def _find_callers(kg: KgSnapshot, arguments: JsonObject) -> JsonObject:
    return _with_symbol_claim_contract(
        _with_symbol_miss_next_actions(
            kg.find_callers(
                _required_string(arguments, "symbol"),
                limit=_limit(arguments),
                path=_optional_string(arguments, "path"),
                line=_optional_int(arguments, "line"),
                include_all=_optional_bool(arguments, "include_all", default=False),
            ),
            direction="callers",
            kg=kg,
        ),
        tool_name="find_callers",
    )


def _reverse_impact(kg: KgSnapshot, arguments: JsonObject) -> JsonObject:
    return _with_symbol_miss_next_actions(
        kg.reverse_impact(
            _required_string(arguments, "symbol"),
            depth=_bounded_int(arguments.get("depth", 3), field="depth", minimum=1, maximum=6),
            limit=_limit(arguments),
            path=_optional_string(arguments, "path"),
            line=_optional_int(arguments, "line"),
            include_all=_optional_bool(arguments, "include_all", default=False),
        ),
        direction="reverse impact",
        kg=kg,
    )


def _with_symbol_miss_next_actions(payload: JsonObject, *, direction: str, kg: KgSnapshot) -> JsonObject:
    if payload.get("status") != "not_found":
        return payload
    next_actions = list(payload.get("next_actions", []))
    coordinate_mismatch = _symbol_coordinate_mismatch_resolution(payload)
    if coordinate_mismatch is not None:
        next_actions.extend(str(action) for action in _as_list(coordinate_mismatch.get("next_actions")))
        next_actions.append(
            "Symbol resolution found candidates that do not match the requested path/line; retry one `coordinate_mismatch.retry_arguments` entry before treating this as a missing symbol or an empty result."
        )
        result = {**payload, "next_actions": _dedupe_strings(next_actions)}
        # Surface the mismatch as a structured top-level marker plus a distinct answerability
        # so an agent can tell "wrong path/line, symbol exists elsewhere" from a genuinely
        # missing symbol or a real empty result — both of which otherwise share
        # status=not_found and a direction-specific answerability (e.g.
        # missing_fact_families=["requested_fact"] for find_callers/callees,
        # ["reverse_callers"] for reverse_impact).
        inner = coordinate_mismatch.get("coordinate_mismatch")
        if isinstance(inner, dict):
            result["coordinate_mismatch"] = inner
            result["answerability"] = {
                "status": "not_answerable",
                "missing_fact_families": ["correct_coordinate"],
                "recommended_source_checks": [
                    "Symbol exists at a different path/line; retry one coordinate_mismatch.retry_arguments entry before treating this as a missing symbol or an empty result.",
                ],
            }
        return result
    import_leads = payload.get("import_consumer_leads")
    if isinstance(import_leads, dict) and import_leads.get("status") == "found":
        next_actions.append(
            "Inspect import_consumer_leads as cross-repo source leads; they are not CALLS proof but may identify importer modules and symbols to verify."
        )
    next_actions.extend(
        [
            f"Use source inspection to verify {direction}; this graph miss is not proof of absence.",
            "If the symbol is imported from an external package, search workspace source files for call sites such as `symbol(`.",
            "If the symbol is locally defined under a different qualified name, retry with `path` or `line` disambiguation.",
        ]
    )
    result = {
        **payload,
        "next_actions": next_actions,
    }
    repo = _symbol_payload_repo(payload)
    coverage_warnings = _uninstrumented_language_coverage_warnings(
        _uninstrumented_language_entries(kg, repo=repo),
        repo=repo,
    )
    if coverage_warnings:
        result["coverage_warnings"] = list(payload.get("coverage_warnings", [])) + coverage_warnings
    return result


def _symbol_coordinate_mismatch_resolution(payload: JsonObject) -> JsonObject | None:
    for key in ("target", "source"):
        value = payload.get(key)
        if isinstance(value, dict) and value.get("confidence") == "coordinate_mismatch":
            return value
    return None


def _find_callees(kg: KgSnapshot, arguments: JsonObject) -> JsonObject:
    return _with_symbol_claim_contract(
        _with_symbol_miss_next_actions(
            kg.find_callees(
                _required_string(arguments, "symbol"),
                limit=_limit(arguments),
                path=_optional_string(arguments, "path"),
                line=_optional_int(arguments, "line"),
                include_all=_optional_bool(arguments, "include_all", default=False),
            ),
            direction="callees",
            kg=kg,
        ),
        tool_name="find_callees",
    )


def _blast_radius(kg: KgSnapshot, arguments: JsonObject) -> JsonObject:
    return _with_symbol_claim_contract(
        _with_symbol_miss_next_actions(
            kg.blast_radius(
                _required_string(arguments, "symbol"),
                depth=_bounded_int(arguments.get("depth", 1), field="depth", minimum=1, maximum=6),
                limit=_limit(arguments),
                path=_optional_string(arguments, "path"),
                line=_optional_int(arguments, "line"),
                include_all=_optional_bool(arguments, "include_all", default=False),
            ),
            direction="static downstream impact",
            kg=kg,
        ),
        tool_name="blast_radius",
    )


def _with_symbol_claim_contract(payload: JsonObject, *, tool_name: str) -> JsonObject:
    if "claim_contract" in payload:
        return payload
    if tool_name == "blast_radius":
        scope = "bounded static downstream CALLS closure"
        known_rows = ["edges"]
        count_fields = ["edge_count"]
    elif tool_name == "find_callees":
        scope = "immediate static downstream CALLS edges"
        known_rows = ["callees"]
        count_fields = ["callee_count"]
    else:
        scope = "immediate static upstream CALLS edges"
        known_rows = ["callers"]
        count_fields = ["caller_count"]
    return {
        **payload,
        "claim_contract": {
            "scope": scope,
            "known_rows": known_rows,
            "count_fields": count_fields,
            "claim_boundary": (
                "Static CALLS facts only; verify source before making runtime, endpoint, deploy, safety, "
                "or absence-of-impact claims."
            ),
        },
    }


def _get_event_consumers(kg: KgSnapshot, arguments: JsonObject) -> JsonObject:
    channel = _required_string(arguments, "channel")
    return _event_facts(kg, channel=channel, predicate="CONSUMES_EVENT", limit=_limit(arguments), result_key="consumers")


def _get_event_producers(kg: KgSnapshot, arguments: JsonObject) -> JsonObject:
    channel = _required_string(arguments, "channel")
    return _event_facts(kg, channel=channel, predicate="PRODUCES_EVENT", limit=_limit(arguments), result_key="producers")


def _deploy_blockers_for(kg: KgSnapshot, arguments: JsonObject) -> JsonObject:
    service = _required_string(arguments, "service")
    result = _unsupported_by_current_kg(
        "deploy_blockers_for",
        f"No canonical deploy-blocker relation is implemented yet for service {service!r}.",
    )
    result["answerability"] = {
        "status": "not_answerable",
        "missing_fact_families": ["canonical_service_deploy_blocker"],
        "cannot_prove": [
            "canonical deploy blockers",
            "must-deploy-before services",
            "safe deploy order from endpoint consumers or deploy_order_guidance alone",
        ],
        "recommended_followups": _unsupported_contract_next_actions("deploy_blockers_for"),
    }
    result["coverage_warnings"] = [
        "Endpoint consumers and deploy_order_guidance are compatibility leads, not a must-deploy-before list.",
    ]
    return result


_OPPOSITE_EVENT_PREDICATE = {
    "CONSUMES_EVENT": "PRODUCES_EVENT",
    "PRODUCES_EVENT": "CONSUMES_EVENT",
}
_EVENT_FACT_PREDICATES = {"REFERENCES_EVENT_CHANNEL", "CONSUMES_EVENT", "PRODUCES_EVENT"}
_DIRECTIONAL_EVENT_FACT_PREDICATES = {"CONSUMES_EVENT", "PRODUCES_EVENT"}
_EVENT_PREDICATE_ROLE = {
    "CONSUMES_EVENT": "consumers",
    "PRODUCES_EVENT": "producers",
}


def _event_facts(kg: KgSnapshot, *, channel: str, predicate: str, limit: int, result_key: str) -> JsonObject:
    opposite_predicate = _OPPOSITE_EVENT_PREDICATE[predicate]
    rows = []
    opposite_count = 0
    for fact in kg.facts:
        fact_predicate = fact.get("predicate")
        if fact_predicate not in (predicate, opposite_predicate):
            continue
        subject = kg.entities_by_id.get(fact["subject_id"])
        object_ = kg.entities_by_id.get(fact["object_id"])
        if not subject or not object_ or object_.get("kind") != "EventChannel":
            continue
        if channel.lower() not in _event_channel_search_text(object_).lower():
            continue
        if fact_predicate == predicate and _is_known_event_fact(fact, object_):
            rows.append(_fact_result(kg, fact, subject, object_))
        elif fact_predicate == opposite_predicate and _is_known_event_fact(fact, object_):
            opposite_count += 1
    returned = rows[:limit]
    missing_fact_families = [] if rows else ["static_event_facts"]
    result = {
        "status": "found" if rows else "not_found",
        "channel": channel,
        "event_fact_count": len(rows),
        "returned_count": len(returned),
        "answerability": {
            "status": "answerable" if rows else "partial",
            "scope": "indexed static event-channel facts only",
            "missing_fact_families": missing_fact_families,
            "cannot_prove": [
                "whether messages were published or consumed in a time window",
                "whether there are runtime-only subscribers outside indexed source/config",
                "whether a zero-consumer result means the event channel is unused",
            ],
        },
        "claim_contract": _event_claim_contract(result_key),
        "next_actions": _event_next_actions(found=bool(rows), channel=channel),
        result_key: returned,
    }
    coverage_warnings = _event_coverage_asymmetry_warnings(
        channel=channel,
        result_key=result_key,
        queried_count=len(rows),
        opposite_predicate=opposite_predicate,
        opposite_count=opposite_count,
    )
    if coverage_warnings:
        result["coverage_warnings"] = coverage_warnings
    return result


def _event_coverage_asymmetry_warnings(
    *,
    channel: str,
    result_key: str,
    queried_count: int,
    opposite_predicate: str,
    opposite_count: int,
) -> list[str]:
    """Flag producer/consumer asymmetry for an event channel.

    A channel that is indexed on one side only (e.g. consumed but never produced
    in the loaded snapshot) is a coverage signal, not proof: the missing side is
    typically external or emitted by an uninstrumented language/service. This is
    derived deterministically from the opposite-predicate fact count over the same
    channel match, so it holds for any language whose extractors emit event facts.
    """
    opposite_role = _EVENT_PREDICATE_ROLE[opposite_predicate]
    if queried_count > 0 and opposite_count == 0:
        return [
            f"Event channel query {channel!r} matched {queried_count} indexed {result_key} but 0 {opposite_role}; "
            f"the {opposite_role} side may be external or in an uninstrumented language/service. "
            f"Treat {opposite_role} coverage as thin, not absent."
        ]
    if queried_count == 0 and opposite_count > 0:
        return [
            f"Event channel query {channel!r} matched 0 indexed {result_key} but {opposite_count} {opposite_role}; "
            f"the {result_key} side may be external or in an uninstrumented language/service. "
            f"Treat this empty {result_key} result as thin coverage, not proof of absence."
        ]
    return []


def _uninstrumented_language_entries(kg: KgSnapshot, *, repo: str | None = None) -> list[JsonObject]:
    """Aggregate ingestion-time loud-refusal coverage by language.

    Reads the ``LANGUAGE_SUPPORT`` / ``reason='unsupported_language'`` coverage
    rows the build emits when a file's language has no allowlisted extractor, and
    returns ``[{repo, language, file_count}]`` sorted by language. Optionally
    scoped to one repo. This is language-agnostic by construction: it reports
    whatever the snapshot could not extract, so an agent never reads an empty
    symbol/event/endpoint result as proof of absence when the relevant code is in
    an unindexed language.
    """
    repo_key = _normalize_repo_text(repo) if repo is not None else None
    by_language: dict[str, JsonObject] = {}
    for row in kg.coverage:
        if not isinstance(row, dict) or row.get("state") != "uninstrumented":
            continue
        scope_ref = row.get("scope_ref")
        if not isinstance(scope_ref, dict) or scope_ref.get("reason") != "unsupported_language":
            continue
        if repo_key is not None and not _repo_text_matches(scope_ref.get("repo"), repo_key):
            continue
        language = scope_ref.get("language")
        if not isinstance(language, str) or not language:
            continue
        file_count = scope_ref.get("file_count")
        if isinstance(file_count, bool) or not isinstance(file_count, int):
            file_count = 0
        entry = by_language.setdefault(language, {"repo": scope_ref.get("repo"), "language": language, "file_count": 0})
        entry["file_count"] += file_count
    return [by_language[language] for language in sorted(by_language)]


def _uninstrumented_language_coverage_warnings(entries: list[JsonObject], *, repo: str | None = None) -> list[str]:
    if not entries:
        return []
    parts = ", ".join(
        f"{entry['language']} ({entry['file_count']} files)" if entry["file_count"] else entry["language"]
        for entry in entries
    )
    location = f"Repo {repo!r}" if repo else "Snapshot"
    return [
        f"{location} has uninstrumented source in languages with no allowlisted extractor: {parts}. "
        "Symbols, calls, endpoints, and events defined in those languages are not indexed, so an empty or "
        "partial result here is a coverage gap, not proof of absence."
    ]


def _symbol_payload_repo(payload: JsonObject) -> str | None:
    for key in ("target", "source"):
        resolution = payload.get(key)
        if not isinstance(resolution, dict):
            continue
        resolved_symbol = resolution.get("resolved_symbol")
        if isinstance(resolved_symbol, dict):
            repo = resolved_symbol.get("repo")
            if isinstance(repo, str) and repo:
                return repo
    return None


def _event_claim_contract(result_key: str) -> JsonObject:
    return {
        "scope": "indexed static event-channel facts only",
        "known_rows_field": result_key,
        "safety_rule": (
            "Known producer or consumer rows do not prove deploy safety, message compatibility, runtime activity, "
            "or absence of unindexed subscribers."
        ),
        "counting_rule": (
            "Report known static rows separately from unresolved runtime/config consumers, missing fact families, "
            "unsupported scopes, and coverage gaps."
        ),
        "required_caveat": (
            "For safe-deploy, time-window, schema, retry, delivery, or runtime-subscriber claims, inspect source/config "
            "and operational evidence beyond this static packet."
        ),
    }


def _event_channel_search_text(channel: JsonObject) -> str:
    identity = channel.get("identity", {})
    return " ".join(
        str(value)
        for value in [
            display_entity(channel),
            identity.get("channel_address"),
            identity.get("name"),
            identity.get("broker_kind"),
        ]
        if value is not None
    )


def _event_next_actions(*, found: bool, channel: str) -> list[str]:
    actions = [
        "For runtime claims such as `no consumers in the last 30 days`, inspect broker metrics, traces, logs, or deployment config; static KG event facts cannot prove time-window usage.",
        "Inspect source/config around returned event evidence before finalizing schema, handler, retry, or delivery-semantics claims.",
    ]
    if not found:
        actions.insert(
            0,
            f"Search source and deployment config for event channel {channel!r}; no indexed static event facts matched this query.",
        )
    return actions


def _unsupported_by_current_kg(tool: str, reason: str) -> JsonObject:
    return {
        "status": "unsupported_by_current_kg",
        "reason": reason,
        "missing_contract": tool,
        "coverage_warnings": [],
        "unsupported_scopes": [
            {
                "kind": tool,
                "reason": reason,
            }
        ],
        "next_actions": _unsupported_contract_next_actions(tool),
    }


def _unsupported_contract_next_actions(tool: str) -> list[str]:
    if tool == "deploy_blockers_for":
        return [
            "In the final answer, report canonical deploy blockers and must-deploy-before services as unknown unless explicit deploy orchestration, rollout, runbook, or deploy-blocker evidence is found.",
            "Do not turn static endpoint consumers or deploy_order_guidance into a recommended deploy order unless the user asks for a speculative rollout plan.",
            "Inspect deployment manifests, CI/CD config, service ownership docs, and source-level runtime dependencies before making deploy-blocker claims.",
            "Use `get_service_brief` or `planning_context` only as static context; absence of explicit deploy-blocker facts is not proof that deployment is safe.",
        ]
    return [
        "Fall back to source, config, or operational evidence for this unsupported contract.",
        "Treat this unsupported result as a coverage gap, not as evidence that the risk is absent.",
    ]


def _validate_declared_arguments(tool: McpTool, arguments: JsonObject) -> None:
    properties = tool.input_schema.get("properties", {})
    if not isinstance(properties, dict):
        raise ValueError(f"MCP tool {tool.name} has an invalid input schema")
    unknown = sorted(set(arguments) - set(properties))
    if unknown:
        raise ValueError(f"MCP tool {tool.name} does not accept argument(s): {', '.join(unknown)}")
    required = tool.input_schema.get("required", [])
    if not isinstance(required, list):
        raise ValueError(f"MCP tool {tool.name} has an invalid required-arguments schema")
    for field in required:
        if field not in arguments:
            raise ValueError(f"MCP tool {tool.name} requires argument: {field}")


def _matching_services(kg: KgSnapshot, query: str | None) -> list[JsonObject]:
    services = [entity for entity in kg.entities if entity.get("kind") == "Service"]
    if not query:
        return sorted(services, key=_service_sort_key)
    needle = query.lower()
    return sorted(
        [
            service
            for service in services
            if needle in _service_search_text(service).lower()
        ],
        key=_service_sort_key,
    )


def _service_row(kg: KgSnapshot, service: JsonObject) -> JsonObject:
    identity = service.get("identity", {})
    properties = service.get("properties", {})
    return {
        "service_id": service.get("entity_id"),
        "urn": service.get("urn"),
        "name": display_entity(service),
        "identity": identity,
        "repo": identity.get("repo") or properties.get("repo"),
        "namespace": identity.get("namespace"),
        "slug": identity.get("slug"),
        "evidence": kg.evidence_by_target.get(service.get("entity_id"), []),
    }


def _service_repo(service: JsonObject) -> str | None:
    identity = service.get("identity", {})
    properties = service.get("properties", {})
    if not isinstance(identity, dict):
        identity = {}
    if not isinstance(properties, dict):
        properties = {}
    repo = identity.get("repo") or properties.get("repo")
    return repo if isinstance(repo, str) and repo.strip() else None


def _planning_context_services_for_repo(kg: KgSnapshot, repo: str) -> list[JsonObject]:
    """Service rows whose repo identity matches a repo anchor.

    A repo anchor must surface its Service entity as the primary identity answer; without
    this the identity question has a real graph answer the packet hides, forcing the agent
    onto weaker packaging-metadata evidence.
    """
    matches = [
        service
        for service in kg.entities
        if service.get("kind") == "Service"
        and (svc_repo := _service_repo(service)) is not None
        and _repo_text_matches(svc_repo, repo)
    ]
    return [_service_row(kg, service) for service in sorted(matches, key=_service_sort_key)]


def _facts_touching_entity(kg: KgSnapshot, entity_id: str) -> list[JsonObject]:
    rows = []
    for fact in kg.facts:
        if fact.get("subject_id") != entity_id and fact.get("object_id") != entity_id:
            continue
        subject = kg.entities_by_id.get(fact["subject_id"])
        object_ = kg.entities_by_id.get(fact["object_id"])
        if not subject or not object_:
            continue
        rows.append(_fact_result(kg, fact, subject, object_))
    return rows


def _service_search_text(service: JsonObject) -> str:
    identity = service.get("identity", {})
    properties = service.get("properties", {})
    return " ".join(str(value) for value in [display_entity(service), *identity.values(), *properties.values()])


def _fact_result(kg: KgSnapshot, fact: JsonObject, subject: JsonObject, object_: JsonObject) -> JsonObject:
    row = {
        "fact_id": fact["fact_id"],
        "predicate": fact["predicate"],
        "canonical_status": _row_canonical_status(fact),
        "subject_canonical_status": _row_canonical_status(subject),
        "object_canonical_status": _row_canonical_status(object_),
        "subject": display_entity(subject),
        "object": display_entity(object_),
        "qualifier": fact.get("qualifier", {}),
        "evidence": kg.evidence_by_target.get(fact["fact_id"], []),
    }
    call_site = call_site_from_qualifier(fact.get("qualifier", {}))
    if call_site is not None:
        row["call_site"] = call_site
    if _is_event_predicate(row["predicate"]):
        row["linkage_status"] = "known_linked" if _is_known_event_fact(fact, object_) else "candidate_or_unlinked"
    if row["predicate"] == "DEPLOYS_VIA_CONFIG":
        row["linkage_status"] = "known_linked" if _is_known_deploy_fact(fact) else "candidate_or_unlinked"
    return row


def _row_canonical_status(row: JsonObject) -> str:
    value = row.get("canonical_status", "canonical")
    return value if isinstance(value, str) and value else "canonical"


def _is_event_predicate(value: object) -> bool:
    return value in _EVENT_FACT_PREDICATES


def _is_known_event_fact(fact: JsonObject, channel: JsonObject) -> bool:
    return (
        fact.get("predicate") in _DIRECTIONAL_EVENT_FACT_PREDICATES
        and _row_canonical_status(fact) == "canonical"
        and _row_canonical_status(channel) == "canonical"
    )


def _is_known_event_row(row: JsonObject) -> bool:
    return (
        row.get("predicate") in _DIRECTIONAL_EVENT_FACT_PREDICATES
        and row.get("canonical_status", "canonical") == "canonical"
        and row.get("object_canonical_status", "canonical") == "canonical"
    )


def _is_candidate_or_unlinked_event_row(row: JsonObject) -> bool:
    return _is_event_predicate(row.get("predicate")) and not _is_known_event_row(row)


def _is_known_deploy_fact(fact: JsonObject) -> bool:
    return fact.get("predicate") == "DEPLOYS_VIA_CONFIG" and _row_canonical_status(fact) == "canonical"


def _is_known_deploy_row(row: JsonObject) -> bool:
    return row.get("predicate") == "DEPLOYS_VIA_CONFIG" and row.get("canonical_status", "canonical") == "canonical"


def _endpoint_consumer_packet_for_service(kg: KgSnapshot, service: JsonObject, *, limit: int) -> JsonObject:
    return _endpoint_consumer_packet(
        kg,
        _exposed_endpoint_rows_for_service_id(kg, str(service["entity_id"])),
        limit=limit,
    )


def _endpoint_consumer_packet(kg: KgSnapshot, exposed_endpoint_rows: list[JsonObject], *, limit: int) -> JsonObject:
    matched_rows = _endpoint_consumer_rows_for_exposed_endpoints(kg, exposed_endpoint_rows)
    public_rows = _planning_context_public_rows(matched_rows)
    confidence_counts = _count_row_qualifier_values(public_rows, "confidence")
    host_resolution_counts = _count_row_qualifier_values(public_rows, "host_resolution_kind")
    consumer_keys = {
        str(row.get("consumer", {}).get("service_id") or row.get("consumer", {}).get("name"))
        for row in public_rows
        if isinstance(row.get("consumer"), dict)
    }
    return {
        "summary": {
            "consumer_fact_count": len(public_rows),
            "consumer_service_count": len(consumer_keys),
            "confidence_counts": confidence_counts,
            "host_resolution_kind_counts": host_resolution_counts,
            "match_basis": ENDPOINT_PATH_SHAPE_MATCH_BASIS,
            "section_limit": limit,
        },
        "consumers": public_rows[:limit],
        "truncated": len(public_rows) > limit,
        "coverage_note": (
            "These rows are candidate inbound endpoint consumers from static CALLS_ENDPOINT facts. "
            "Unresolved hosts require source or environment verification before runtime/deploy conclusions."
        ),
    }


def _endpoint_consumer_rows_for_exposed_endpoints(
    kg: KgSnapshot,
    exposed_endpoint_rows: list[JsonObject],
) -> list[JsonObject]:
    provider_index = _provider_endpoint_index(exposed_endpoint_rows)
    if not provider_index:
        return []
    provider_service_ids = {
        str(row.get("_subject", {}).get("entity_id"))
        for row in exposed_endpoint_rows
        if isinstance(row.get("_subject"), dict) and row.get("_subject", {}).get("entity_id")
    }
    provider_service_repos = {
        repo
        for row in exposed_endpoint_rows
        if isinstance(row.get("_subject"), dict)
        for repo in [_planning_context_entity_repo(row["_subject"])]
        if isinstance(repo, str) and repo.strip()
    }
    rows: list[JsonObject] = []
    for fact in kg.facts:
        if fact.get("predicate") != "CALLS_ENDPOINT":
            continue
        subject = kg.entities_by_id.get(fact.get("subject_id"))
        endpoint = kg.entities_by_id.get(fact.get("object_id"))
        if not subject or not endpoint or endpoint.get("kind") != "Endpoint":
            continue
        if str(subject.get("entity_id")) in provider_service_ids:
            continue
        if _same_repo_internal_endpoint_consumer(subject, provider_repos=provider_service_repos):
            continue
        endpoint_path = _endpoint_path(endpoint)
        if endpoint_path is None or endpoint_path not in provider_index:
            continue
        endpoint_method = _endpoint_method(endpoint, fact)
        provider_methods = provider_index[endpoint_path]
        if not _endpoint_methods_are_compatible(endpoint_method, provider_methods):
            continue
        rows.append(
            {
                **_planning_context_fact_result(kg, fact, subject, endpoint),
                "consumer": _endpoint_consumer_identity(subject),
                "matched_provider_endpoint": {
                    "path": endpoint_path,
                    "methods": sorted(provider_methods),
                },
                "match_basis": ENDPOINT_PATH_SHAPE_MATCH_BASIS,
            }
        )
    return _planning_context_dedupe_rows(rows)


def _same_repo_internal_endpoint_consumer(subject: JsonObject, *, provider_repos: set[str]) -> bool:
    if subject.get("kind") not in {"CodeModule", "CodeSymbol"}:
        return False
    repo = _planning_context_entity_repo(subject)
    return repo is not None and repo in provider_repos


def _exposed_endpoint_rows_for_service_id(kg: KgSnapshot, service_id: str) -> list[JsonObject]:
    rows: list[JsonObject] = []
    for fact in kg.facts:
        if fact.get("predicate") != "EXPOSES_ENDPOINT" or fact.get("subject_id") != service_id:
            continue
        service = kg.entities_by_id.get(fact.get("subject_id"))
        endpoint = kg.entities_by_id.get(fact.get("object_id"))
        if not service or not endpoint or endpoint.get("kind") != "Endpoint":
            continue
        rows.append(_planning_context_fact_result(kg, fact, service, endpoint))
    return rows


def _provider_endpoint_index(exposed_endpoint_rows: list[JsonObject]) -> dict[str, set[str]]:
    paths: dict[str, set[str]] = {}
    for row in exposed_endpoint_rows:
        endpoint = row.get("_object")
        fact = row.get("_fact")
        if not isinstance(endpoint, dict) or not isinstance(fact, dict):
            continue
        path = _endpoint_path(endpoint)
        if path is None:
            continue
        methods = paths.setdefault(path, set())
        method = _endpoint_method(endpoint, fact)
        if method is not None:
            methods.add(method)
    return paths


def _endpoint_path(endpoint: JsonObject) -> str | None:
    identity = endpoint.get("identity", {})
    if not isinstance(identity, dict):
        return None
    path = identity.get("path")
    if not isinstance(path, str) or not path.strip():
        return None
    return normalize_endpoint_path_shape(path)


def _endpoint_method(endpoint: JsonObject, fact: JsonObject | None = None) -> str | None:
    identity = endpoint.get("identity", {})
    method = identity.get("method") if isinstance(identity, dict) else None
    if method is None and fact is not None:
        qualifier = fact.get("qualifier", {})
        method = qualifier.get("method") if isinstance(qualifier, dict) else None
    if not isinstance(method, str) or not method.strip():
        return None
    return method.strip().upper()


def _endpoint_methods_are_compatible(consumer_method: str | None, provider_methods: set[str]) -> bool:
    if consumer_method is None or not provider_methods:
        return False
    return "ANY" in provider_methods or consumer_method in provider_methods


def _endpoint_consumer_identity(subject: JsonObject) -> JsonObject:
    identity = subject.get("identity", {})
    properties = subject.get("properties", {})
    if not isinstance(identity, dict):
        identity = {}
    if not isinstance(properties, dict):
        properties = {}
    return {
        "service_id": subject.get("entity_id"),
        "kind": subject.get("kind"),
        "name": display_entity(subject),
        "repo": identity.get("repo") or properties.get("repo"),
        "namespace": identity.get("namespace"),
        "slug": identity.get("slug"),
    }


def _count_row_qualifier_values(rows: list[JsonObject], key: str) -> JsonObject:
    counts: dict[str, int] = {}
    for row in rows:
        qualifier = row.get("qualifier", {})
        if not isinstance(qualifier, dict):
            continue
        value = qualifier.get(key)
        if value is None:
            continue
        value_key = str(value)
        counts[value_key] = counts.get(value_key, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _service_operational_surfaces(kg: KgSnapshot, service: JsonObject, *, limit: int) -> JsonObject:
    repo = _planning_context_entity_repo(service)
    service_id = service.get("entity_id")
    exposed_endpoint_rows = _exposed_endpoint_rows_for_service_id(kg, str(service_id)) if isinstance(service_id, str) else []
    endpoint_consumer_rows = _endpoint_consumer_rows_for_exposed_endpoints(kg, exposed_endpoint_rows)
    direct_domain_rows: list[JsonObject] = []
    deploy_target_rows: list[JsonObject] = []
    deploy_link_rows: list[JsonObject] = []
    domain_route_rows: list[JsonObject] = []
    unlinked_domain_route_rows: list[JsonObject] = []
    unlinked_deploy_link_rows: list[JsonObject] = []
    service_deploy_targets = {
        fact.get("object_id")
        for fact in kg.facts
        if fact.get("predicate") == "DEPLOYS_VIA_CONFIG"
        and fact.get("subject_id") == service_id
        and _is_known_deploy_fact(fact)
        and isinstance(fact.get("object_id"), str)
    }

    for entity in kg.entities:
        if entity.get("kind") != "DeployTarget":
            continue
        if not _repo_text_matches(_planning_context_entity_repo(entity), repo):
            continue
        deploy_target_rows.append(
            _operational_entity_row(kg, entity, match_basis="deploy_target_repo_equals_service_repo")
        )

    for fact in kg.facts:
        predicate = fact.get("predicate")
        if predicate not in {"REFERENCES_DOMAIN", "ROUTES_DOMAIN_TO_DEPLOY", "DEPLOYS_VIA_CONFIG"}:
            continue
        subject = kg.entities_by_id.get(fact.get("subject_id"))
        object_ = kg.entities_by_id.get(fact.get("object_id"))
        if not subject or not object_:
            continue
        row = _fact_result(kg, fact, subject, object_)
        if predicate == "DEPLOYS_VIA_CONFIG" and subject.get("entity_id") == service_id:
            row = {
                **row,
                "match_basis": "service_deploys_via_config"
                if _is_known_deploy_fact(fact)
                else "candidate_service_deploys_via_config",
            }
            if _is_known_deploy_fact(fact):
                deploy_link_rows.append(row)
            else:
                unlinked_deploy_link_rows.append(row)
            continue
        if _repo_text_matches(_planning_context_entity_repo(subject), repo) or _repo_text_matches(
            _planning_context_entity_repo(object_), repo
        ):
            direct_domain_rows.append(row)
            if predicate == "ROUTES_DOMAIN_TO_DEPLOY":
                domain_route_rows.append(
                    {
                        **row,
                        "match_basis": "domain_or_deploy_target_repo_equals_service_repo",
                    }
                )
            continue
        if predicate == "ROUTES_DOMAIN_TO_DEPLOY" and fact.get("object_id") in service_deploy_targets:
            domain_route_rows.append(
                {
                    **row,
                    "match_basis": "route_deploy_target_linked_to_service",
                }
            )
        elif predicate == "ROUTES_DOMAIN_TO_DEPLOY":
            unlinked_domain_route_rows.append(
                {
                    **row,
                    "relationship_to_service": "unlinked_fleet_route",
                }
            )

    direct_domain_rows = _planning_context_dedupe_rows(direct_domain_rows)
    domain_route_rows = _planning_context_dedupe_rows(domain_route_rows)
    deploy_target_rows = _planning_context_dedupe_rows(deploy_target_rows)
    deploy_link_rows = _planning_context_dedupe_rows(deploy_link_rows)
    unlinked_domain_route_rows = _planning_context_dedupe_rows(unlinked_domain_route_rows)
    unlinked_deploy_link_rows = _planning_context_dedupe_rows(unlinked_deploy_link_rows)
    endpoint_consumer_rows = _planning_context_dedupe_rows(endpoint_consumer_rows)
    deploy_runtime_units = _service_deploy_runtime_units(kg, service, limit=limit)
    deploy_order_guidance = _service_deploy_order_guidance(endpoint_consumer_rows, limit=limit)
    deploy_evidence_count = len(deploy_target_rows) + len(deploy_link_rows)
    missing_contract_items = _operational_missing_contracts(
        domain_route_rows=domain_route_rows,
        deploy_target_rows=deploy_target_rows,
        deploy_link_rows=deploy_link_rows,
        unlinked_domain_route_rows=unlinked_domain_route_rows,
        unlinked_deploy_link_rows=unlinked_deploy_link_rows,
        endpoint_consumer_rows=endpoint_consumer_rows,
    )
    return {
        "summary": {
            "direct_domain_reference_count": len(direct_domain_rows),
            "domain_route_candidate_count": len(domain_route_rows),
            "deploy_target_candidate_count": deploy_evidence_count,
            "deploy_target_entity_count": len(deploy_target_rows),
            "deploy_link_fact_count": len(deploy_link_rows),
            "candidate_or_unlinked_deploy_link_count": len(unlinked_deploy_link_rows),
            "deploy_runtime_unit_count": len(deploy_runtime_units),
            "unlinked_domain_route_count": len(unlinked_domain_route_rows),
            "endpoint_consumer_fact_count": len(endpoint_consumer_rows),
            "practical_deploy_order_guidance_count": len(deploy_order_guidance["practical_deploy_order"]),
            "match_basis": "structured_repo_identity_only",
            "section_limit": limit,
        },
        "missing_fact_families": [str(item["contract"]) for item in missing_contract_items],
        "evidence_buckets": list(OPERATIONAL_EVIDENCE_BUCKETS),
        "bucket_descriptions": OPERATIONAL_BUCKET_DESCRIPTIONS,
        "evidence_partition": _operational_evidence_partition(
            direct_domain_rows=direct_domain_rows,
            domain_route_rows=domain_route_rows,
            deploy_target_rows=deploy_target_rows,
            deploy_link_rows=deploy_link_rows,
            unlinked_domain_route_rows=unlinked_domain_route_rows,
            unlinked_deploy_link_rows=unlinked_deploy_link_rows,
            missing_contract_items=missing_contract_items,
            limit=limit,
        ),
        "deploy_runtime_units": deploy_runtime_units[:limit],
        "deploy_order_guidance": deploy_order_guidance,
        "direct_domain_references": direct_domain_rows[:limit],
        "domain_route_candidates": domain_route_rows[:limit],
        "deploy_target_candidates": deploy_target_rows[:limit],
        "deploy_link_facts": deploy_link_rows[:limit],
        "endpoint_consumers": _compact_endpoint_consumer_rows(endpoint_consumer_rows, limit=limit),
        "unlinked_domain_route_samples": unlinked_domain_route_rows[:limit],
        "candidate_or_unlinked_deploy_links": unlinked_deploy_link_rows[:limit],
        "truncated": any(
            len(rows) > limit
            for rows in (
                direct_domain_rows,
                domain_route_rows,
                deploy_target_rows,
                deploy_link_rows,
                deploy_runtime_units,
                endpoint_consumer_rows,
                unlinked_domain_route_rows,
                unlinked_deploy_link_rows,
            )
        ),
        "coverage_note": (
            "domain_route_candidates and deploy_target_candidates require exact repo-identity evidence; deploy_link_facts require known service-to-target evidence; candidate_or_unlinked_deploy_links are source leads only; deploy_runtime_units join known DEPLOYS_VIA_CONFIG to domain/ingress routes by deploy target. endpoint_consumers are static caller facts; deploy_order_guidance is a practical compatibility inference, not a canonical deploy-blocker fact. unlinked_domain_route_samples are fleet config evidence and are not service deploy-blocker facts."
        ),
    }


def _operational_evidence_partition(
    *,
    direct_domain_rows: list[JsonObject],
    domain_route_rows: list[JsonObject],
    deploy_target_rows: list[JsonObject],
    deploy_link_rows: list[JsonObject],
    unlinked_domain_route_rows: list[JsonObject],
    unlinked_deploy_link_rows: list[JsonObject],
    missing_contract_items: list[JsonObject],
    limit: int,
) -> JsonObject:
    known_count = len(direct_domain_rows) + len(domain_route_rows) + len(deploy_target_rows) + len(deploy_link_rows)
    return {
        OPERATIONAL_KNOWN_LINKED: {
            "status": "found" if known_count else "empty",
            "interpretation": OPERATIONAL_BUCKET_DESCRIPTIONS[OPERATIONAL_KNOWN_LINKED],
            "direct_domain_references": direct_domain_rows[:limit],
            "domain_routes": domain_route_rows[:limit],
            "deploy_targets": deploy_target_rows[:limit],
            "deploy_links": deploy_link_rows[:limit],
            "counts": {
                "direct_domain_reference_count": len(direct_domain_rows),
                "domain_route_count": len(domain_route_rows),
                "deploy_target_count": len(deploy_target_rows) + len(deploy_link_rows),
                "deploy_target_entity_count": len(deploy_target_rows),
                "deploy_link_fact_count": len(deploy_link_rows),
            },
        },
        OPERATIONAL_UNLINKED_EVIDENCE: {
            "status": "found" if unlinked_domain_route_rows or unlinked_deploy_link_rows else "empty",
            "interpretation": OPERATIONAL_BUCKET_DESCRIPTIONS[OPERATIONAL_UNLINKED_EVIDENCE],
            "domain_route_samples": unlinked_domain_route_rows[:limit],
            "deploy_link_samples": unlinked_deploy_link_rows[:limit],
            "counts": {
                "domain_route_sample_count": len(unlinked_domain_route_rows),
                "deploy_link_sample_count": len(unlinked_deploy_link_rows),
            },
        },
        OPERATIONAL_MISSING_CONTRACTS: {
            "status": "present",
            "interpretation": OPERATIONAL_BUCKET_DESCRIPTIONS[OPERATIONAL_MISSING_CONTRACTS],
            "items": missing_contract_items,
        },
    }


def _operational_missing_contracts(
    *,
    domain_route_rows: list[JsonObject],
    deploy_target_rows: list[JsonObject],
    deploy_link_rows: list[JsonObject],
    unlinked_domain_route_rows: list[JsonObject],
    unlinked_deploy_link_rows: list[JsonObject],
    endpoint_consumer_rows: list[JsonObject] | None = None,
) -> list[JsonObject]:
    missing = [
        {
            "contract": "canonical_service_deploy_blocker",
            "status": "unsupported_by_current_kg",
            "meaning": (
                "The current KG has no canonical relation proving another service must deploy before this service."
            ),
        },
        {
            "contract": "runtime_host_resolution",
            "status": "not_proven",
            "meaning": (
                "Static endpoint/domain/config evidence does not prove which runtime host or environment a client uses."
            ),
        },
    ]
    if not domain_route_rows and not deploy_target_rows and not deploy_link_rows:
        missing.append(
            {
                "contract": "service_to_deploy_target",
                "status": "missing",
                "meaning": (
                    "No exact repo-linked DeployTarget or ROUTES_DOMAIN_TO_DEPLOY row is connected to this service."
                ),
            }
        )
    if unlinked_domain_route_rows:
        missing.append(
            {
                "contract": "unlinked_route_to_service",
                "status": "not_proven",
                "meaning": (
                    "Fleet domain-route evidence exists, but the current KG does not connect it to this service."
                ),
            }
        )
    if unlinked_deploy_link_rows:
        missing.append(
            {
                "contract": "candidate_service_to_deploy_target",
                "status": "not_proven",
                "meaning": (
                    "Candidate DEPLOYS_VIA_CONFIG rows identify possible service-to-deploy links but are not known deploy evidence."
                ),
            }
        )
    if endpoint_consumer_rows:
        missing.append(
            {
                "contract": "endpoint_contract_change_classification",
                "status": "not_proven",
                "meaning": (
                    "Static endpoint consumers prove compatibility risk candidates, but the KG does not know whether the provider change is backward-compatible."
                ),
            }
        )
    return missing


def _service_deploy_runtime_units(kg: KgSnapshot, service: JsonObject, *, limit: int) -> list[JsonObject]:
    service_id = service.get("entity_id")
    if not isinstance(service_id, str):
        return []
    route_facts_by_target: dict[str, list[JsonObject]] = {}
    for fact in kg.facts:
        if fact.get("predicate") != "ROUTES_DOMAIN_TO_DEPLOY":
            continue
        target_id = fact.get("object_id")
        if isinstance(target_id, str):
            route_facts_by_target.setdefault(target_id, []).append(fact)

    units = []
    for fact in kg.facts:
        if not _is_known_deploy_fact(fact) or fact.get("subject_id") != service_id:
            continue
        target = kg.entities_by_id.get(fact.get("object_id"))
        if not target:
            continue
        qualifier = fact.get("qualifier") if isinstance(fact.get("qualifier"), dict) else {}
        target_id = str(fact.get("object_id"))
        ingress_or_domain_routes = []
        for route_fact in route_facts_by_target.get(target_id, []):
            route_row = _service_deploy_route_row(kg, route_fact)
            if route_row:
                ingress_or_domain_routes.append(route_row)
            if len(ingress_or_domain_routes) >= limit:
                break
        units.append(
            {
                "status": "known_linked_deploy_unit",
                "service": _operational_compact_entity(service),
                "deploy_target": _operational_compact_entity(target),
                "deploy_kind": _operational_deploy_kind(target, qualifier),
                "deploy_details": _operational_deploy_details(qualifier),
                "ingress_or_domain_routes": ingress_or_domain_routes,
                "evidence_coordinates": _operational_evidence_coordinates(kg.evidence_by_target.get(fact.get("fact_id"), [])),
            }
        )
    return sorted(_dedupe_operational_units(units), key=_operational_unit_sort_key)[:limit]


def _service_deploy_route_row(kg: KgSnapshot, fact: JsonObject) -> JsonObject:
    domain = kg.entities_by_id.get(fact.get("subject_id"))
    target = kg.entities_by_id.get(fact.get("object_id"))
    if not domain or not target:
        return {}
    qualifier = fact.get("qualifier") if isinstance(fact.get("qualifier"), dict) else {}
    return {
        "domain": _operational_compact_entity(domain),
        "deploy_target": _operational_compact_entity(target),
        "deploy_kind": _operational_deploy_kind(target, qualifier),
        "route_source_kind": qualifier.get("source_kind"),
        "backend_service": qualifier.get("backend_service"),
        "backend_service_ports": qualifier.get("backend_service_ports", []),
        "ingress_path": qualifier.get("ingress_path"),
        "namespace": qualifier.get("namespace"),
        "workload": qualifier.get("workload"),
        "match_basis": qualifier.get("match_basis"),
        "evidence_coordinates": _operational_evidence_coordinates(kg.evidence_by_target.get(fact.get("fact_id"), [])),
    }


def _service_deploy_order_guidance(endpoint_consumer_rows: list[JsonObject], *, limit: int) -> JsonObject:
    public_consumers = _compact_endpoint_consumer_rows(endpoint_consumer_rows, limit=max(limit, len(endpoint_consumer_rows)))
    returned_consumers = public_consumers[:limit]
    practical_order = []
    for row in returned_consumers:
        consumer = row.get("consumer") if isinstance(row.get("consumer"), dict) else {}
        practical_order.append(
            {
                "status": "practical_inference",
                "consumer": consumer,
                "matched_provider_endpoint": row.get("matched_provider_endpoint", {}),
                "recommendation": (
                    "If the provider endpoint changes incompatibly, make this consumer compatible before or alongside the provider deploy."
                ),
                "basis": "static CALLS_ENDPOINT consumer matched to a provider endpoint; not a canonical deploy-blocker fact",
                "missing_fact_families": [
                    "canonical_service_deploy_blocker",
                    "runtime_host_resolution",
                    "endpoint_contract_change_classification",
                ],
                "evidence_coordinates": row.get("evidence_coordinates", []),
            }
        )
    return {
        "status": "inference_available" if practical_order else "no_static_endpoint_consumers",
        "inference_contract": (
            "deploy_order_guidance is a compatibility-risk recommendation derived from static endpoint consumers. It must not be presented as a canonical deploy-blocker relation."
        ),
        "proven_endpoint_consumers": returned_consumers,
        "practical_deploy_order": practical_order,
        "truncated": len(public_consumers) > limit,
    }


def _compact_endpoint_consumer_rows(rows: list[JsonObject], *, limit: int) -> list[JsonObject]:
    compact_rows = []
    for row in _planning_context_public_rows(rows)[:limit]:
        compact_rows.append(
            {
                "consumer": row.get("consumer", {}),
                "matched_provider_endpoint": row.get("matched_provider_endpoint", {}),
                "match_basis": row.get("match_basis"),
                "qualifier": row.get("qualifier", {}),
                "evidence_coordinates": _operational_evidence_coordinates(row.get("evidence", [])),
            }
        )
    return compact_rows


def _operational_compact_entity(entity: JsonObject) -> JsonObject:
    identity = entity.get("identity")
    properties = entity.get("properties")
    if not isinstance(identity, dict):
        identity = {}
    if not isinstance(properties, dict):
        properties = {}
    return {
        "entity_id": entity.get("entity_id"),
        "kind": entity.get("kind"),
        "name": display_entity(entity),
        "repo": identity.get("repo") or properties.get("repo"),
        "slug": identity.get("slug"),
        "type": identity.get("type"),
        "target": identity.get("target"),
        "path": identity.get("path") or properties.get("path"),
    }


def _operational_deploy_kind(target: JsonObject, qualifier: JsonObject) -> str | None:
    identity = target.get("identity") if isinstance(target.get("identity"), dict) else {}
    target_type = qualifier.get("target_type")
    deploy_type = identity.get("type") or target_type
    return str(deploy_type) if deploy_type else None


def _operational_deploy_details(qualifier: JsonObject) -> JsonObject:
    keys = (
        "source_kind",
        "target_type",
        "kubernetes_kind",
        "namespace",
        "workload",
        "containers",
        "images",
        "ownership_basis",
        "path",
    )
    return {key: qualifier[key] for key in keys if key in qualifier}


def _operational_evidence_coordinates(evidence_rows: object, *, limit: int = 2) -> list[JsonObject]:
    if not isinstance(evidence_rows, list):
        return []
    coordinates = []
    for evidence in evidence_rows:
        if not isinstance(evidence, dict):
            continue
        bytes_ref = evidence.get("bytes_ref")
        if not isinstance(bytes_ref, dict):
            continue
        coordinate = {
            "repo": bytes_ref.get("repo_name") or bytes_ref.get("repo"),
            "path": bytes_ref.get("path"),
            "line_start": bytes_ref.get("line_start"),
            "line_end": bytes_ref.get("line_end"),
            "confidence": evidence.get("confidence"),
            "source_system": evidence.get("source_system"),
        }
        coordinates.append({key: value for key, value in coordinate.items() if value is not None})
        if len(coordinates) >= limit:
            break
    return coordinates


def _dedupe_operational_units(rows: list[JsonObject]) -> list[JsonObject]:
    seen = set()
    deduped = []
    for row in rows:
        service = row.get("service") if isinstance(row.get("service"), dict) else {}
        target = row.get("deploy_target") if isinstance(row.get("deploy_target"), dict) else {}
        key = (service.get("entity_id"), target.get("entity_id"), row.get("deploy_kind"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def _operational_unit_sort_key(row: JsonObject) -> tuple[str, str]:
    target = row.get("deploy_target") if isinstance(row.get("deploy_target"), dict) else {}
    return (str(row.get("deploy_kind") or ""), str(target.get("name") or ""))


def _operational_entity_row(kg: KgSnapshot, entity: JsonObject, *, match_basis: str) -> JsonObject:
    return {
        "entity_id": entity.get("entity_id"),
        "kind": entity.get("kind"),
        "name": display_entity(entity),
        "urn": entity.get("urn"),
        "identity": entity.get("identity", {}),
        "properties": entity.get("properties", {}),
        "match_basis": match_basis,
        "evidence": kg.evidence_by_target.get(entity.get("entity_id"), []),
    }


def _service_sort_key(service: JsonObject) -> tuple[str, str]:
    identity = service.get("identity", {})
    return (str(identity.get("namespace") or ""), str(identity.get("slug") or display_entity(service)))


def _required_string(arguments: JsonObject, field: str) -> str:
    value = arguments.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"MCP tool argument {field!r} must be a non-empty string")
    return value.strip()


def _optional_string(arguments: JsonObject, field: str) -> str | None:
    value = arguments.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"MCP tool argument {field!r} must be a string")
    stripped = value.strip()
    return stripped or None


def _optional_int(arguments: JsonObject, field: str) -> int | None:
    value = arguments.get(field)
    if value is None:
        return None
    return _bounded_int(value, field=field, minimum=1, maximum=10_000_000)


def _optional_bool(arguments: JsonObject, field: str, *, default: bool) -> bool:
    value = arguments.get(field, default)
    if not isinstance(value, bool):
        raise ValueError(f"MCP tool argument {field!r} must be a boolean")
    return value


def _required_string_list(arguments: JsonObject, field: str) -> list[str]:
    value = arguments.get(field)
    if not isinstance(value, list) or not value:
        raise ValueError(f"MCP tool argument {field!r} must be a non-empty list of strings")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"MCP tool argument {field!r} must be a non-empty list of strings")
        normalized.append(item.strip())
    return normalized


def _optional_string_list(arguments: JsonObject, field: str) -> list[str]:
    if field not in arguments:
        return []
    value = arguments[field]
    if not isinstance(value, list):
        raise ValueError(f"MCP tool argument {field!r} must be a list of strings")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"MCP tool argument {field!r} must be a list of non-empty strings")
        normalized.append(item.strip())
    return normalized


def _optional_review_surfaces_tolerant(
    arguments: JsonObject, field: str
) -> tuple[list[str], list[str]]:
    """review_context call site: known surfaces + unknown tokens (verbatim, no error)."""
    surfaces: list[str] = []
    unknown: list[str] = []
    seen_unknown: set[str] = set()
    for value in _optional_string_list(arguments, field):
        normalized_value = value.strip().lower().replace("-", "_").replace(" ", "_")
        if normalized_value in REVIEW_CONTEXT_BUILTIN_SECTION_ALIASES:
            continue
        canonical = REVIEW_CONTEXT_SURFACE_ALIASES.get(normalized_value)
        if canonical is None:
            if normalized_value not in seen_unknown:
                seen_unknown.add(normalized_value)
                unknown.append(value)
            continue
        if canonical not in surfaces:
            surfaces.append(canonical)
    return surfaces, unknown


def _optional_review_section_aliases(arguments: JsonObject, field: str) -> set[str]:
    sections: set[str] = set()
    for value in _optional_string_list(arguments, field):
        normalized_value = value.strip().lower().replace("-", "_").replace(" ", "_")
        if normalized_value in REVIEW_CONTEXT_OWNERSHIP_SECTION_ALIASES:
            sections.add("ownership_context")
    return sections


def _optional_changed_ranges(arguments: JsonObject, field: str) -> list[JsonObject]:
    if field not in arguments:
        return []
    value = arguments[field]
    if not isinstance(value, list):
        raise ValueError(f"MCP tool argument {field!r} must be a list of changed-range objects")
    ranges: list[JsonObject] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError(f"MCP tool argument {field!r} must be a list of changed-range objects")
        unknown = sorted(set(item) - {"path", "start_line", "end_line"})
        if unknown:
            raise ValueError(f"MCP tool argument {field!r} range does not accept field(s): {', '.join(unknown)}")
        path = item.get("path")
        start_line = item.get("start_line")
        end_line = item.get("end_line")
        if not isinstance(path, str) or not path.strip():
            raise ValueError(f"MCP tool argument {field!r} requires non-empty range paths")
        if isinstance(start_line, bool) or not isinstance(start_line, int) or start_line < 1:
            raise ValueError(f"MCP tool argument {field!r} requires integer start_line >= 1")
        if isinstance(end_line, bool) or not isinstance(end_line, int) or end_line < start_line:
            raise ValueError(f"MCP tool argument {field!r} requires integer end_line >= start_line")
        ranges.append({"path": path.strip(), "start_line": start_line, "end_line": end_line})
    return ranges


def _limit(arguments: JsonObject) -> int:
    return _bounded_int(arguments.get("limit", 25), field="limit", minimum=1, maximum=100)


def _bounded_int(value: object, *, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"MCP tool argument {field!r} must be an integer")
    if isinstance(value, int):
        raw = value
    else:
        raise ValueError(f"MCP tool argument {field!r} must be an integer")
    if raw < minimum or raw > maximum:
        raise ValueError(f"MCP tool argument {field!r} must be between {minimum} and {maximum}")
    return raw


def _object_schema(properties: JsonObject, required: list[str] | None = None) -> JsonObject:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


def _string_schema(description: str) -> JsonObject:
    return {"type": "string", "description": description}


def _nullable_string_schema(description: str) -> JsonObject:
    return {"type": ["string", "null"], "description": description}


def _nullable_line_schema() -> JsonObject:
    return {"type": ["integer", "null"], "minimum": 1, "description": "Optional source line for disambiguation."}


def _limit_schema() -> JsonObject:
    return {"type": "integer", "minimum": 1, "maximum": 100, "default": 25}


def _symbol_properties() -> JsonObject:
    return {
        "symbol": _string_schema(
            "Symbol name or exact qualified name. If the user supplied only an unqualified symbol name, call with that name first so the graph can surface ambiguity; add path/line only from a user-provided location or a prior disambiguation candidate. If a prior result was ambiguous, retry with a candidate `qualified_name`."
        ),
        "path": _nullable_string_schema("Optional source-file path for disambiguation."),
        "line": _nullable_line_schema(),
        "include_all": {"type": "boolean", "default": False},
        "limit": _limit_schema(),
    }


def _planning_context_properties() -> JsonObject:
    return {
        "query": _nullable_string_schema("Optional exact identifier query when no structured anchor is known."),
        "repo": _nullable_string_schema("Repository identifier anchor."),
        "path": _nullable_string_schema("File path anchor."),
        "line": _nullable_line_schema(),
        "symbol": _nullable_string_schema("Symbol anchor."),
        "service": _nullable_string_schema("Service anchor."),
        "package": _nullable_string_schema("Package/module anchor."),
        "endpoint": _nullable_string_schema("Endpoint path anchor."),
        "event_channel": _nullable_string_schema("Event channel anchor."),
        "domain": _nullable_string_schema("Domain anchor."),
        "limit": _limit_schema(),
    }


def _changed_range_schema() -> JsonObject:
    return {
        "type": "object",
        "properties": {
            "path": _string_schema("Changed file path."),
            "start_line": {"type": "integer", "minimum": 1},
            "end_line": {"type": "integer", "minimum": 1},
        },
        "required": ["path", "start_line", "end_line"],
        "additionalProperties": False,
    }


def _review_context_properties() -> JsonObject:
    return {
        "repo": _string_schema("Repository identifier for the review target."),
        "changed_files": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "description": "Changed file paths to review.",
        },
        "changed_ranges": {
            "type": "array",
            "items": _changed_range_schema(),
            "description": "Optional changed line ranges for narrowing file symbols.",
        },
        "limit": _limit_schema(),
        "requested_surfaces": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Optional impact surfaces named by the review prompt. Supported canonical values include "
                f"{', '.join(REVIEW_CONTEXT_SURFACES)}; common aliases such as UI, workers, SQS, tracking, schemas, "
                "and contracts are accepted. Built-in review sections and broad answer categories such as callers, "
                "reverse_impact, services, and deployables are always returned or covered by other packet sections, "
                "and may be requested as no-op aliases. Owner/maintainer requests are accepted as explicit coverage "
                "gaps that point to planning_context.ownership_context. "
                "Unknown surface names are not rejected — they produce unsupported_or_unlinked surface_status rows "
                "with source_inspection_terms so the agent can continue with manual inspection."
            ),
        },
        "include_deploy_blockers": {"type": "boolean", "default": False},
        "include_unlinked_leads": {
            "type": "boolean",
            "default": False,
            "description": (
                "Opt in to broad unlinked namespace/name leads when changed ranges cannot be anchored to symbols. "
                "By default, file-anchor-only PR-review packets stay compact and point back to source inspection."
            ),
        },
    }


def _planning_context(kg: KgSnapshot, arguments: JsonObject) -> JsonObject:
    limit = _limit(arguments)
    query = _optional_string(arguments, "query")
    anchors = _planning_context_anchors(arguments)
    line = _optional_int(arguments, "line")
    if _is_planning_context_fleet_request(query=query, line=line, anchors=anchors):
        services = _planning_context_fleet_services(kg, limit=min(limit, PLANNING_CONTEXT_SECTION_LIMIT))
        return _planning_context_output(
            kg=kg,
            query=query,
            anchors=anchors,
            services=services,
            symbols=[],
            dependencies=[],
            endpoints=[],
            endpoint_consumers=[],
            event_channels=[],
            candidate_or_unlinked_event_channels=[],
            domains=[],
            next_actions=[
                "Read runtime_architecture.answer_packet for a fleet runtime map; if output_budget is present and truncated is true, use narrower repo/service/domain/endpoint anchors for omitted detail."
            ],
            status="found" if services else "not_found",
        )

    if query is not None and not any(anchors.values()) and line is None:
        return _planning_context_from_query(kg, query=query, limit=limit)

    services: list[JsonObject] = []
    symbols: list[JsonObject] = []
    dependencies: list[JsonObject] = []
    endpoints: list[JsonObject] = []
    endpoint_consumers: list[JsonObject] = []
    event_channels: list[JsonObject] = []
    candidate_or_unlinked_event_channels: list[JsonObject] = []
    domains: list[JsonObject] = []
    next_actions: list[str] = []

    if anchors["service"]:
        matches = _matching_services(kg, anchors["service"])
        services = [_service_row(kg, service) for service in matches[:limit]]
        if len(matches) > 1:
            next_actions.extend(_service_refinement_actions(services))
            return _planning_context_output(
                kg=kg,
                query=query,
                anchors=anchors,
                services=services,
                symbols=symbols,
                dependencies=dependencies,
                endpoints=endpoints,
                endpoint_consumers=endpoint_consumers,
                event_channels=event_channels,
                candidate_or_unlinked_event_channels=candidate_or_unlinked_event_channels,
                domains=domains,
                next_actions=next_actions,
                status="ambiguous",
                line=line,
            )
        if len(matches) == 1:
            related = _planning_context_service_related_rows(kg, matches[0], limit=limit)
            dependencies = _planning_context_dedupe_rows(dependencies + related["dependencies"])
            endpoints = _planning_context_dedupe_rows(endpoints + related["endpoints"])
            endpoint_consumers = _planning_context_dedupe_rows(endpoint_consumers + related["endpoint_consumers"])
            event_channels = _planning_context_dedupe_rows(event_channels + related["event_channels"])
            candidate_or_unlinked_event_channels = _planning_context_dedupe_rows(
                candidate_or_unlinked_event_channels + related["candidate_or_unlinked_event_channels"]
            )
            domains = _planning_context_dedupe_rows(domains + related["domains"])
    if anchors["symbol"]:
        resolution = kg.lookup_symbol(anchors["symbol"], limit=limit, path=anchors["path"], line=line)
        if resolution["status"] == "ambiguous":
            symbols = list(resolution.get("candidates", []))[:limit]
            next_actions.extend(_symbol_refinement_actions(symbols))
            return _planning_context_output(
                kg=kg,
                query=query,
                anchors=anchors,
                services=services,
                symbols=symbols,
                dependencies=dependencies,
                endpoints=endpoints,
                endpoint_consumers=endpoint_consumers,
                event_channels=event_channels,
                candidate_or_unlinked_event_channels=candidate_or_unlinked_event_channels,
                domains=domains,
                next_actions=next_actions,
                status="ambiguous",
                line=line,
            )
        if resolution["status"] == "resolved" and resolution.get("resolved_symbol") is not None:
            symbols = [resolution["resolved_symbol"]]

    if anchors["repo"]:
        services = _planning_context_dedupe_rows(
            services + _planning_context_services_for_repo(kg, anchors["repo"])
        )[:limit]
        dependencies = _planning_context_dedupe_rows(
            dependencies + _planning_context_collect_rows(_planning_context_repo_matches(kg, anchors["repo"]), limit=limit)
        )
    if anchors["package"]:
        dependencies = _planning_context_dedupe_rows(
            dependencies + _planning_context_collect_rows(_planning_context_package_matches(kg, anchors["package"]), limit=limit)
        )
    if anchors["endpoint"]:
        endpoints = _planning_context_dedupe_rows(
            endpoints + _planning_context_collect_rows(_planning_context_endpoint_matches(kg, anchors["endpoint"]), limit=limit)
        )
    if anchors["event_channel"]:
        event_channels = _planning_context_dedupe_rows(
            event_channels
            + _planning_context_collect_rows(_planning_context_event_matches(kg, anchors["event_channel"]), limit=limit)
        )
        candidate_or_unlinked_event_channels = _planning_context_dedupe_rows(
            candidate_or_unlinked_event_channels
            + _planning_context_collect_rows(
                _planning_context_event_matches(kg, anchors["event_channel"], known_only=False),
                limit=limit,
            )
        )
    if anchors["domain"]:
        domains = _planning_context_dedupe_rows(
            domains + _planning_context_collect_rows(_planning_context_domain_matches(kg, anchors["domain"]), limit=limit)
        )
    if anchors["path"] and not anchors["symbol"]:
        path_symbols = kg.symbols_in_file(anchors["path"], limit=10_000)
        rows = list(path_symbols.get("symbols", []))
        if line is not None:
            rows = [row for row in rows if _line_matches_symbol(line, row)]
        symbols = rows[:limit]
    if line is not None and anchors["path"] is None and anchors["symbol"] is None and not any(
        anchors[key] for key in ("repo", "service", "package", "endpoint", "event_channel", "domain")
    ):
        next_actions.append("Add `path` or `symbol` with `line` to target a concrete source location.")
        return _planning_context_output(
            kg=kg,
            query=query,
            anchors=anchors,
            services=services,
            symbols=symbols,
            dependencies=dependencies,
            endpoints=endpoints,
            endpoint_consumers=endpoint_consumers,
            event_channels=event_channels,
            candidate_or_unlinked_event_channels=candidate_or_unlinked_event_channels,
            domains=domains,
            next_actions=next_actions,
            status="ambiguous",
            line=line,
        )

    base_category = _planning_context_base_category(anchors)
    if base_category == "services":
        services = _planning_context_filter_rows("services", services, anchors, line=line)[:limit]
    elif base_category == "symbols":
        symbols = _planning_context_filter_rows("symbols", symbols, anchors, line=line)[:limit]
    elif base_category == "dependencies":
        dependencies = _planning_context_filter_rows("dependencies", dependencies, anchors, line=line)[:limit]
    elif base_category == "endpoints":
        endpoints = _planning_context_filter_rows("endpoints", endpoints, anchors, line=line)[:limit]
    elif base_category == "event_channels":
        event_channels = _planning_context_filter_rows("event_channels", event_channels, anchors, line=line)[:limit]
        candidate_or_unlinked_event_channels = _planning_context_filter_rows(
            "event_channels", candidate_or_unlinked_event_channels, anchors, line=line
        )[:limit]
    elif base_category == "domains":
        domains = _planning_context_filter_rows("domains", domains, anchors, line=line)[:limit]

    status = (
        "found"
        if any((services, symbols, dependencies, endpoints, event_channels, candidate_or_unlinked_event_channels, domains))
        else "not_found"
    )
    if status == "not_found":
        next_actions.append(PLANNING_CONTEXT_NO_OVERLAP_ACTION)
    return _planning_context_output(
        kg=kg,
        query=query,
        anchors=anchors,
        services=services,
        symbols=symbols,
        dependencies=dependencies,
        endpoints=endpoints,
        endpoint_consumers=endpoint_consumers,
        event_channels=event_channels,
        candidate_or_unlinked_event_channels=candidate_or_unlinked_event_channels,
        domains=domains,
        next_actions=next_actions,
        status=status,
        line=line,
    )


def _review_context_repo_resolution(
    kg: KgSnapshot,
    *,
    requested_repo: str,
    changed_files: list[str],
) -> JsonObject:
    snapshot_repos = _review_context_snapshot_repos(kg)
    requested_repo_key = _normalize_repo_text(requested_repo)
    direct_matches = [repo for repo in snapshot_repos if repo == requested_repo_key]
    if direct_matches:
        effective_repo = direct_matches[0]
        return {
            "status": "matched",
            "requested_repo": requested_repo,
            "effective_repo": effective_repo,
            "basis": "direct_repo_match",
            "snapshot_repo_count": len(snapshot_repos),
            "matched_repos": direct_matches[:PLANNING_CONTEXT_SECTION_LIMIT],
        }
    if "/" not in requested_repo:
        return {
            "status": "unresolved",
            "requested_repo": requested_repo,
            "effective_repo": requested_repo,
            "basis": "unchanged",
            "reason": "requested_repo_not_owner_qualified",
            "snapshot_repo_count": len(snapshot_repos),
            "candidate_repos": snapshot_repos[:PLANNING_CONTEXT_SECTION_LIMIT],
        }
    if len(snapshot_repos) != 1:
        return {
            "status": "ambiguous" if snapshot_repos else "unresolved",
            "requested_repo": requested_repo,
            "effective_repo": requested_repo,
            "basis": "unchanged",
            "reason": "multiple_snapshot_repos" if snapshot_repos else "no_snapshot_repo_identity",
            "snapshot_repo_count": len(snapshot_repos),
            "candidate_repos": snapshot_repos[:PLANNING_CONTEXT_SECTION_LIMIT],
        }
    effective_repo = snapshot_repos[0]
    if not _review_context_changed_files_overlap_snapshot(kg, changed_files):
        return {
            "status": "unresolved",
            "requested_repo": requested_repo,
            "effective_repo": requested_repo,
            "basis": "unchanged",
            "reason": "no_changed_file_overlap",
            "snapshot_repo_count": len(snapshot_repos),
            "candidate_repos": snapshot_repos[:PLANNING_CONTEXT_SECTION_LIMIT],
        }
    return {
        "status": "resolved",
        "requested_repo": requested_repo,
        "effective_repo": effective_repo,
        "basis": "single_repo_snapshot_changed_file_overlap",
        "snapshot_repo_count": 1,
    }


def _review_context_snapshot_repos(kg: KgSnapshot) -> list[str]:
    repos: set[str] = set()
    manifest_repo = kg.manifest.get("repo_name") if isinstance(kg.manifest, dict) else None
    manifest_repo_key = _normalize_repo_text(manifest_repo)
    if manifest_repo_key:
        repos.add(manifest_repo_key)
    for entity in kg.entities:
        repo_key = _normalize_repo_text(_planning_context_entity_repo(entity))
        if repo_key:
            repos.add(repo_key)
    fact_repos: set[str] = set()
    for fact in kg.facts:
        qualifier = fact.get("qualifier")
        if not isinstance(qualifier, dict):
            continue
        consumer_repo_key = _normalize_repo_text(qualifier.get("consumer_repo"))
        if consumer_repo_key:
            fact_repos.add(consumer_repo_key)
    if not repos:
        repos.update(fact_repos)
    return sorted(repos)


def _review_context_changed_files_overlap_snapshot(kg: KgSnapshot, changed_files: list[str]) -> bool:
    normalized_changed_files = {
        normalized_path
        for path in changed_files
        if isinstance(path, str) and path.strip()
        for normalized_path in [_review_context_normalize_changed_file(path)]
        if normalized_path is not None
    }
    if not normalized_changed_files:
        return False
    snapshot_paths = _review_context_snapshot_paths(kg)
    if snapshot_paths.intersection(normalized_changed_files):
        return True
    repo_path = kg.manifest.get("repo_path") if isinstance(kg.manifest, dict) else None
    if not isinstance(repo_path, str) or not repo_path.strip():
        return False
    root = Path(repo_path).expanduser()
    try:
        resolved_root = root.resolve()
    except (OSError, RuntimeError):
        return False
    # Any changed-file overlap is enough only after the resolver has already proved
    # the snapshot contains exactly one repo identity; multi-repo snapshots fail closed.
    # This fallback intentionally depends on the serving host having the checkout at
    # manifest.repo_path, so indexed snapshot paths remain the deterministic first check.
    for changed_file in normalized_changed_files:
        try:
            candidate = (root / changed_file).resolve()
            if not candidate.is_relative_to(resolved_root):
                continue
            if candidate.exists():
                return True
        except (OSError, RuntimeError):
            continue
    return False


def _review_context_normalize_changed_file(path: str) -> str | None:
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if normalized.startswith("/"):
        return None
    parts = [part for part in normalized.split("/") if part and part != "."]
    if not parts or any(part == ".." for part in parts):
        return None
    return "/".join(parts)


def _review_context_snapshot_paths(kg: KgSnapshot) -> set[str]:
    paths: set[str] = set()
    for entity in kg.entities:
        properties = entity.get("properties")
        if not isinstance(properties, dict):
            continue
        path = properties.get("path")
        if isinstance(path, str) and path.strip():
            paths.add(_planning_context_normalize_path(path))
    for row in kg.evidence:
        for key in ("bytes_ref", "source_ref"):
            ref = row.get(key)
            if not isinstance(ref, dict):
                continue
            path = ref.get("path") or ref.get("file_path")
            if isinstance(path, str) and path.strip():
                paths.add(_planning_context_normalize_path(path))
    for row in kg.coverage:
        scope_ref = row.get("scope_ref")
        if not isinstance(scope_ref, dict):
            continue
        path = scope_ref.get("path") or scope_ref.get("file_path")
        if isinstance(path, str) and path.strip():
            paths.add(_planning_context_normalize_path(path))
    return paths


def _review_context(kg: KgSnapshot, arguments: JsonObject) -> JsonObject:
    requested_repo = _required_string(arguments, "repo")
    changed_files = _required_string_list(arguments, "changed_files")
    repo_resolution = _review_context_repo_resolution(kg, requested_repo=requested_repo, changed_files=changed_files)
    repo = str(repo_resolution.get("effective_repo") or requested_repo)
    changed_ranges = _optional_changed_ranges(arguments, "changed_ranges")
    limit = _limit(arguments)
    detail_limit = min(limit, REVIEW_CONTEXT_DETAIL_LIMIT)
    requested_surfaces, unknown_surfaces = _optional_review_surfaces_tolerant(arguments, "requested_surfaces")
    requested_review_sections = _optional_review_section_aliases(arguments, "requested_surfaces")
    include_deploy_blockers = _optional_bool(arguments, "include_deploy_blockers", default=False)
    include_unlinked_leads = _optional_bool(arguments, "include_unlinked_leads", default=False)

    changed_symbols: list[JsonObject] = []
    range_filters = _changed_ranges_by_path(changed_ranges)
    changed_symbols_by_path: dict[str, list[JsonObject]] = {}
    file_symbols_by_path: dict[str, list[JsonObject]] = {}
    diff_anchors: list[JsonObject] = []

    for changed_file in changed_files:
        symbol_rows = list(kg.symbols_in_file(changed_file, limit=10_000).get("symbols", []))
        symbol_rows = [row for row in symbol_rows if _review_context_repo_matches(row.get("repo"), repo)]
        normalized_changed_file = _planning_context_normalize_path(changed_file)
        file_symbols_by_path[normalized_changed_file] = symbol_rows
        file_ranges = range_filters.get(normalized_changed_file, [])
        if file_ranges:
            range_symbols: list[JsonObject] = []
            for range_start, range_end in file_ranges:
                anchor, anchor_symbols = _review_context_anchor_for_range(
                    repo=repo,
                    path=normalized_changed_file,
                    range_start=range_start,
                    range_end=range_end,
                    symbols=symbol_rows,
                )
                diff_anchors.append(anchor)
                range_symbols.extend(anchor_symbols)
            symbol_rows = _review_context_dedupe_rows(range_symbols)
        elif changed_ranges:
            diff_anchors.append(
                _review_context_file_anchor(
                    repo=repo,
                    path=normalized_changed_file,
                    match_kind="changed_file_without_range",
                    symbol_count=len(symbol_rows),
                )
            )
        else:
            diff_anchors.append(
                _review_context_file_anchor(
                    repo=repo,
                    path=normalized_changed_file,
                    match_kind="changed_file_inventory",
                    symbol_count=len(symbol_rows),
                )
            )
        for row in symbol_rows:
            changed_symbols.append(row)
        changed_symbols_by_path[normalized_changed_file] = symbol_rows

    changed_symbols = _review_context_dedupe_rows(changed_symbols)[:detail_limit]
    diff_anchors = _review_context_dedupe_rows(diff_anchors)[:detail_limit]
    changed_file_symbols = _review_context_dedupe_rows(
        [row for rows in file_symbols_by_path.values() for row in rows]
    )[:detail_limit]
    direct_callers: list[JsonObject] = []
    direct_callees: list[JsonObject] = []
    for row in changed_symbols:
        symbol_name = str(row.get("qualified_name") or row.get("qualname") or "")
        if not symbol_name:
            continue
        callers = kg.find_callers(
            symbol_name,
            limit=detail_limit,
            path=_optional_symbol_path(row),
            line=_optional_symbol_line(row),
            include_all=False,
        )
        callees = kg.find_callees(
            symbol_name,
            limit=detail_limit,
            path=_optional_symbol_path(row),
            line=_optional_symbol_line(row),
            include_all=False,
        )
        direct_callers.extend(callers.get("callers", []))
        direct_callees.extend(callees.get("callees", []))
    repo_dependency_result = kg.repo_dependencies(repo, limit=detail_limit)
    repo_dependencies = list(repo_dependency_result.get("dependencies", []))
    direct_callers = _review_context_dedupe_rows(direct_callers)[:detail_limit]
    direct_callees = _review_context_dedupe_rows(direct_callees)[:detail_limit]
    transitive_callers = _review_context_transitive_callers(
        kg, changed_symbols=changed_symbols, depth=3, limit=detail_limit
    )
    repo_dependencies = _review_context_dedupe_rows(repo_dependencies)[:detail_limit]
    runtime_surfaces = _review_context_runtime_surfaces(
        kg, repo=repo, changed_symbols=changed_symbols, limit=detail_limit
    )
    framework_impact = framework_impact_packet(
        kg,
        repo=repo,
        changed_symbols=changed_symbols,
        limit=detail_limit,
    )
    application_impact = application_impact_packet(
        kg,
        repo=repo,
        changed_files=changed_files,
        changed_symbols=changed_symbols,
        limit=detail_limit,
    )
    risk_signals = _review_context_risk_signals(
        kg,
        repo=repo,
        changed_symbols=changed_symbols,
        changed_files=changed_files,
        range_filters=range_filters,
        limit=12,
    )
    endpoint_rows = runtime_surfaces["endpoints"]
    endpoint_consumer_rows = runtime_surfaces["endpoint_consumers"]
    event_channel_rows = runtime_surfaces["event_channels"]
    candidate_event_channel_rows = runtime_surfaces["candidate_or_unlinked_event_channels"]
    deploy_mapping_rows = runtime_surfaces["deploy_mappings"]

    unsupported_scopes: list[JsonObject] = []
    if include_deploy_blockers:
        unsupported_scopes.append(
            {
                "kind": "deploy_blockers",
                "scope": repo,
                "reason": "No canonical deploy-blocker relation is implemented yet",
            }
        )
    if "ownership_context" in requested_review_sections:
        unsupported_scopes.append(
            {
                "kind": "ownership_context",
                "scope": repo,
                "reason": "review_context does not assemble ownership evidence; call planning_context with the repo or service and read ownership_context.answer_packet.",
            }
        )

    status = "found" if any((changed_symbols, direct_callers, direct_callees, repo_dependencies)) else "not_found"
    changed_surface = _review_context_changed_surface(
        changed_files=changed_files,
        range_filters=range_filters,
        symbols_by_path=changed_symbols_by_path,
        changed_symbols=changed_symbols,
    )
    source_coordinates = _planning_context_source_coordinates(
        _review_context_anchor_coordinate_rows(diff_anchors),
        changed_symbols,
        direct_callers,
        direct_callees,
        repo_dependencies,
        endpoint_rows,
        endpoint_consumer_rows,
        event_channel_rows + candidate_event_channel_rows,
        deploy_mapping_rows,
        limit=PLANNING_CONTEXT_SECTION_LIMIT,
    )
    surface_status = _review_context_surface_status(
        application_impact=application_impact,
        runtime_surfaces=runtime_surfaces,
        requested_surfaces=requested_surfaces,
        unknown_surfaces=unknown_surfaces,
        changed_symbols=changed_symbols,
    )
    answerability = _review_context_answerability(
        status=status,
        changed_symbols=changed_symbols,
        include_deploy_blockers=include_deploy_blockers,
        include_ownership_context="ownership_context" in requested_review_sections,
        surface_status=surface_status,
        requested_surfaces=requested_surfaces,
    )
    next_actions = _review_context_next_actions(answerability, unsupported_scopes=unsupported_scopes)
    public_changed_symbols = _review_context_public_symbol_rows(changed_symbols)
    public_changed_file_symbols = _review_context_public_symbol_rows(changed_file_symbols)
    public_direct_callers = _planning_context_public_rows(direct_callers)
    public_direct_callees = _planning_context_public_rows(direct_callees)
    public_transitive_callers = _planning_context_public_rows(transitive_callers)
    public_repo_dependencies = _planning_context_public_rows(repo_dependencies)
    public_endpoints = _planning_context_public_rows(endpoint_rows)
    public_endpoint_consumers = _planning_context_public_rows(endpoint_consumer_rows)
    public_event_channels = _planning_context_public_rows(event_channel_rows)
    public_candidate_event_channels = _planning_context_public_rows(candidate_event_channel_rows)
    public_deploy_mappings = _planning_context_public_rows(deploy_mapping_rows)
    # Without changed_ranges we only know the changed-file symbol inventory, not which
    # symbols actually changed. Mirror the answer packet: top-level changed_symbols is
    # empty in that case and the inventory is exposed via changed_file_symbols, so the
    # list is never mistaken for proof that every file symbol was touched.
    changed_symbols_in_scope = public_changed_symbols if changed_ranges else []
    # Caller/callee/transitive edges are "of changed_symbols" per scope_contract, so when no
    # ranges are supplied (changed_symbols_in_scope empty) they are in-scope-empty too. This
    # keeps the summary counts, top-level fields, and the answer packet consistent instead of
    # reporting 0 changed symbols alongside non-zero caller counts.
    direct_callers_in_scope = public_direct_callers if changed_ranges else []
    direct_callees_in_scope = public_direct_callees if changed_ranges else []
    transitive_callers_in_scope = public_transitive_callers if changed_ranges else []
    # Stamp lead_id / lead_kind on the in-scope lists before the lead packet and before
    # building the result dict. The same stamped rows are used everywhere — top-level fields,
    # review_leads, and hypotheses — so IDs survive budget compaction without re-derivation.
    _stamped_in_scope = add_review_lead_ids(
        {
            "changed_symbols": changed_symbols_in_scope,
            "direct_callers": direct_callers_in_scope,
            "direct_callees": direct_callees_in_scope,
            "transitive_callers": transitive_callers_in_scope,
            "source_coordinates": source_coordinates,
        }
    )
    changed_symbols_in_scope = _stamped_in_scope["changed_symbols"]
    direct_callers_in_scope = _stamped_in_scope["direct_callers"]
    direct_callees_in_scope = _stamped_in_scope["direct_callees"]
    transitive_callers_in_scope = _stamped_in_scope["transitive_callers"]
    source_coordinates = _stamped_in_scope["source_coordinates"]
    summary = _review_context_summary(
        changed_files=changed_files,
        changed_symbols=changed_symbols_in_scope,
        direct_callers=direct_callers_in_scope,
        direct_callees=direct_callees_in_scope,
        repo_dependencies=public_repo_dependencies,
        endpoints=public_endpoints,
        endpoint_consumers=public_endpoint_consumers,
        event_channels=public_event_channels,
        candidate_or_unlinked_event_channels=public_candidate_event_channels,
        deploy_mappings=public_deploy_mappings,
        source_coordinates=source_coordinates,
        changed_file_symbols=public_changed_file_symbols,
        diff_anchors=diff_anchors,
        transitive_callers=transitive_callers_in_scope,
        framework_impact=framework_impact,
        application_impact=application_impact,
    )
    summary["requested_limit"] = limit
    summary["detail_limit"] = detail_limit
    scope_contract = _review_context_scope_contract(
        changed_ranges=changed_ranges,
        changed_symbols=changed_symbols_in_scope,
        changed_file_symbols=public_changed_file_symbols,
    )
    claim_contract = _review_context_claim_contract()
    review_packet_changed_symbols = changed_symbols_in_scope
    review_packet_direct_callers = direct_callers_in_scope
    review_packet_direct_callees = direct_callees_in_scope
    review_packet_transitive_callers = transitive_callers_in_scope
    review_answer_packet = _review_context_answer_packet(
        status=status,
        summary=summary,
        scope_contract=scope_contract,
        claim_contract=claim_contract,
        changed_symbols=review_packet_changed_symbols,
        changed_file_symbols=public_changed_file_symbols,
        diff_anchors=diff_anchors,
        direct_callers=review_packet_direct_callers,
        direct_callees=review_packet_direct_callees,
        transitive_callers=review_packet_transitive_callers,
        framework_impact=framework_impact,
        application_impact=application_impact,
        runtime_surfaces={
            "endpoints": public_endpoints,
            "endpoint_consumers": public_endpoint_consumers,
            "event_channels": public_event_channels,
            "candidate_or_unlinked_event_channels": public_candidate_event_channels,
            "deploy_mappings": public_deploy_mappings,
        },
        surface_status=surface_status,
        answerability=answerability,
    )
    review_lead_packet = _review_context_lead_packet(
        changed_files=changed_files,
        summary=summary,
        changed_symbols=changed_symbols_in_scope,
        direct_callers=direct_callers_in_scope,
        direct_callees=direct_callees_in_scope,
        transitive_callers=transitive_callers_in_scope,
        source_coordinates=source_coordinates,
    )
    review_answer_packet["repo_resolution"] = repo_resolution
    review_answer_packet["review_lead_status"] = review_lead_packet["review_lead_status"]
    review_hypotheses = review_hypotheses_for_context(
        changed_files=changed_files,
        changed_symbols=changed_symbols_in_scope,
        direct_callers=direct_callers_in_scope,
        direct_callees=direct_callees_in_scope,
        transitive_callers=transitive_callers_in_scope,
        framework_impact=framework_impact,
        application_impact=application_impact,
        runtime_surfaces=runtime_surfaces,
        review_leads=review_lead_packet["review_leads"],
        review_lead_status=review_lead_packet["review_lead_status"],
        risk_signals=risk_signals,
    )
    review_answer_packet["top_review_hypotheses"] = review_hypotheses[:PLANNING_CONTEXT_SECTION_LIMIT]
    result = {
        "status": status,
        "repo": repo,
        "requested_repo": requested_repo,
        "repo_resolution": repo_resolution,
        "summary": summary,
        "review_answer_packet": review_answer_packet,
        "review_lead_status": review_lead_packet["review_lead_status"],
        "review_leads": review_lead_packet["review_leads"],
        "diff_anchors": diff_anchors,
        "changed_symbols": changed_symbols_in_scope,
        "changed_file_symbols": public_changed_file_symbols,
        "direct_callers": direct_callers_in_scope,
        "direct_callees": direct_callees_in_scope,
        "direct_callers_of_changed_symbols": direct_callers_in_scope,
        "direct_callees_from_changed_symbols": direct_callees_in_scope,
        "transitive_callers": transitive_callers_in_scope,
        "repo_dependencies": public_repo_dependencies,
        "changed_surface": changed_surface,
        "scope_contract": scope_contract,
        "claim_contract": claim_contract,
        "impact": {
            "direct_callers": direct_callers_in_scope[:PLANNING_CONTEXT_SECTION_LIMIT],
            "direct_callees": direct_callees_in_scope[:PLANNING_CONTEXT_SECTION_LIMIT],
            "transitive_callers": transitive_callers_in_scope[:PLANNING_CONTEXT_SECTION_LIMIT],
            "repo_dependencies": public_repo_dependencies[:PLANNING_CONTEXT_SECTION_LIMIT],
        },
        "runtime_surfaces": {
            "endpoints": public_endpoints[:PLANNING_CONTEXT_SECTION_LIMIT],
            "endpoint_consumers": public_endpoint_consumers[:PLANNING_CONTEXT_SECTION_LIMIT],
            "event_channels": public_event_channels[:PLANNING_CONTEXT_SECTION_LIMIT],
            "candidate_or_unlinked_event_channels": public_candidate_event_channels[
                :PLANNING_CONTEXT_SECTION_LIMIT
            ],
            "deploy_mappings": public_deploy_mappings[:PLANNING_CONTEXT_SECTION_LIMIT],
        },
        "framework_impact": framework_impact,
        "application_impact": application_impact,
        "surface_status": surface_status,
        "source_coordinates": source_coordinates,
        "answerability": answerability,
        "coverage_warnings": [],
        "unsupported_scopes": unsupported_scopes,
        "unsupported_review_scopes": unsupported_scopes,
        "evidence": _planning_context_evidence(
            changed_symbols,
            direct_callers,
            direct_callees,
            repo_dependencies,
            endpoint_rows,
            endpoint_consumer_rows,
            event_channel_rows,
            candidate_event_channel_rows,
            deploy_mapping_rows,
            framework_impact.get("model_fields", []),
            framework_impact.get("model_relations", []),
            framework_impact.get("serializers", []),
            framework_impact.get("views", []),
            framework_impact.get("tasks", []),
            application_impact.get("runtime_facts", []),
        ),
        "review_hypotheses": review_hypotheses,
        "next_actions": next_actions,
    }
    if _review_context_should_compact_unanchored(
        changed_ranges=changed_ranges,
        include_unlinked_leads=include_unlinked_leads,
        include_deploy_blockers=include_deploy_blockers,
        requested_surfaces=requested_surfaces,
        requested_review_sections=requested_review_sections,
        summary=summary,
        direct_callers=direct_callers_in_scope,
        direct_callees=direct_callees_in_scope,
        transitive_callers=transitive_callers_in_scope,
    ):
        return _review_context_compact_unanchored_result(result)
    return result


def _review_context_lead_packet(
    *,
    changed_files: list[str],
    summary: JsonObject,
    changed_symbols: list[JsonObject],
    direct_callers: list[JsonObject],
    direct_callees: list[JsonObject],
    transitive_callers: list[JsonObject],
    source_coordinates: list[JsonObject],
) -> JsonObject:
    changed_anchor_count = int(summary.get("symbol_anchor_count") or 0)
    direct_impact_count = len(direct_callers) + len(direct_callees)
    transitive_impact_count = len(transitive_callers)
    useful = bool(changed_anchor_count or changed_symbols or direct_impact_count or transitive_impact_count)
    status: JsonObject = {
        "coverage_status": "useful" if useful else "low_coverage",
        "recommended_action": "use_supercontext_packet" if useful else "fall_back_to_plain_review",
        "changed_anchor_count": changed_anchor_count,
        "changed_symbol_count": len(changed_symbols),
        "direct_impact_count": direct_impact_count,
        "transitive_impact_count": transitive_impact_count,
        "source_coordinate_count": len(source_coordinates),
        "file_anchor_count": int(summary.get("file_anchor_count") or 0),
    }
    if not useful:
        status["reason"] = "no symbol anchors, changed symbols, or direct/transitive impact edges"
    available = review_available_counts(
        changed_symbols=changed_symbols,
        direct_callers=direct_callers,
        direct_callees=direct_callees,
        transitive_callers=transitive_callers,
        source_coordinates=source_coordinates,
    )
    leads_pre: JsonObject = {
        "changed_files": changed_files[:PLANNING_CONTEXT_SECTION_LIMIT],
        "changed_symbols": changed_symbols[:PLANNING_CONTEXT_SECTION_LIMIT],
        "direct_callers": direct_callers[:PLANNING_CONTEXT_SECTION_LIMIT],
        "direct_callees": direct_callees[:PLANNING_CONTEXT_SECTION_LIMIT],
        "transitive_callers": transitive_callers[:PLANNING_CONTEXT_SECTION_LIMIT],
        "source_coordinates": source_coordinates[:PLANNING_CONTEXT_SECTION_LIMIT],
    }
    leads = add_review_lead_ids(leads_pre)
    status["available"] = available
    status["returned"] = review_lead_counts(leads)
    return {"review_lead_status": status, "review_leads": leads}


def _review_context_should_compact_unanchored(
    *,
    changed_ranges: list[JsonObject],
    include_unlinked_leads: bool,
    include_deploy_blockers: bool,
    requested_surfaces: list[str],
    requested_review_sections: list[str],
    summary: JsonObject,
    direct_callers: list[JsonObject],
    direct_callees: list[JsonObject],
    transitive_callers: list[JsonObject],
) -> bool:
    if (
        not changed_ranges
        or include_unlinked_leads
        or include_deploy_blockers
        or requested_surfaces
        or requested_review_sections
    ):
        return False
    return (
        summary.get("changed_symbol_count") == 0
        and summary.get("symbol_anchor_count") == 0
        and not direct_callers
        and not direct_callees
        and not transitive_callers
    )


def _review_context_compact_unanchored_result(result: JsonObject) -> JsonObject:
    summary = dict(result.get("summary", {}))
    summary["packet_mode"] = "diff_anchor_only"
    diff_anchors = [row for row in result.get("diff_anchors", []) if isinstance(row, dict)]
    repo_dependencies = [
        row for row in result.get("repo_dependencies", []) if isinstance(row, dict)
    ][:PLANNING_CONTEXT_SECTION_LIMIT]
    source_coordinates = _review_context_diff_anchor_source_coordinates(diff_anchors)
    summary["source_coordinate_count"] = len(source_coordinates)
    answerability = result.get("answerability") if isinstance(result.get("answerability"), dict) else {}
    packet = result.get("review_answer_packet") if isinstance(result.get("review_answer_packet"), dict) else {}
    existing_review_leads = result.get("review_leads") if isinstance(result.get("review_leads"), dict) else {}
    changed_files = [
        path for path in existing_review_leads.get("changed_files", []) if isinstance(path, str) and path.strip()
    ]
    review_lead_packet = _review_context_lead_packet(
        changed_files=changed_files,
        summary=summary,
        changed_symbols=[],
        direct_callers=[],
        direct_callees=[],
        transitive_callers=[],
        source_coordinates=source_coordinates,
    )
    review_lead_status = review_lead_packet["review_lead_status"]
    review_leads = review_lead_packet["review_leads"]
    stamped_source_coordinates = review_leads.get("source_coordinates") if isinstance(review_leads.get("source_coordinates"), list) else source_coordinates
    omitted_counts = _review_context_omitted_context_counts(result)
    packet_summary = dict(packet.get("summary", {})) if isinstance(packet.get("summary"), dict) else {}
    packet_summary["packet_mode"] = "diff_anchor_only"
    packet_summary["omitted_context_counts"] = omitted_counts
    compact_packet: JsonObject = {
        "status": result.get("status"),
        "packet_mode": "diff_anchor_only",
        "repo_resolution": result.get("repo_resolution", {}),
        "review_lead_status": review_lead_status,
        "answerability": answerability,
        "summary": packet_summary,
        "top_diff_anchors": diff_anchors[:PLANNING_CONTEXT_SECTION_LIMIT],
        "top_changed_symbols": [],
        "top_direct_callers": [],
        "top_direct_callees": [],
        "top_transitive_callers": [],
    }
    next_actions = [
        *[str(action) for action in result.get("next_actions", []) if str(action).strip()],
        (
            "Inspect diff_anchors/source_coordinates directly; pass include_unlinked_leads=true only when broad "
            "unlinked namespace/name leads are worth the extra context."
        ),
    ]
    changed_surface = result.get("changed_surface") if isinstance(result.get("changed_surface"), dict) else {}
    compact_changed_surface = {
        "files": changed_surface.get("files", []) if isinstance(changed_surface.get("files"), list) else [],
        "symbols": [],
    }
    compact_result: JsonObject = {
        "status": result.get("status"),
        "repo": result.get("repo"),
        "requested_repo": result.get("requested_repo"),
        "repo_resolution": result.get("repo_resolution", {}),
        "summary": summary,
        "review_answer_packet": compact_packet,
        "review_lead_status": review_lead_status,
        "review_leads": review_leads,
        "diff_anchors": diff_anchors,
        "changed_symbols": [],
        "changed_file_symbols": [],
        "direct_callers": [],
        "direct_callees": [],
        "direct_callers_of_changed_symbols": [],
        "direct_callees_from_changed_symbols": [],
        "transitive_callers": [],
        "changed_surface": compact_changed_surface,
        "impact": {
            "direct_callers": [],
            "direct_callees": [],
            "transitive_callers": [],
            "repo_dependencies": repo_dependencies,
        },
        "repo_dependencies": repo_dependencies,
        "source_coordinates": stamped_source_coordinates,
        "review_hypotheses": [
            h for h in (result.get("review_hypotheses") or [])
            if isinstance(h, dict) and h.get("risk_type") == "low_coverage_stylesheet_gap"
        ],
        "answerability": answerability,
        "coverage_warnings": result.get("coverage_warnings", []),
        "unsupported_scopes": result.get("unsupported_scopes", []),
        "unsupported_review_scopes": result.get("unsupported_review_scopes", []),
        "omitted_context": {
            "reason": "No changed symbols or direct impact edges were found for the supplied changed ranges.",
            "counts": omitted_counts,
            "opt_in_argument": {"include_unlinked_leads": True},
        },
        "packet_contract": {
            "tool": "review_context",
            "positioning": "Compact diff-anchor-only packet; inspect changed ranges directly before making review claims.",
            "common_fields": {
                "diff_anchors": "Changed file/range coordinates.",
                "omitted_context": "Counts of broad context omitted by default.",
                "answerability": "Remaining source checks.",
            },
            "claim_rule": "Omitted counts and unlinked leads are not proven impact.",
        },
        "next_actions": _dedupe_strings(next_actions),
    }
    return compact_result


COMPACT_REVIEW_SOURCE_COORDINATE_LIMIT = PLANNING_CONTEXT_SECTION_LIMIT


def _review_context_omitted_context_counts(result: JsonObject) -> JsonObject:
    counts: JsonObject = {}
    for field in ("surface_status", "evidence", "scope_contract", "claim_contract"):
        value = result.get(field)
        count = 1 if field in {"scope_contract", "claim_contract"} and isinstance(value, dict) and value else (
            _review_context_omitted_value_count(value)
        )
        if count:
            counts[field] = count
    runtime_surfaces = result.get("runtime_surfaces")
    if isinstance(runtime_surfaces, dict):
        candidate_events = runtime_surfaces.get("candidate_or_unlinked_event_channels")
        count = _review_context_omitted_value_count(candidate_events)
        if count:
            counts["runtime_surfaces.candidate_or_unlinked_event_channels"] = count
    application_impact = result.get("application_impact")
    if isinstance(application_impact, dict):
        cross_repo_name_leads = application_impact.get("cross_repo_name_leads")
        count = _review_context_omitted_value_count(cross_repo_name_leads)
        if count:
            counts["application_impact.cross_repo_name_leads"] = count
    return counts


def _review_context_omitted_value_count(value: object) -> int:
    if isinstance(value, list):
        return len([row for row in value if row])
    return 0


def _review_context_diff_anchor_source_coordinates(diff_anchors: list[JsonObject]) -> list[JsonObject]:
    coordinates: list[JsonObject] = []
    seen: set[tuple[object, object, object, object]] = set()
    for anchor in diff_anchors:
        values = anchor.get("source_coordinates")
        if not isinstance(values, list):
            continue
        for value in values:
            if not isinstance(value, dict):
                continue
            path = value.get("path")
            line_start = value.get("line_start")
            line_end = value.get("line_end")
            key = (value.get("repo"), path, line_start, line_end)
            if key in seen:
                continue
            seen.add(key)
            coordinates.append(
                {
                    "repo": value.get("repo"),
                    "path": path,
                    "line_start": line_start,
                    "line_end": line_end,
                    "provenance": value.get("provenance") or "changed_range",
                }
            )
            if len(coordinates) >= COMPACT_REVIEW_SOURCE_COORDINATE_LIMIT:
                return coordinates
    return coordinates


_RISK_SIGNAL_PREDICATE = "code_risk_signal"


def _cap_risk_signals_per_subject(
    signals: list[JsonObject],
    *,
    range_filters: dict[str, list[tuple[int, int]]],
    limit_per_subject: int,
) -> list[JsonObject]:
    """Keep at most limit_per_subject signals per enclosing subject_id.

    Relevance order within each subject:
      1. Evidence overlaps a changed range (overlap first).
      2. Lowest evidence line (ascending).
    """
    def _signal_sort_key(sig: JsonObject) -> tuple[int, int]:
        overlaps_range = 0
        min_line = 999_999
        for ev in (sig.get("_evidence") or []):
            br = ev.get("bytes_ref") if isinstance(ev, dict) else None
            if not isinstance(br, dict):
                continue
            ev_line_start = br.get("line_start")
            ev_line_end = br.get("line_end")
            if isinstance(ev_line_start, int) and isinstance(ev_line_end, int):
                min_line = min(min_line, ev_line_start)
                p = br.get("path")
                if isinstance(p, str):
                    p_norm = _planning_context_normalize_path(p)
                    path_ranges = range_filters.get(p_norm, [])
                    if path_ranges and any(
                        ev_line_start <= rng_end and rng_start <= ev_line_end
                        for rng_start, rng_end in path_ranges
                    ):
                        overlaps_range = 1
        return (1 - overlaps_range, min_line)

    by_subject: dict[str, list[JsonObject]] = {}
    for sig in signals:
        subj = sig.get("subject_id") or ""
        by_subject.setdefault(subj, []).append(sig)

    result: list[JsonObject] = []
    for subj_sigs in by_subject.values():
        subj_sigs.sort(key=_signal_sort_key)
        result.extend(subj_sigs[:limit_per_subject])
    return result


def _review_context_risk_signals(
    kg: KgSnapshot,
    *,
    repo: str,
    changed_symbols: list[JsonObject],
    changed_files: list[str],
    range_filters: dict[str, list[tuple[int, int]]],
    limit: int = 12,
) -> list[JsonObject]:
    """Retrieve code_risk_signal support facts for changed symbols and files.

    Returns up to *limit* signal rows (default 12), each augmented with a
    ``_evidence`` list containing the evidence rows that carry ``bytes_ref``
    coordinates (so the hypothesis builder can extract paths without a separate
    KG lookup).

    Two bounds are applied in sequence:
      1. Per-subject cap (3): at most 3 signals per subject_id, ordered by
         range-overlap first then lowest evidence line.
      2. Total retrieval bound (*limit*, default 12): the post-cap list is
         sorted deterministically — range-overlap signals first, then
         subject-matched signals, then by (evidence path, evidence line) —
         and truncated to *limit*.  With 6 subjects × 3 = 18 possible rows
         after step 1, this bound is the enforced global ceiling.

    Scoping rules:
      1. subject_id directly matches the symbol_id of a changed-symbol row
         (symbol_id = entity["entity_id"] from _symbol_result; matched_by_subject)
         → included regardless of range filters.
      2. evidence bytes_ref.repo must equal *repo* (case-insensitive suffix
         match, same logic as _review_context_repo_matches).
      3. evidence bytes_ref.path must match a normalized changed file path.
      4. When range_filters contains ranges for that path, at least one
         evidence line (line_start..line_end) must overlap a supplied range.
         If range_filters is empty or has no entry for this path, the path
         match alone is sufficient (no range filter applied).
    """
    # Index changed context.
    # _symbol_result rows carry symbol_id (= entity["entity_id"]); not entity_id.
    changed_entity_ids: set[str] = set()
    for sym in changed_symbols:
        eid = sym.get("symbol_id")
        if isinstance(eid, str) and eid:
            changed_entity_ids.add(eid)

    normalized_changed_files: set[str] = set()
    for f in changed_files:
        normalized = _planning_context_normalize_path(f)
        if normalized:
            normalized_changed_files.add(normalized)

    results: list[JsonObject] = []
    seen_fact_ids: set[str] = set()

    for fact in kg.support_facts:
        if fact.get("predicate") != _RISK_SIGNAL_PREDICATE:
            continue
        fact_id = fact.get("fact_id")
        if isinstance(fact_id, str) and fact_id in seen_fact_ids:
            continue

        subject_id = fact.get("subject_id")
        matched_by_subject = isinstance(subject_id, str) and subject_id in changed_entity_ids

        # Attach evidence rows for coordinate extraction.
        evidence_rows: list[JsonObject] = []
        if isinstance(fact_id, str):
            for ev in kg.evidence_by_target.get(fact_id, []):
                evidence_rows.append(ev)

        matched = matched_by_subject
        if not matched:
            # Try evidence path matching, scoped to the effective repo and
            # changed ranges when supplied.
            for ev in evidence_rows:
                br = ev.get("bytes_ref")
                if not isinstance(br, dict):
                    continue
                # Repo scope: evidence must belong to the effective repo.
                ev_repo = br.get("repo")
                if not isinstance(ev_repo, str):
                    continue
                if not _review_context_repo_matches(ev_repo, repo):
                    continue
                p = br.get("path")
                if not isinstance(p, str):
                    continue
                p_norm = _planning_context_normalize_path(p)
                if p_norm not in normalized_changed_files:
                    continue
                # Range scope: when ranges are supplied for this path, the
                # evidence line interval must overlap at least one range.
                path_ranges = range_filters.get(p_norm, [])
                if path_ranges:
                    line_start = br.get("line_start")
                    line_end = br.get("line_end")
                    if not (isinstance(line_start, int) and isinstance(line_end, int)):
                        continue
                    overlaps = any(
                        line_start <= rng_end and rng_start <= line_end
                        for rng_start, rng_end in path_ranges
                    )
                    if not overlaps:
                        continue
                matched = True
                break

        if not matched:
            continue

        if isinstance(fact_id, str):
            seen_fact_ids.add(fact_id)

        enriched: JsonObject = dict(fact)
        enriched["_evidence"] = evidence_rows
        results.append(enriched)

    # Per-subject retrieval cap: keep at most 3 signals per enclosing subject,
    # selected by relevance: changed-range overlap first, then lowest evidence line.
    results = _cap_risk_signals_per_subject(results, range_filters=range_filters, limit_per_subject=3)

    # Total retrieval bound: apply deterministic order so the strongest *limit*
    # rows survive.  Priority: range-overlap signals first, then subject-matched
    # signals, then by (evidence path, evidence line) for stable ordering.
    def _total_sort_key(sig: JsonObject) -> tuple[int, int, str, int]:
        overlaps_range = 0
        matched_by_subj = 1
        min_path = ""
        min_line = 999_999
        sid = sig.get("subject_id")
        if isinstance(sid, str) and sid in changed_entity_ids:
            matched_by_subj = 0
        for ev in (sig.get("_evidence") or []):
            br = ev.get("bytes_ref") if isinstance(ev, dict) else None
            if not isinstance(br, dict):
                continue
            ev_ls = br.get("line_start")
            ev_le = br.get("line_end")
            ev_path = br.get("path") or ""
            if isinstance(ev_ls, int):
                if not min_path or ev_path < min_path or (ev_path == min_path and ev_ls < min_line):
                    min_path = ev_path
                    min_line = ev_ls
            if isinstance(ev_ls, int) and isinstance(ev_le, int):
                p_norm = _planning_context_normalize_path(ev_path)
                path_ranges = range_filters.get(p_norm, [])
                if path_ranges and any(
                    ev_ls <= rng_end and rng_start <= ev_le
                    for rng_start, rng_end in path_ranges
                ):
                    overlaps_range = 1
        return (1 - overlaps_range, matched_by_subj, min_path, min_line)

    results.sort(key=_total_sort_key)
    return results[:limit]


def _planning_context_from_query(kg: KgSnapshot, *, query: str, limit: int) -> JsonObject:
    service_matches = _matching_services(kg, query)
    service_rows = [_service_row(kg, service) for service in service_matches[:limit]]
    symbol_result = kg.lookup_symbol(query, limit=limit)
    repo_result = kg.repo_dependencies(query, limit=limit)
    package_rows = list(kg.modules_importing(query, limit=limit))
    package_uniqueness_probe = list(kg.modules_importing(query, limit=2))
    endpoint_result = kg.endpoints(path_query=query, limit=limit)
    event_result = kg.event_channels(channel_query=query, limit=limit)
    domain_result = kg.domain_references(query, limit=limit)

    resolved_symbol = symbol_result.get("resolved_symbol")
    symbol_rows = (
        [resolved_symbol]
        if symbol_result["status"] == "resolved" and isinstance(resolved_symbol, dict)
        else list(symbol_result.get("candidates", []))[:limit]
    )
    repo_rows = list(repo_result.get("dependencies", []))[:limit]
    endpoint_rows = list(endpoint_result.get("endpoints", []))[:limit]
    event_rows = list(event_result.get("event_channels", []))[:limit]
    candidate_event_rows = list(event_result.get("candidate_or_unlinked", []))[:limit]
    domain_rows = list(domain_result.get("references", []))[:limit]
    exact_endpoint_rows = _planning_context_exact_endpoint_rows(kg, query, limit)
    exact_event_rows = _planning_context_exact_event_rows(kg, query, limit)
    exact_domain_rows = _planning_context_exact_domain_rows(kg, query, limit)

    known_event_count = int(event_result.get("known_linked_count", 0))
    event_evidence_count = int(event_result.get("event_fact_count", 0))
    resolver_hits = {
        "service": bool(service_matches),
        "symbol": symbol_result["status"] in {"resolved", "ambiguous"},
        "repo": int(repo_result.get("dependency_count", 0)) > 0,
        "package": bool(package_rows),
        "endpoint": int(endpoint_result.get("endpoint_fact_count", 0)) > 0,
        "event_channel": event_evidence_count > 0,
        "domain": int(domain_result.get("reference_count", 0)) > 0,
    }
    unique_matches: dict[str, list[JsonObject]] = {}
    if len(service_matches) == 1 and _service_exact_match(query, service_matches[0]):
        unique_matches["service"] = service_rows
    if symbol_result["status"] == "resolved" and str(symbol_result.get("confidence", "")).startswith("exact_") and symbol_rows:
        unique_matches["symbol"] = symbol_rows[:1]
    if int(repo_result.get("dependency_count", 0)) == 1 and len(repo_rows) == 1:
        unique_matches["repo"] = repo_rows
    if len(package_uniqueness_probe) == 1 and len(package_rows) == 1:
        unique_matches["package"] = package_rows
    if len(exact_endpoint_rows) == 1 and int(endpoint_result.get("endpoint_fact_count", 0)) == 1:
        unique_matches["endpoint"] = exact_endpoint_rows
    if len(exact_event_rows) == 1 and known_event_count == 1:
        unique_matches["event_channel"] = exact_event_rows
    if len(exact_domain_rows) == 1 and int(domain_result.get("reference_count", 0)) == 1:
        unique_matches["domain"] = exact_domain_rows

    query_anchors = {
        "repo": None,
        "path": None,
        "symbol": None,
        "service": None,
        "package": None,
        "endpoint": None,
        "event_channel": None,
        "domain": None,
    }
    hit_resolvers = [kind for kind, hit in resolver_hits.items() if hit]
    if len(hit_resolvers) == 1 and len(unique_matches) == 1:
        kind, rows = next(iter(unique_matches.items()))
        query_anchors[kind] = query
        dependencies: list[JsonObject] = rows if kind in {"repo", "package"} else []
        endpoints: list[JsonObject] = rows if kind == "endpoint" else []
        event_channels: list[JsonObject] = rows if kind == "event_channel" else []
        candidate_or_unlinked_event_channels: list[JsonObject] = []
        domains: list[JsonObject] = rows if kind == "domain" else []
        endpoint_consumers: list[JsonObject] = []
        if kind == "service" and len(service_matches) == 1:
            related = _planning_context_service_related_rows(kg, service_matches[0], limit=limit)
            dependencies = _planning_context_dedupe_rows(dependencies + related["dependencies"])
            endpoints = _planning_context_dedupe_rows(endpoints + related["endpoints"])
            endpoint_consumers = _planning_context_dedupe_rows(endpoint_consumers + related["endpoint_consumers"])
            event_channels = _planning_context_dedupe_rows(event_channels + related["event_channels"])
            candidate_or_unlinked_event_channels = _planning_context_dedupe_rows(
                candidate_or_unlinked_event_channels + related["candidate_or_unlinked_event_channels"]
            )
            domains = _planning_context_dedupe_rows(domains + related["domains"])
        return _planning_context_output(
            kg=kg,
            query=query,
            anchors=query_anchors,
            services=service_rows if kind == "service" else [],
            symbols=rows if kind == "symbol" else [],
            dependencies=dependencies,
            endpoints=endpoints,
            endpoint_consumers=endpoint_consumers,
            event_channels=event_channels,
            candidate_or_unlinked_event_channels=candidate_or_unlinked_event_channels,
            domains=domains,
            next_actions=[],
            status="found",
        )

    dependencies = (repo_rows + package_rows)[:limit]
    next_actions = [
        "Use a structured anchor instead of `query`: `symbol`, `service`, `repo`, `package`, `endpoint`, `event_channel`, or `domain`.",
    ]
    if resolver_hits["symbol"]:
        next_actions.extend(_symbol_refinement_actions(symbol_rows[:limit]))
    if resolver_hits["service"]:
        next_actions.extend(_service_refinement_actions(service_rows))
    if resolver_hits["repo"]:
        next_actions.append(f"Use `repo={query}` to inspect cross-repo dependency links for that consumer repo.")
    if resolver_hits["package"]:
        next_actions.append(f"Use `package={query}` to inspect importer modules for that package.")
    if resolver_hits["endpoint"]:
        next_actions.append(f"Use `endpoint={query}` to inspect matching endpoint facts.")
    if resolver_hits["event_channel"]:
        next_actions.append(f"Use `event_channel={query}` to inspect matching event-channel facts.")
    if resolver_hits["domain"]:
        next_actions.append(f"Use `domain={query}` to inspect matching domain references.")

    return _planning_context_output(
        kg=kg,
        query=query,
        anchors=query_anchors,
        services=service_rows,
        symbols=symbol_rows[:limit],
        dependencies=dependencies,
        endpoints=endpoint_rows,
        endpoint_consumers=[],
        event_channels=event_rows,
        candidate_or_unlinked_event_channels=candidate_event_rows,
        domains=domain_rows,
        next_actions=next_actions,
        status="ambiguous",
    )


def _planning_context_base_category(anchors: dict[str, str | None]) -> str | None:
    if anchors.get("service"):
        return "services"
    if anchors.get("symbol") or anchors.get("path"):
        return "symbols"
    if anchors.get("endpoint"):
        return "endpoints"
    if anchors.get("event_channel"):
        return "event_channels"
    if anchors.get("domain"):
        return "domains"
    if anchors.get("package") or anchors.get("repo"):
        return "dependencies"
    return None


def _planning_context_filter_rows(
    category: str,
    rows: list[JsonObject],
    anchors: dict[str, str | None],
    *,
    line: int | None,
) -> list[JsonObject]:
    category_anchors = _planning_context_category_anchors(category, anchors)
    if category == "services":
        return [row for row in rows if _planning_context_service_row_matches(row, category_anchors)]
    if category == "symbols":
        return [row for row in rows if _planning_context_symbol_row_matches(row, category_anchors, line=line)]
    return [row for row in rows if _planning_context_fact_row_matches(row, category_anchors, line=line)]


def _planning_context_category_anchors(category: str, anchors: dict[str, str | None]) -> dict[str, str | None]:
    relevant = {
        "services": {"service", "repo"},
        "symbols": {"symbol", "path", "repo"},
        "dependencies": {"repo", "package", "service", "path"},
        "endpoints": {"repo", "service", "endpoint", "path"},
        "event_channels": {"repo", "service", "event_channel", "path"},
        "domains": {"repo", "service", "domain", "path"},
    }.get(category, set())
    return {key: value if key in relevant else None for key, value in anchors.items()}


def _planning_context_service_row_matches(row: JsonObject, anchors: dict[str, str | None]) -> bool:
    service = anchors.get("service")
    if service and service.strip().lower() not in _planning_context_service_row_search_text(row).lower():
        return False
    repo = anchors.get("repo")
    if repo and not _repo_text_matches(row.get("repo"), repo):
        return False
    return True


def _planning_context_symbol_row_matches(
    row: JsonObject,
    anchors: dict[str, str | None],
    *,
    line: int | None,
) -> bool:
    symbol = anchors.get("symbol")
    if symbol and not _planning_context_value_matches(
        symbol,
        row.get("qualified_name"),
        row.get("qualname"),
        row.get("display_name"),
    ):
        return False
    repo = anchors.get("repo")
    if repo and not _repo_text_matches(row.get("repo"), repo):
        return False
    path = anchors.get("path")
    if path and not _planning_context_path_matches(str(row.get("path") or ""), path):
        return False
    if line is not None and not _line_matches_symbol(line, row):
        return False
    return True


def _planning_context_fact_row_matches(
    row: JsonObject,
    anchors: dict[str, str | None],
    *,
    line: int | None,
) -> bool:
    fact = row.get("_fact")
    subject = row.get("_subject")
    object_ = row.get("_object")
    if not isinstance(fact, dict) or not isinstance(subject, dict) or not isinstance(object_, dict):
        return False
    if anchors.get("repo") and not _planning_context_fact_matches_repo(fact, subject, object_, anchors["repo"]):
        return False
    if anchors.get("package") and not _planning_context_fact_matches_package(fact, subject, object_, anchors["package"]):
        return False
    if anchors.get("endpoint") and not _planning_context_fact_matches_endpoint(subject, object_, anchors["endpoint"]):
        return False
    if anchors.get("event_channel") and not _planning_context_fact_matches_event_channel(subject, object_, anchors["event_channel"]):
        return False
    if anchors.get("domain") and not _planning_context_fact_matches_domain(subject, object_, anchors["domain"]):
        return False
    if anchors.get("service") and not _planning_context_fact_matches_service(subject, object_, anchors["service"]):
        return False
    path = anchors.get("path")
    evidence_rows = row.get("evidence", [])
    if path and not _planning_context_fact_matches_path_or_line(
        subject, object_, evidence_rows, path=path, line=line
    ):
        return False
    if path is None and line is not None and not _planning_context_fact_matches_path_or_line(
        subject, object_, evidence_rows, path=None, line=line
    ):
        return False
    return True


def _planning_context_fact_matches_repo(fact: JsonObject, subject: JsonObject, object_: JsonObject, repo: str) -> bool:
    qualifier = fact.get("qualifier", {})
    if _repo_text_matches(qualifier.get("consumer_repo"), repo):
        return True
    return any(_repo_text_matches(_planning_context_entity_repo(entity), repo) for entity in (subject, object_))


def _planning_context_fact_matches_package(fact: JsonObject, subject: JsonObject, object_: JsonObject, package: str) -> bool:
    needle = package.strip().lower()
    qualifier = fact.get("qualifier", {})
    candidates = (
        str(qualifier.get("package_name") or ""),
        str(qualifier.get("distribution_name") or ""),
        str(qualifier.get("import_root") or ""),
        _planning_context_entity_name(subject),
        _planning_context_entity_name(object_),
    )
    return any(value.strip().lower() == needle for value in candidates if value)


def _normalize_repo_text(value: object) -> str:
    return str(value or "").strip().lower()


def _repo_text_matches(candidate: object, requested: object) -> bool:
    candidate_key = _normalize_repo_text(candidate)
    requested_key = _normalize_repo_text(requested)
    if not candidate_key or not requested_key:
        return False
    if candidate_key == requested_key:
        return True
    if "/" in candidate_key and "/" in requested_key:
        return False
    return candidate_key.rsplit("/", 1)[-1] == requested_key.rsplit("/", 1)[-1]


def _planning_context_service_row_search_text(row: JsonObject) -> str:
    values = [
        row.get("slug"),
        row.get("repo"),
        row.get("namespace"),
        row.get("name"),
    ]
    return " ".join(str(value) for value in values if value is not None)


def _planning_context_fact_matches_endpoint(subject: JsonObject, object_: JsonObject, endpoint: str) -> bool:
    return any(_planning_context_endpoint_entity_matches(entity, endpoint) for entity in (subject, object_))


def _planning_context_fact_matches_event_channel(subject: JsonObject, object_: JsonObject, event_channel: str) -> bool:
    return any(_planning_context_event_channel_entity_matches(entity, event_channel) for entity in (subject, object_))


def _planning_context_fact_matches_domain(subject: JsonObject, object_: JsonObject, domain: str) -> bool:
    return any(_planning_context_domain_entity_matches(entity, domain) for entity in (subject, object_))


def _planning_context_fact_matches_service(subject: JsonObject, object_: JsonObject, service: str) -> bool:
    return any(entity.get("kind") == "Service" and _service_exact_match(service, entity) for entity in (subject, object_))


def _planning_context_fact_matches_path_or_line(
    subject: JsonObject,
    object_: JsonObject,
    evidence_rows: object,
    *,
    path: str | None,
    line: int | None,
) -> bool:
    if any(_planning_context_entity_matches_coordinate(entity, path=path, line=line) for entity in (subject, object_)):
        return True
    if not isinstance(evidence_rows, list):
        return False
    for evidence in evidence_rows:
        if not isinstance(evidence, dict):
            continue
        bytes_ref = evidence.get("bytes_ref") or {}
        evidence_path = str(bytes_ref.get("path") or "")
        if path is not None and not _planning_context_path_matches(evidence_path, path):
            continue
        if line is None:
            return True
        line_start = int(bytes_ref.get("line_start") or 0)
        line_end = int(bytes_ref.get("line_end") or line_start)
        if line_start <= line <= line_end:
            return True
    return False


def _planning_context_entity_matches_coordinate(entity: JsonObject, *, path: str | None, line: int | None) -> bool:
    properties = entity.get("properties", {})
    entity_path = str(properties.get("path") or "")
    if path is not None and not _planning_context_path_matches(entity_path, path):
        return False
    if line is None:
        return bool(entity_path) if path is not None else True
    start = properties.get("line")
    if not isinstance(start, int):
        return False
    end = properties.get("end_line")
    line_end = end if isinstance(end, int) else start
    return start <= line <= line_end


def _planning_context_path_matches(candidate: str, target: str) -> bool:
    return candidate.replace("\\", "/").lstrip("./") == target.replace("\\", "/").lstrip("./")


def _planning_context_entity_repo(entity: JsonObject) -> str | None:
    identity = entity.get("identity", {})
    properties = entity.get("properties", {})
    repo = identity.get("repo") or properties.get("repo")
    if not isinstance(repo, str):
        return None
    return repo.strip().lower() or None


def _planning_context_entity_name(entity: JsonObject) -> str:
    identity = entity.get("identity", {})
    return str(
        identity.get("name")
        or identity.get("qualname")
        or identity.get("slug")
        or identity.get("path")
        or display_entity(entity)
    )


def _planning_context_endpoint_entity_matches(entity: JsonObject, endpoint: str) -> bool:
    if entity.get("kind") != "Endpoint":
        return False
    return _planning_context_endpoint_path_matches(str(entity.get("identity", {}).get("path") or ""), endpoint)


def _planning_context_event_channel_entity_matches(entity: JsonObject, event_channel: str) -> bool:
    if entity.get("kind") != "EventChannel":
        return False
    identity = entity.get("identity", {})
    return _planning_context_value_matches(event_channel, identity.get("channel_address"), identity.get("name"))


def _planning_context_domain_entity_matches(entity: JsonObject, domain: str) -> bool:
    if entity.get("kind") != "Domain":
        return False
    return str(entity.get("identity", {}).get("name") or "").strip().lower() == domain.strip().lower()


def _planning_context_value_matches(anchor: str, *candidates: object) -> bool:
    needle = anchor.strip().lower()
    normalized = {
        str(candidate).strip().lower()
        for candidate in candidates
        if candidate is not None and str(candidate).strip()
    }
    return needle in normalized


def _planning_context_normalize_path(path: str) -> str:
    # Strip only literal "./" prefixes; lstrip("./") is a char-class strip that
    # would corrupt "../x" and "/abs/x".
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _changed_ranges_by_path(changed_ranges: list[JsonObject]) -> dict[str, list[tuple[int, int]]]:
    by_path: dict[str, list[tuple[int, int]]] = {}
    for row in changed_ranges:
        by_path.setdefault(_planning_context_normalize_path(str(row["path"])), []).append(
            (int(row["start_line"]), int(row["end_line"]))
        )
    return by_path


def _review_context_symbol_overlaps_ranges(symbol: JsonObject, ranges: list[tuple[int, int]]) -> bool:
    bounds = _review_context_symbol_bounds(symbol)
    if bounds is None:
        return False
    line_start, line_end = bounds
    for range_start, range_end in ranges:
        if line_start <= range_end and range_start <= line_end:
            return True
    return False


def _review_context_anchor_for_range(
    *,
    repo: str,
    path: str,
    range_start: int,
    range_end: int,
    symbols: list[JsonObject],
) -> tuple[JsonObject, list[JsonObject]]:
    enclosing = [
        symbol
        for symbol in symbols
        if (bounds := _review_context_symbol_bounds(symbol)) is not None
        and bounds[0] <= range_start
        and range_end <= bounds[1]
    ]
    if enclosing:
        selected = _review_context_most_specific_symbols(enclosing)
        return (
            _review_context_symbol_anchor(
                repo=repo,
                path=path,
                range_start=range_start,
                range_end=range_end,
                match_kind="enclosing_symbol",
                symbols=selected,
            ),
            selected,
        )

    overlapping = [
        symbol
        for symbol in symbols
        if _review_context_symbol_overlaps_ranges(symbol, [(range_start, range_end)])
    ]
    if overlapping:
        selected = _review_context_most_specific_symbols(overlapping)
        return (
            _review_context_symbol_anchor(
                repo=repo,
                path=path,
                range_start=range_start,
                range_end=range_end,
                match_kind="overlapping_symbol",
                symbols=selected,
            ),
            selected,
        )

    return (
        _review_context_file_anchor(
            repo=repo,
            path=path,
            match_kind="changed_range_without_indexed_symbol",
            symbol_count=0,
            range_start=range_start,
            range_end=range_end,
        ),
        [],
    )


def _review_context_symbol_anchor(
    *,
    repo: str,
    path: str,
    range_start: int,
    range_end: int,
    match_kind: str,
    symbols: list[JsonObject],
) -> JsonObject:
    public_symbols = _review_context_public_symbol_rows(symbols)
    return {
        "repo": repo,
        "path": path,
        "range": {"start_line": range_start, "end_line": range_end},
        "anchor_type": "symbol",
        "match_kind": match_kind,
        "symbol_count": len(public_symbols),
        "symbols": public_symbols,
        "source_coordinates": [
            {
                "repo": repo,
                "path": path,
                "line_start": range_start,
                "line_end": range_end,
                "provenance": "changed_range",
            }
        ],
    }


def _review_context_file_anchor(
    *,
    repo: str,
    path: str,
    match_kind: str,
    symbol_count: int,
    range_start: int | None = None,
    range_end: int | None = None,
) -> JsonObject:
    anchor: JsonObject = {
        "repo": repo,
        "path": path,
        "anchor_type": "file",
        "match_kind": match_kind,
        "symbol_count": symbol_count,
        "source_coordinates": [],
    }
    if range_start is not None and range_end is not None:
        anchor["range"] = {"start_line": range_start, "end_line": range_end}
        anchor["source_coordinates"] = [
            {
                "repo": repo,
                "path": path,
                "line_start": range_start,
                "line_end": range_end,
                "provenance": "changed_range",
            }
        ]
    return anchor


def _review_context_anchor_coordinate_rows(diff_anchors: list[JsonObject]) -> list[JsonObject]:
    rows: list[JsonObject] = []
    for anchor in diff_anchors:
        coordinates = anchor.get("source_coordinates")
        if not isinstance(coordinates, list):
            continue
        for coordinate in coordinates:
            if not isinstance(coordinate, dict):
                continue
            rows.append(
                {
                    "repo": coordinate.get("repo"),
                    "path": coordinate.get("path"),
                    "line": coordinate.get("line_start"),
                    "end_line": coordinate.get("line_end"),
                }
            )
    return rows


def _review_context_symbol_bounds(symbol: JsonObject) -> tuple[int, int] | None:
    line = symbol.get("line")
    if isinstance(line, bool) or not isinstance(line, int):
        return None
    end_line = symbol.get("end_line")
    if isinstance(end_line, bool) or not isinstance(end_line, int) or end_line < line:
        end_line = None
    if end_line is not None:
        return line, end_line
    for evidence in symbol.get("evidence", []):
        if not isinstance(evidence, dict):
            continue
        bytes_ref = evidence.get("bytes_ref")
        if not isinstance(bytes_ref, dict):
            continue
        evidence_start = bytes_ref.get("line_start")
        evidence_end = bytes_ref.get("line_end")
        if (
            isinstance(evidence_start, int)
            and not isinstance(evidence_start, bool)
            and isinstance(evidence_end, int)
            and not isinstance(evidence_end, bool)
            and evidence_start <= line <= evidence_end
        ):
            return evidence_start, evidence_end
    return line, line


def _review_context_most_specific_symbols(symbols: list[JsonObject]) -> list[JsonObject]:
    if len(symbols) <= 1:
        return symbols
    kept: list[JsonObject] = []
    for index, symbol in enumerate(symbols):
        bounds = _review_context_symbol_bounds(symbol)
        path = _planning_context_normalize_path(str(symbol.get("path") or ""))
        if bounds is None:
            kept.append(symbol)
            continue
        start, end = bounds
        contained_by_more_specific = False
        for other_index, other in enumerate(symbols):
            if index == other_index:
                continue
            other_path = _planning_context_normalize_path(str(other.get("path") or ""))
            if other_path != path:
                continue
            other_bounds = _review_context_symbol_bounds(other)
            if other_bounds is None:
                continue
            other_start, other_end = other_bounds
            if start <= other_start and other_end <= end and (other_start, other_end) != (start, end):
                contained_by_more_specific = True
                break
        if not contained_by_more_specific:
            kept.append(symbol)
    return kept


def _optional_symbol_path(symbol: JsonObject) -> str | None:
    path = symbol.get("path")
    return path if isinstance(path, str) and path else None


def _optional_symbol_line(symbol: JsonObject) -> int | None:
    line = symbol.get("line")
    return line if isinstance(line, int) else None


def _review_context_dedupe_rows(rows: list[JsonObject]) -> list[JsonObject]:
    deduped: list[JsonObject] = []
    seen: set[str] = set()
    for row in rows:
        key = str(
            row.get("fact_id")
            or row.get("symbol_id")
            or row.get("service_id")
            or row.get("qualified_name")
            or row
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def _review_context_public_symbol_rows(rows: list[JsonObject]) -> list[JsonObject]:
    public_rows = _planning_context_public_rows(rows)
    for row in public_rows:
        bounds = _review_context_symbol_bounds(row)
        if bounds is None:
            continue
        row["line_start"] = bounds[0]
        row["line_end"] = bounds[1]
    return public_rows


def _review_context_repo_matches(row_repo: object, requested_repo: object) -> bool:
    return _repo_text_matches(row_repo, requested_repo)


def _review_context_changed_surface(
    *,
    changed_files: list[str],
    range_filters: dict[str, list[tuple[int, int]]],
    symbols_by_path: dict[str, list[JsonObject]],
    changed_symbols: list[JsonObject],
) -> JsonObject:
    files = []
    for changed_file in changed_files:
        normalized_path = _planning_context_normalize_path(changed_file)
        ranges = [
            {"start_line": start_line, "end_line": end_line}
            for start_line, end_line in range_filters.get(normalized_path, [])
        ]
        files.append(
            {
                "path": changed_file,
                "normalized_path": normalized_path,
                "ranges": ranges,
                "symbol_count": len(symbols_by_path.get(normalized_path, [])),
            }
        )
    return {
        "files": files,
        "symbols": _planning_context_public_rows(changed_symbols)[:PLANNING_CONTEXT_SECTION_LIMIT],
    }


def _review_context_scope_contract(
    *,
    changed_ranges: list[JsonObject],
    changed_symbols: list[JsonObject],
    changed_file_symbols: list[JsonObject],
) -> JsonObject:
    return {
        "changed_symbols": (
            "Symbols whose source span overlaps changed_ranges."
            if changed_ranges
            else (
                "No changed_ranges were supplied, so top-level changed_symbols is empty: the KG cannot tell which "
                "symbols changed without ranges. Read changed_file_symbols for the bounded changed-file symbol "
                "inventory (source-inspection context), then inspect the diff before claiming any symbol changed."
            )
        ),
        "review_answer_packet.top_changed_symbols": (
            "Range-overlap changed symbols only; empty when changed_ranges are omitted."
        ),
        "review_answer_packet.changed_file_symbol_inventory": (
            "Bounded symbol inventory for changed files; source-inspection context, not proof of touched symbols."
        ),
        "diff_anchors": (
            "Changed file/range anchors. Symbol anchors come from indexed KG symbol spans; file anchors mean no indexed "
            "symbol enclosed or overlapped the changed range, so inspect the file directly."
        ),
        "changed_file_symbols": "Bounded symbol inventory for the changed files; these are context, not all changed symbols.",
        "direct_callers_of_changed_symbols": "Incoming CALLS edges for changed_symbols only.",
        "direct_callees_from_changed_symbols": "Outgoing CALLS edges from changed_symbols only.",
        "transitive_callers": "Bounded reverse CALLS closure from changed_symbols up to depth 3; limit is a total edge cap, not per-depth.",
        "changed_symbol_count": len(changed_symbols),
        "changed_file_symbol_count": len(changed_file_symbols),
    }


def _review_context_claim_contract() -> JsonObject:
    return {
        "scope": "bounded static review context for changed files and optional ranges",
        "known_runtime_rows": [
            "runtime_surfaces.endpoints",
            "runtime_surfaces.endpoint_consumers",
            "runtime_surfaces.event_channels",
            "runtime_surfaces.deploy_mappings",
            "application_impact.runtime_facts",
        ],
        "candidate_or_unlinked_rows": [
            "runtime_surfaces.candidate_or_unlinked_event_channels",
            "application_impact.cross_repo_name_leads",
            "surface_status rows with status=inventory_context or status=unlinked_lead",
            "coverage_gaps",
            "unsupported_scopes",
        ],
        "safety_rule": (
            "Known endpoint, event, deploy, contract, or application rows do not prove deploy safety, runtime health, "
            "schema compatibility, authorization completeness, ownership, or absence of unindexed consumers."
        ),
        "changed_symbol_rule": (
            "If changed_ranges were omitted, top-level changed_symbols is empty and the changed-file symbol "
            "inventory is exposed via changed_file_symbols / review_answer_packet.changed_file_symbol_inventory; "
            "that inventory is source-inspection context, so inspect the diff before claiming any listed symbol was touched."
        ),
        "required_caveat": (
            "For deploy order, safe-deploy, runtime routing, endpoint/schema compatibility, ownership, or security claims, "
            "separate known static rows from missing fact families, unsupported scopes, unlinked leads, and source-verified findings."
        ),
    }


def _review_context_transitive_callers(
    kg: KgSnapshot,
    *,
    changed_symbols: list[JsonObject],
    depth: int,
    limit: int,
) -> list[JsonObject]:
    root_ids: list[str] = []
    seen_root_ids: set[str] = set()
    for row in changed_symbols:
        symbol_id = row.get("symbol_id")
        if not isinstance(symbol_id, str) or not symbol_id or symbol_id in seen_root_ids:
            continue
        seen_root_ids.add(symbol_id)
        root_ids.append(symbol_id)
    if not root_ids:
        return []
    incoming: dict[str, list[JsonObject]] = {}
    for fact in kg.facts:
        if fact.get("predicate") != "CALLS":
            continue
        object_id = fact.get("object_id")
        if not isinstance(object_id, str):
            continue
        incoming.setdefault(object_id, []).append(fact)
    seen_nodes = set(root_ids)
    queued: list[tuple[str, int]] = [(root_id, 0) for root_id in root_ids]
    results: list[JsonObject] = []
    seen_edges: set[str] = set()
    while queued and len(results) < limit:
        current_id, current_depth = queued.pop(0)
        if current_depth >= depth:
            continue
        for fact in incoming.get(current_id, []):
            caller = kg.entities_by_id.get(fact.get("subject_id"))
            callee = kg.entities_by_id.get(fact.get("object_id"))
            if not caller or not callee:
                continue
            edge_key = str(
                fact.get("fact_id")
                or canonical_json(
                    {
                        "subject_id": fact.get("subject_id"),
                        "object_id": fact.get("object_id"),
                        "predicate": fact.get("predicate"),
                        "qualifier": fact.get("qualifier", {}),
                        "fact": fact,
                    }
                )
            )
            if edge_key in seen_edges:
                continue
            seen_edges.add(edge_key)
            row = _fact_result(kg, fact, caller, callee)
            row["depth"] = current_depth + 1
            results.append(row)
            caller_id = caller.get("entity_id")
            if isinstance(caller_id, str) and caller_id not in seen_nodes:
                seen_nodes.add(caller_id)
                queued.append((caller_id, current_depth + 1))
            if len(results) >= limit:
                break
    return results


def _review_context_runtime_surfaces(
    kg: KgSnapshot,
    *,
    repo: str,
    changed_symbols: list[JsonObject],
    limit: int,
) -> dict[str, list[JsonObject]]:
    changed_symbol_ids = {
        str(row["symbol_id"]) for row in changed_symbols if isinstance(row.get("symbol_id"), str) and row["symbol_id"]
    }
    endpoints: list[JsonObject] = []
    exposed_endpoints: list[JsonObject] = []
    event_channels: list[JsonObject] = []
    candidate_event_channels: list[JsonObject] = []
    deploy_mappings: list[JsonObject] = []
    for fact in kg.facts:
        predicate = fact.get("predicate")
        if predicate not in {
            "EXPOSES_ENDPOINT",
            "CALLS_ENDPOINT",
            "DOCUMENTS_ENDPOINT",
            "REFERENCES_EVENT_CHANNEL",
            "CONSUMES_EVENT",
            "PRODUCES_EVENT",
            "ROUTES_DOMAIN_TO_DEPLOY",
            "DEPLOYS_VIA_CONFIG",
        }:
            continue
        subject = kg.entities_by_id.get(fact.get("subject_id"))
        object_ = kg.entities_by_id.get(fact.get("object_id"))
        if not subject or not object_:
            continue
        if not _review_context_fact_is_in_review_scope(
            subject,
            object_,
            repo=repo,
            changed_symbol_ids=changed_symbol_ids,
        ):
            continue
        row = _fact_result(kg, fact, subject, object_)
        if predicate in {"EXPOSES_ENDPOINT", "CALLS_ENDPOINT", "DOCUMENTS_ENDPOINT"}:
            endpoints.append(row)
            if predicate == "EXPOSES_ENDPOINT":
                exposed_endpoints.append(_planning_context_fact_result(kg, fact, subject, object_))
        elif predicate in _EVENT_FACT_PREDICATES:
            if _is_known_event_fact(fact, object_):
                event_channels.append(row)
            else:
                candidate_event_channels.append(row)
        elif predicate in {"ROUTES_DOMAIN_TO_DEPLOY", "DEPLOYS_VIA_CONFIG"}:
            if predicate == "ROUTES_DOMAIN_TO_DEPLOY" or _is_known_deploy_fact(fact):
                deploy_mappings.append(row)
    endpoint_consumers = _endpoint_consumer_rows_for_exposed_endpoints(kg, exposed_endpoints)
    return {
        "endpoints": _planning_context_dedupe_rows(endpoints)[:limit],
        "endpoint_consumers": endpoint_consumers[:limit],
        "event_channels": _planning_context_dedupe_rows(event_channels)[:limit],
        "candidate_or_unlinked_event_channels": _planning_context_dedupe_rows(candidate_event_channels)[:limit],
        "deploy_mappings": _planning_context_dedupe_rows(deploy_mappings)[:limit],
    }


def _review_context_fact_is_in_review_scope(
    subject: JsonObject,
    object_: JsonObject,
    *,
    repo: str,
    changed_symbol_ids: set[str],
) -> bool:
    if subject.get("entity_id") in changed_symbol_ids or object_.get("entity_id") in changed_symbol_ids:
        return True
    return any(_review_context_repo_matches(_review_context_entity_repo(entity), repo) for entity in (subject, object_))


def _review_context_entity_repo(entity: JsonObject) -> str | None:
    identity = entity.get("identity")
    properties = entity.get("properties")
    if not isinstance(identity, dict):
        identity = {}
    if not isinstance(properties, dict):
        properties = {}
    repo = identity.get("repo") or properties.get("repo")
    return str(repo) if repo is not None else None


def _review_context_summary(
    *,
    changed_files: list[str],
    changed_symbols: list[JsonObject],
    changed_file_symbols: list[JsonObject],
    diff_anchors: list[JsonObject],
    direct_callers: list[JsonObject],
    direct_callees: list[JsonObject],
    transitive_callers: list[JsonObject],
    repo_dependencies: list[JsonObject],
    endpoints: list[JsonObject],
    endpoint_consumers: list[JsonObject],
    event_channels: list[JsonObject],
    candidate_or_unlinked_event_channels: list[JsonObject],
    deploy_mappings: list[JsonObject],
    source_coordinates: list[JsonObject],
    framework_impact: JsonObject,
    application_impact: JsonObject,
) -> JsonObject:
    return {
        "changed_file_count": len(changed_files),
        "changed_symbol_count": len(changed_symbols),
        "changed_file_symbol_count": len(changed_file_symbols),
        "diff_anchor_count": len(diff_anchors),
        "symbol_anchor_count": sum(1 for anchor in diff_anchors if anchor.get("anchor_type") == "symbol"),
        "file_anchor_count": sum(1 for anchor in diff_anchors if anchor.get("anchor_type") == "file"),
        "direct_caller_count": len(direct_callers),
        "direct_callee_count": len(direct_callees),
        "transitive_caller_count": len(transitive_callers),
        "repo_dependency_count": len(repo_dependencies),
        "endpoint_fact_count": len(endpoints),
        "endpoint_consumer_fact_count": len(endpoint_consumers),
        "event_fact_count": len(event_channels),
        "candidate_or_unlinked_event_fact_count": len(candidate_or_unlinked_event_channels),
        "deploy_mapping_count": len(deploy_mappings),
        "framework_model_count": _summary_count(framework_impact, "changed_framework_model_count"),
        "framework_task_count": _summary_count(framework_impact, "task_count"),
        "framework_relation_count": _summary_count(framework_impact, "model_relation_count"),
        "app_surface_count": _summary_count(application_impact, "same_repo_entity_count"),
        "app_runtime_fact_count": _summary_count(application_impact, "runtime_fact_count"),
        "app_cross_repo_lead_count": _summary_count(application_impact, "cross_repo_name_lead_count"),
        "source_coordinate_count": len(source_coordinates),
        "section_limit": PLANNING_CONTEXT_SECTION_LIMIT,
    }


def _summary_count(packet: JsonObject, field_name: str) -> int:
    summary = packet.get("summary")
    if not isinstance(summary, dict):
        raise ValueError("context packet is missing required summary object")
    value = summary.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"context packet summary field {field_name} must be an integer")
    return value


def _review_context_answer_packet(
    *,
    status: str,
    summary: JsonObject,
    scope_contract: JsonObject,
    claim_contract: JsonObject,
    changed_symbols: list[JsonObject],
    changed_file_symbols: list[JsonObject],
    diff_anchors: list[JsonObject],
    direct_callers: list[JsonObject],
    direct_callees: list[JsonObject],
    transitive_callers: list[JsonObject],
    framework_impact: JsonObject,
    application_impact: JsonObject,
    runtime_surfaces: dict[str, list[JsonObject]],
    surface_status: list[JsonObject],
    answerability: JsonObject,
) -> JsonObject:
    same_repo_surfaces = application_impact.get("same_repo_surfaces", {})
    if not isinstance(same_repo_surfaces, dict):
        same_repo_surfaces = {}
    return {
        "status": status,
        "answerability": answerability,
        "summary": {
            "changed_symbol_count": len(changed_symbols),
            "changed_file_symbol_count": len(changed_file_symbols),
            "diff_anchor_count": len(diff_anchors),
            "symbol_anchor_count": summary["symbol_anchor_count"],
            "file_anchor_count": summary["file_anchor_count"],
            "direct_caller_count": len(direct_callers),
            "direct_callee_count": len(direct_callees),
            "transitive_caller_count": len(transitive_callers),
            "framework_model_count": summary["framework_model_count"],
            "framework_relation_count": summary["framework_relation_count"],
            "app_surface_count": summary["app_surface_count"],
            "app_runtime_fact_count": summary["app_runtime_fact_count"],
            "app_cross_repo_lead_count": summary["app_cross_repo_lead_count"],
            "candidate_or_unlinked_event_fact_count": summary["candidate_or_unlinked_event_fact_count"],
            "detail_limit": summary["detail_limit"],
        },
        "scope_contract": scope_contract,
        "claim_contract": claim_contract,
        "top_diff_anchors": diff_anchors[:PLANNING_CONTEXT_SECTION_LIMIT],
        "top_changed_symbols": changed_symbols[:PLANNING_CONTEXT_SECTION_LIMIT],
        "changed_file_symbol_inventory": changed_file_symbols[:PLANNING_CONTEXT_SECTION_LIMIT],
        "top_direct_callers": direct_callers[:PLANNING_CONTEXT_SECTION_LIMIT],
        "top_direct_callees": direct_callees[:PLANNING_CONTEXT_SECTION_LIMIT],
        "top_transitive_callers": transitive_callers[:PLANNING_CONTEXT_SECTION_LIMIT],
        "framework": {
            "changed_models": framework_impact.get("changed_models", [])[:PLANNING_CONTEXT_SECTION_LIMIT],
            "model_fields": framework_impact.get("model_fields", [])[:PLANNING_CONTEXT_SECTION_LIMIT],
            "model_relations": framework_impact.get("model_relations", [])[:PLANNING_CONTEXT_SECTION_LIMIT],
            "relationship_paths": framework_impact.get("relationship_paths", [])[:PLANNING_CONTEXT_SECTION_LIMIT],
            "serializers": framework_impact.get("serializers", [])[:PLANNING_CONTEXT_SECTION_LIMIT],
            "views": framework_impact.get("views", [])[:PLANNING_CONTEXT_SECTION_LIMIT],
            "tasks": framework_impact.get("tasks", [])[:PLANNING_CONTEXT_SECTION_LIMIT],
        },
        "application": {
            "api": same_repo_surfaces.get("api", [])[:PLANNING_CONTEXT_SECTION_LIMIT],
            "models": same_repo_surfaces.get("models", [])[:PLANNING_CONTEXT_SECTION_LIMIT],
            "serializers": same_repo_surfaces.get("serializers", [])[:PLANNING_CONTEXT_SECTION_LIMIT],
            "workers": same_repo_surfaces.get("workers", [])[:PLANNING_CONTEXT_SECTION_LIMIT],
            "scheduled_jobs": same_repo_surfaces.get("scheduled_jobs", [])[:PLANNING_CONTEXT_SECTION_LIMIT],
            "runtime_facts": application_impact.get("runtime_facts", [])[:PLANNING_CONTEXT_SECTION_LIMIT],
            "cross_repo_name_leads": application_impact.get("cross_repo_name_leads", [])[:PLANNING_CONTEXT_SECTION_LIMIT],
        },
        "runtime": {
            "endpoints": runtime_surfaces.get("endpoints", [])[:PLANNING_CONTEXT_SECTION_LIMIT],
            "endpoint_consumers": runtime_surfaces.get("endpoint_consumers", [])[:PLANNING_CONTEXT_SECTION_LIMIT],
            "event_channels": runtime_surfaces.get("event_channels", [])[:PLANNING_CONTEXT_SECTION_LIMIT],
            "candidate_or_unlinked_event_channels": runtime_surfaces.get(
                "candidate_or_unlinked_event_channels", []
            )[:PLANNING_CONTEXT_SECTION_LIMIT],
            "deploy_mappings": runtime_surfaces.get("deploy_mappings", [])[:PLANNING_CONTEXT_SECTION_LIMIT],
        },
        "surface_status": surface_status,
    }


def _review_context_surface_status(
    *,
    application_impact: JsonObject,
    runtime_surfaces: dict[str, list[JsonObject]],
    requested_surfaces: list[str],
    unknown_surfaces: list[str] | None = None,
    changed_symbols: list[JsonObject] | None = None,
) -> list[JsonObject]:
    surfaces = requested_surfaces if (requested_surfaces or unknown_surfaces) else list(REVIEW_CONTEXT_SURFACES)
    rows: list[JsonObject] = [
        _review_context_surface_status_row(
            surface,
            application_impact=application_impact,
            runtime_surfaces=runtime_surfaces,
        )
        for surface in surfaces
    ]
    capped = (unknown_surfaces or [])[:REVIEW_CONTEXT_UNKNOWN_SURFACE_CAP]
    omitted_unknown = max(0, len(unknown_surfaces or []) - REVIEW_CONTEXT_UNKNOWN_SURFACE_CAP)
    for token in capped:
        rows.append(_review_context_unknown_surface_status_row(token, changed_symbols=changed_symbols or []))
    if omitted_unknown and rows:
        rows[-1] = dict(rows[-1], omitted_unknown_surface_count=omitted_unknown)
    return rows


_REVIEW_CONTEXT_SURFACE_TOKEN_MAX_LEN = 200
_REVIEW_CONTEXT_SOURCE_INSPECTION_TERMS_MAX = 12


def _review_context_unknown_surface_status_row(
    token: str,
    *,
    changed_symbols: list[JsonObject],
) -> JsonObject:
    token = token[:_REVIEW_CONTEXT_SURFACE_TOKEN_MAX_LEN]
    words = [w for w in token.replace("-", "_").replace(" ", "_").split("_") if w and len(w) >= 2]
    terms: list[str] = []
    seen: set[str] = set()
    for t in [token] + words:
        if t and t not in seen:
            terms.append(t)
            seen.add(t)
    for row in changed_symbols[:5]:
        name = str(row.get("qualified_name") or row.get("qualname") or "")[:_REVIEW_CONTEXT_SURFACE_TOKEN_MAX_LEN]
        if name and name not in seen:
            terms.append(name)
            seen.add(name)
            if len(terms) >= _REVIEW_CONTEXT_SOURCE_INSPECTION_TERMS_MAX:
                break
    return {
        "surface": token,
        "status": "unsupported_or_unlinked",
        "source_inspection_terms": terms[:_REVIEW_CONTEXT_SOURCE_INSPECTION_TERMS_MAX],
    }


def _review_context_surface_status_row(
    surface: str,
    *,
    application_impact: JsonObject,
    runtime_surfaces: dict[str, list[JsonObject]],
) -> JsonObject:
    context_rows, unlinked_rows, evidence_path = _review_context_surface_rows(
        surface,
        application_impact=application_impact,
        runtime_surfaces=runtime_surfaces,
    )
    if context_rows:
        status = "inventory_context"
        interpretation = (
            "Rows exist in the review packet, but they are namespace, static, or term-selected context. "
            "Use them as source-inspection leads; they do not prove this surface is affected by the change."
        )
    elif unlinked_rows:
        status = "unlinked_lead"
        interpretation = "Only unlinked source leads are available; verify source before claiming impact."
    else:
        status = "missing"
        interpretation = "No current KG evidence for this requested surface."
    sample_rows = context_rows or unlinked_rows
    return {
        "surface": surface,
        "status": status,
        "known_count": 0,
        "context_count": len(context_rows),
        "unlinked_count": len(unlinked_rows),
        "evidence_path": evidence_path,
        "sample_rows": [_review_context_surface_sample(row) for row in sample_rows[:2]],
        "interpretation": interpretation,
    }


def _review_context_surface_rows(
    surface: str,
    *,
    application_impact: JsonObject,
    runtime_surfaces: dict[str, list[JsonObject]],
) -> tuple[list[JsonObject], list[JsonObject], str]:
    same_repo_surfaces = application_impact.get("same_repo_surfaces", {})
    if not isinstance(same_repo_surfaces, dict):
        same_repo_surfaces = {}
    cross_repo_leads = [
        row for row in application_impact.get("cross_repo_name_leads", []) if isinstance(row, dict)
    ]
    runtime_facts = [row for row in application_impact.get("runtime_facts", []) if isinstance(row, dict)]
    event_rows = [row for row in runtime_surfaces.get("event_channels", []) if isinstance(row, dict)]
    candidate_event_rows = [
        row for row in runtime_surfaces.get("candidate_or_unlinked_event_channels", []) if isinstance(row, dict)
    ]
    if surface == "scheduled_jobs":
        return _surface_list(same_repo_surfaces, "scheduled_jobs"), [], "application_impact.same_repo_surfaces.scheduled_jobs"
    if surface == "delivery_workers":
        return _surface_list(same_repo_surfaces, "workers"), [], "application_impact.same_repo_surfaces.workers"
    if surface == "api_surfaces":
        return _surface_list(same_repo_surfaces, "api"), [], "application_impact.same_repo_surfaces.api"
    if surface == "models":
        return _surface_list(same_repo_surfaces, "models"), [], "application_impact.same_repo_surfaces.models"
    if surface == "serializers":
        return _surface_list(same_repo_surfaces, "serializers"), [], "application_impact.same_repo_surfaces.serializers"
    if surface == "ui_screens":
        ui_leads = [
            row
            for row in cross_repo_leads
            if row.get("surface_role") == "api" or _review_context_row_mentions_any(row, {"screen", "screens", "view", "views", "ui"})
        ]
        return [], ui_leads, "application_impact.cross_repo_name_leads"
    if surface == "sqs_consumers":
        sqs_rows = [
            row
            for row in event_rows
            if row.get("predicate") == "CONSUMES_EVENT" and _review_context_row_mentions_any(row, {"sqs", "queue"})
        ]
        sqs_leads = [
            row
            for row in candidate_event_rows
            if _review_context_row_mentions_any(row, {"sqs", "queue"})
        ]
        return sqs_rows, sqs_leads, "runtime_surfaces.event_channels|runtime_surfaces.candidate_or_unlinked_event_channels"
    if surface == "tracking_paths":
        known_rows = [
            row
            for row in [*runtime_facts, *event_rows]
            if _review_context_row_mentions_any(row, {"tracking", "track"})
        ]
        unlinked_rows = [
            row
            for row in [*cross_repo_leads, *candidate_event_rows]
            if _review_context_row_mentions_any(row, {"tracking", "track"})
        ]
        return (
            known_rows,
            unlinked_rows,
            "application_impact.runtime_facts|runtime_surfaces.event_channels|"
            "application_impact.cross_repo_name_leads|runtime_surfaces.candidate_or_unlinked_event_channels",
        )
    raise ValueError(f"Unsupported review surface: {surface}")


def _surface_list(rows_by_role: JsonObject, role: str) -> list[JsonObject]:
    rows = rows_by_role.get(role, [])
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _review_context_surface_sample(row: JsonObject) -> JsonObject:
    return {
        key: row[key]
        for key in (
            "repo",
            "module",
            "qualname",
            "path",
            "predicate",
            "subject",
            "object",
            "surface_role",
            "match_basis",
        )
        if key in row
    }


def _review_context_row_mentions_any(row: JsonObject, terms: set[str]) -> bool:
    text = _review_context_row_text(row).lower()
    return any(term in text for term in terms)


def _review_context_row_text(value: object) -> str:
    if isinstance(value, dict):
        return " ".join(_review_context_row_text(item) for item in value.values())
    if isinstance(value, list):
        return " ".join(_review_context_row_text(item) for item in value)
    if value is None:
        return ""
    return str(value)


def _review_context_answerability(
    *,
    status: str,
    changed_symbols: list[JsonObject],
    include_deploy_blockers: bool,
    include_ownership_context: bool,
    surface_status: list[JsonObject],
    requested_surfaces: list[str],
) -> JsonObject:
    if status == "not_found":
        return {
            "status": "not_answerable",
            "missing_fact_families": ["review_anchor"],
            "unlinked_fact_families": [],
            "recommended_followups": ["Read the changed files directly or pass narrower changed_ranges."],
        }
    missing = []
    unlinked = []
    inventory_context = []
    if not changed_symbols:
        missing.append("changed_symbols")
    if include_deploy_blockers:
        missing.append("deploy_blockers")
    if include_ownership_context:
        missing.append("ownership_context")
    if requested_surfaces:
        for row in surface_status:
            surface = row.get("surface")
            if not isinstance(surface, str) or surface not in requested_surfaces:
                continue
            if row.get("status") == "missing":
                missing.append(surface)
            elif row.get("status") == "inventory_context":
                inventory_context.append(surface)
            elif row.get("status") in {"unlinked_lead", "unlinked"}:
                unlinked.append(surface)
    return {
        "status": "partial" if missing or unlinked or inventory_context else "answerable",
        "missing_fact_families": missing,
        "unlinked_fact_families": unlinked,
        "inventory_context_fact_families": inventory_context,
        "recommended_followups": _review_context_answerability_followups(missing, unlinked, inventory_context),
    }


def _review_context_answerability_followups(
    missing: list[str], unlinked: list[str], inventory_context: list[str]
) -> list[str]:
    actions = []
    if "changed_symbols" in missing:
        actions.append("Read the changed files directly; no indexed symbols overlapped the review scope.")
    if "deploy_blockers" in missing:
        actions.append("Use deployment manifests or source inspection; deploy-blocker facts are unsupported by the current KG.")
    if "ownership_context" in missing:
        actions.append("Call planning_context with the repo or service and read ownership_context.answer_packet before claiming owners.")
    for surface in missing:
        if surface in REVIEW_CONTEXT_SURFACES:
            actions.append(f"Inspect source or runtime/config evidence for requested surface {surface}; the current KG has no proof.")
    for surface in inventory_context:
        actions.append(
            f"Inspect source for requested surface {surface}; current KG rows are inventory/context leads, not proof of affectedness."
        )
    for surface in unlinked:
        actions.append(f"Verify unlinked source leads for requested surface {surface} before claiming impact.")
    return actions


def _review_context_next_actions(
    answerability: JsonObject,
    *,
    unsupported_scopes: list[JsonObject],
) -> list[str]:
    actions = list(answerability.get("recommended_followups", []))
    if unsupported_scopes:
        actions.append("Treat unsupported_review_scopes as explicit coverage gaps, not as findings.")
    return actions


def _dependency_importer_packet(kg: KgSnapshot, dependencies: list[JsonObject], *, limit: int) -> JsonObject:
    package_ids = _dependency_package_ids(kg, dependencies)
    if not package_ids:
        return {
            "summary": {
                "package_count": 0,
                "importer_fact_count": 0,
                "importer_repo_count": 0,
                "section_limit": limit,
            },
            "packages": [],
            "importers": [],
            "repo_counts": {},
            "truncated": False,
        }
    packages = [
        kg.entities_by_id[package_id]
        for package_id in sorted(package_ids)
        if package_id in kg.entities_by_id
    ]
    importers: list[JsonObject] = []
    repo_counts: dict[str, int] = {}
    for fact in kg.facts:
        if fact.get("predicate") != "IMPORTS" or fact.get("object_id") not in package_ids:
            continue
        module = kg.entities_by_id.get(fact.get("subject_id"))
        package = kg.entities_by_id.get(fact.get("object_id"))
        if not module or not package:
            continue
        repo = _planning_context_entity_repo(module) or "unknown"
        repo_counts[repo] = repo_counts.get(repo, 0) + 1
        importers.append(_fact_result(kg, fact, module, package))
    importers = _planning_context_dedupe_rows(importers)
    return {
        "summary": {
            "package_count": len(packages),
            "importer_fact_count": len(importers),
            "importer_repo_count": len(repo_counts),
            "section_limit": limit,
        },
        "packages": [
            {
                "package_id": package.get("entity_id"),
                "name": display_entity(package),
                "identity": package.get("identity", {}),
                "properties": package.get("properties", {}),
            }
            for package in packages[:limit]
        ],
        "importers": importers[:limit],
        "repo_counts": dict(sorted(repo_counts.items(), key=lambda item: (-item[1], item[0]))[:limit]),
        "truncated": len(packages) > limit or len(importers) > limit or len(repo_counts) > limit,
    }


def _dependency_package_ids(kg: KgSnapshot, dependencies: list[JsonObject]) -> set[str]:
    facts_by_id = {str(fact.get("fact_id")): fact for fact in kg.facts if fact.get("fact_id")}
    package_ids: set[str] = set()
    for row in dependencies:
        fact_id = row.get("fact_id")
        if not isinstance(fact_id, str):
            continue
        fact = facts_by_id.get(fact_id)
        if not fact:
            continue
        predicate = fact.get("predicate")
        if predicate == "IMPORTS":
            package_ids.add(str(fact.get("object_id")))
        elif predicate in {"RESOLVES_TO_REPO", "RESOLVES_TO_SERVICE"}:
            package_ids.add(str(fact.get("subject_id")))
    return package_ids


def _planning_context_service_operational_surfaces(
    kg: KgSnapshot,
    services: list[JsonObject],
    *,
    limit: int,
) -> JsonObject:
    if len(services) != 1:
        return {
            "status": "not_computed",
            "reason": "service operational surfaces require one resolved service",
        }
    service_id = services[0].get("service_id") or services[0].get("entity_id")
    if not isinstance(service_id, str):
        return {
            "status": "not_computed",
            "reason": "resolved service missing service_id",
        }
    service = kg.entities_by_id.get(service_id)
    if not service:
        return {
            "status": "not_computed",
            "reason": "resolved service not found in snapshot",
        }
    return {
        "status": "found",
        **_service_operational_surfaces(kg, service, limit=limit),
    }


def _planning_context_service_related_rows(kg: KgSnapshot, service: JsonObject, *, limit: int) -> dict[str, list[JsonObject]]:
    related = _facts_touching_entity(kg, str(service["entity_id"]))
    exposed_endpoint_rows = _exposed_endpoint_rows_for_service_id(kg, str(service["entity_id"]))
    return {
        "dependencies": [
            row
            for row in related
            if row.get("predicate") in {"IMPORTS", "RESOLVES_TO_REPO", "RESOLVES_TO_SERVICE"}
        ][:limit],
        "endpoints": [
            row
            for row in related
            if row.get("predicate") in {"EXPOSES_ENDPOINT", "CALLS_ENDPOINT", "DOCUMENTS_ENDPOINT"}
        ][:limit],
        "endpoint_consumers": _endpoint_consumer_rows_for_exposed_endpoints(kg, exposed_endpoint_rows)[:limit],
        "event_channels": [
            row
            for row in related
            if _is_known_event_row(row)
        ][:limit],
        "candidate_or_unlinked_event_channels": [
            row
            for row in related
            if _is_candidate_or_unlinked_event_row(row)
        ][:limit],
        "domains": [
            row
            for row in related
            if row.get("predicate") in {"REFERENCES_DOMAIN", "ROUTES_DOMAIN_TO_DEPLOY"} or _is_known_deploy_row(row)
        ][:limit],
    }


def _planning_context_fleet_services(kg: KgSnapshot, *, limit: int) -> list[JsonObject]:
    services = [entity for entity in kg.entities if entity.get("kind") == "Service"]
    rows = []
    for service in sorted(services, key=display_entity)[:limit]:
        identity = service.get("identity")
        properties = service.get("properties")
        if not isinstance(identity, dict):
            identity = {}
        if not isinstance(properties, dict):
            properties = {}
        rows.append(
            {
                "service_id": service.get("entity_id"),
                "urn": service.get("urn"),
                "name": display_entity(service),
                "repo": identity.get("repo") or properties.get("repo"),
                "namespace": identity.get("namespace"),
                "slug": identity.get("slug"),
            }
        )
    return rows


def _planning_context_output(
    *,
    kg: KgSnapshot,
    query: str | None,
    anchors: dict[str, str | None],
    services: list[JsonObject],
    symbols: list[JsonObject],
    dependencies: list[JsonObject],
    endpoints: list[JsonObject],
    endpoint_consumers: list[JsonObject],
    event_channels: list[JsonObject],
    candidate_or_unlinked_event_channels: list[JsonObject],
    domains: list[JsonObject],
    next_actions: list[str],
    status: str,
    line: int | None = None,
) -> JsonObject:
    dependency_importers = _dependency_importer_packet(kg, dependencies, limit=PLANNING_CONTEXT_SECTION_LIMIT)
    bounded_services = _planning_context_public_rows(services)
    bounded_symbols = _planning_context_public_rows(symbols)
    bounded_dependencies = _planning_context_public_rows(dependencies)
    bounded_endpoints = _planning_context_public_rows(endpoints)
    bounded_endpoint_consumers = _planning_context_public_rows(endpoint_consumers)
    bounded_event_channels = _planning_context_public_rows(event_channels)
    bounded_candidate_event_channels = _planning_context_public_rows(candidate_or_unlinked_event_channels)
    bounded_domains = _planning_context_public_rows(domains)
    groups = {
        "services": bounded_services,
        "symbols": bounded_symbols,
        "dependencies": bounded_dependencies,
        "endpoints": bounded_endpoints,
        "endpoint_consumers": bounded_endpoint_consumers,
        "event_channels": bounded_event_channels,
        "candidate_or_unlinked_event_channels": bounded_candidate_event_channels,
        "domains": bounded_domains,
    }
    inventory = _snapshot_inventory_packet(kg, anchors=anchors, limit=PLANNING_CONTEXT_SECTION_LIMIT)
    service_operational_surfaces = _planning_context_service_operational_surfaces(
        kg,
        bounded_services,
        limit=PLANNING_CONTEXT_SECTION_LIMIT,
    )
    runtime_repo = anchors.get("repo") or _planning_context_single_service_repo(anchors, bounded_services)
    include_runtime_architecture = _planning_context_should_include_runtime_architecture(
        query=query,
        anchors=anchors,
        line=line,
    )
    runtime_architecture: JsonObject | None = None
    if include_runtime_architecture:
        runtime_architecture = runtime_architecture_packet(
            kg,
            repo=runtime_repo,
            limit=PLANNING_CONTEXT_SECTION_LIMIT,
            include_legacy_sections=False,
        )
        runtime_architecture = _planning_context_runtime_architecture_for_anchor_status(
            runtime_architecture,
            status=status,
            query=query,
            anchors=anchors,
            line=line,
        )
    include_authz_surface = _planning_context_should_include_authz_surface(
        query=query,
        anchors=anchors,
        line=line,
    )
    authz_surface: JsonObject | None = None
    if include_authz_surface:
        authz_surface = authz_surface_packet(
            kg,
            repo=runtime_repo,
            limit=PLANNING_CONTEXT_SECTION_LIMIT,
        )
    ownership_context = ownership_context_packet(
        kg,
        repo=runtime_repo,
        services=bounded_services,
        limit=PLANNING_CONTEXT_SECTION_LIMIT,
    )
    related_facts = _planning_context_related_facts(
        kg=kg,
        services=bounded_services,
        symbols=bounded_symbols,
        dependencies=bounded_dependencies,
        endpoints=bounded_endpoints,
        endpoint_consumers=bounded_endpoint_consumers,
        event_channels=bounded_event_channels,
        candidate_or_unlinked_event_channels=bounded_candidate_event_channels,
        domains=bounded_domains,
        anchors=anchors,
        status=status,
        line=line,
        dependency_importers=dependency_importers,
        inventory=inventory,
        service_operational_surfaces=service_operational_surfaces,
        runtime_architecture=runtime_architecture,
        authz_surface=authz_surface,
    )
    source_coordinates = _planning_context_source_coordinates(
        bounded_services,
        bounded_symbols,
        bounded_dependencies,
        bounded_endpoints,
        bounded_endpoint_consumers,
        bounded_event_channels,
        bounded_candidate_event_channels,
        bounded_domains,
        limit=PLANNING_CONTEXT_SECTION_LIMIT,
    )
    snapshot_scope = _planning_context_snapshot_scope(kg, anchors)
    indexed_scope_without_rows = status == "not_found" and _planning_context_has_indexed_scope(snapshot_scope)
    if indexed_scope_without_rows:
        next_actions = [action for action in next_actions if action != PLANNING_CONTEXT_NO_OVERLAP_ACTION]
        next_actions.append(
            "The repo anchor has indexed snapshot scope but no matching first-class dependency, endpoint, event, domain, service, or symbol rows for the supplied filters."
        )
        next_actions.append(
            "Use `snapshot_summary` and `snapshot_scope` for KG inventory counts, then inspect source or narrower anchors for behavioral claims."
        )
    # Answerability is computed with the original status so its indexed-scope branch (keyed
    # on not_found) still scopes which claim families are answerable.
    answerability = _planning_context_answerability(
        status=status,
        anchors=anchors,
        groups=groups,
        snapshot_scope=snapshot_scope,
    )
    # An indexed repo is not "not_found": surface a truthful top-level status so the agent
    # does not read the anchor as a hard miss. The answerability split still marks
    # behavioral claims not_answerable while inventory is answerable. Runtime gating and
    # related_facts above already ran with the original status, so they stay gated.
    result_status = "partial" if indexed_scope_without_rows else status
    result = {
        "status": result_status,
        "query": query,
        "summary": _planning_context_summary(groups, source_coordinates=source_coordinates),
        "snapshot_summary": _planning_context_snapshot_summary(kg),
        "snapshot_scope": snapshot_scope,
        "inventory": inventory,
        "service_operational_surfaces": service_operational_surfaces,
        "ownership_context": ownership_context,
        "anchors": {field: anchors.get(field) for field in _PLANNING_CONTEXT_ANCHOR_FIELDS},
        "services": bounded_services,
        "symbols": bounded_symbols,
        "dependencies": bounded_dependencies,
        "endpoints": bounded_endpoints,
        "endpoint_consumers": bounded_endpoint_consumers,
        "event_channels": bounded_event_channels,
        "candidate_or_unlinked_event_channels": bounded_candidate_event_channels,
        "domains": bounded_domains,
        "entry_points": _planning_context_entry_points(groups),
        "related_facts": related_facts,
        "source_coordinates": source_coordinates,
        "answerability": answerability,
        "evidence": _planning_context_evidence(
            bounded_services,
            bounded_symbols,
            bounded_dependencies,
            bounded_endpoints,
            bounded_endpoint_consumers,
            bounded_event_channels,
            bounded_candidate_event_channels,
            bounded_domains,
        ),
        "coverage_warnings": [],
        "unsupported_scopes": [],
        "next_actions": next_actions,
    }
    if runtime_architecture is not None:
        result["runtime_architecture"] = runtime_architecture
    if authz_surface is not None:
        result["authz_surface"] = authz_surface
    return result


_RUNTIME_ANSWER_SECTIONS = (
    "runtime_building_blocks",
    "domain_routing_map",
    "deploy_runtime_map",
    "endpoint_consumer_map",
    "deploy_order_guidance",
    "deploy_kind_counts",
)


def _planning_context_runtime_architecture_for_anchor_status(
    runtime_architecture: JsonObject,
    *,
    status: str,
    query: str | None,
    anchors: dict[str, str | None],
    line: int | None = None,
) -> JsonObject:
    if not _planning_context_should_gate_runtime_architecture(
        status=status, query=query, anchors=anchors, line=line
    ):
        return runtime_architecture
    answer_packet = runtime_architecture.get("answer_packet")
    if not isinstance(answer_packet, dict):
        answer_packet = {}
    investigation_brief = answer_packet.get("investigation_brief")
    if not isinstance(investigation_brief, dict):
        investigation_brief = {}
    omitted_sections = [
        section
        for section in _RUNTIME_ANSWER_SECTIONS
        if section in answer_packet and answer_packet.get(section) not in (None, [], {})
    ]
    gated_summary = _planning_context_gated_runtime_summary(runtime_architecture, investigation_brief)
    return {
        "scope": runtime_architecture.get("scope", {}),
        "summary": gated_summary,
        "answer_packet": {
            "investigation_brief": investigation_brief,
            "missing_fact_families": answer_packet.get("missing_fact_families", []),
            "evidence_contract": (
                "Planning anchor was ambiguous or unresolved. Runtime architecture is available only as an "
                "investigation brief; omitted runtime maps and counts are inventory context, not an answer to the query."
            ),
            "omitted_answer_sections": omitted_sections,
        },
        "anchor_resolution_contract": {
            "status": "inventory_context",
            "reason": (
                "The planning anchor did not resolve to a unique primary service/repo/symbol/domain/endpoint/event row."
            ),
            "claim_rule": (
                "Use investigation_brief rows to inspect source or retry with narrower anchors before making runtime "
                "building-block, domain-routing, deploy-order, endpoint-consumer, or event-surface claims."
            ),
            "omitted_answer_sections": omitted_sections,
        },
        "assembly_contract": runtime_architecture.get("assembly_contract"),
    }


def _planning_context_should_gate_runtime_architecture(
    *,
    status: str,
    query: str | None,
    anchors: dict[str, str | None],
    line: int | None,
) -> bool:
    if _is_planning_context_fleet_request(query=query, line=line, anchors=anchors):
        return False
    return status in {"ambiguous", "not_found"}


def _planning_context_should_include_runtime_architecture(
    *,
    query: str | None,
    anchors: dict[str, str | None],
    line: int | None,
) -> bool:
    if _is_planning_context_fleet_request(query=query, line=line, anchors=anchors):
        return True
    if query is not None and line is None and not any(anchors.values()):
        return True
    return any(
        anchors.get(field)
        for field in ("repo", "service", "endpoint", "event_channel", "domain")
    )


def _planning_context_should_include_authz_surface(
    *,
    query: str | None,
    anchors: dict[str, str | None],
    line: int | None,
) -> bool:
    if _is_planning_context_fleet_request(query=query, line=line, anchors=anchors):
        return True
    if query is not None and line is None and not any(anchors.values()):
        return True
    return any(anchors.get(field) for field in ("repo", "service", "endpoint"))


def _planning_context_gated_runtime_summary(
    runtime_architecture: JsonObject, investigation_brief: JsonObject
) -> JsonObject:
    original_summary = runtime_architecture.get("summary")
    if not isinstance(original_summary, dict):
        original_summary = {}
    return {
        "answer_packet_mode": "investigation_brief_only",
        "runtime_anchor_count": _list_len(investigation_brief.get("runtime_anchors")),
        "known_route_lead_count": _list_len(investigation_brief.get("known_routes")),
        "unlinked_runtime_lead_count": _list_len(investigation_brief.get("unlinked_runtime_leads")),
        "deploy_unit_lead_count": _list_len(investigation_brief.get("deploy_units")),
        "unlinked_deploy_lead_count": _list_len(investigation_brief.get("unlinked_deploy_leads")),
        "consumer_link_lead_count": _list_len(investigation_brief.get("consumer_links")),
        "recommended_source_check_count": _list_len(investigation_brief.get("recommended_source_checks")),
        "original_count_fields_omitted": [
            key
            for key in (
                "domain_route_count",
                "deploy_link_count",
                "candidate_or_unlinked_deploy_lead_count",
                "endpoint_surface_count",
                "client_endpoint_call_count",
                "event_surface_count",
                "runtime_building_block_count",
                "domain_routing_map_count",
                "deploy_runtime_unit_count",
                "endpoint_consumer_map_count",
                "deploy_order_guidance_count",
            )
            if key in original_summary
        ],
    }


def _list_len(value: object) -> int:
    return len(value) if isinstance(value, list) else 0


def _planning_context_anchors(arguments: JsonObject) -> JsonObject:
    return {field: _optional_string(arguments, field) for field in _PLANNING_CONTEXT_ANCHOR_FIELDS}


def _planning_context_single_service_repo(anchors: dict[str, str | None], services: list[JsonObject]) -> str | None:
    if not anchors.get("service"):
        return None
    repos = {repo for service in services for repo in [service.get("repo")] if isinstance(repo, str) and repo.strip()}
    if len(repos) != 1:
        return None
    return next(iter(repos))


def _is_planning_context_fleet_request(*, query: str | None, line: int | None, anchors: JsonObject) -> bool:
    return query is None and line is None and not any(anchors.values())


def _planning_context_has_resolved_anchor(payload: JsonObject) -> bool:
    answerability = payload.get("answerability") if isinstance(payload.get("answerability"), dict) else {}
    answerability_status = answerability.get("status")
    if answerability_status in {"ambiguous", "not_answerable", "not_found", "unsupported_by_current_kg"}:
        return False
    if payload.get("status") in {"ambiguous", "not_found"}:
        return False
    return True


def _planning_context_summary(groups: dict[str, list[JsonObject]], *, source_coordinates: list[JsonObject]) -> JsonObject:
    return {
        "service_count": len(groups["services"]),
        "symbol_count": len(groups["symbols"]),
        "dependency_count": len(groups["dependencies"]),
        "endpoint_fact_count": len(groups["endpoints"]),
        "endpoint_consumer_fact_count": len(groups["endpoint_consumers"]),
        "event_fact_count": len(groups["event_channels"]),
        "candidate_or_unlinked_event_fact_count": len(groups["candidate_or_unlinked_event_channels"]),
        "domain_fact_count": len(groups["domains"]),
        "source_coordinate_count": len(source_coordinates),
        "section_limit": PLANNING_CONTEXT_SECTION_LIMIT,
    }


def _planning_context_snapshot_summary(kg: KgSnapshot) -> JsonObject:
    summary = kg.summary()
    entity_kinds = _top_count_map(summary.get("entity_kinds", {}), limit=10)
    predicates = _top_count_map(summary.get("predicates", {}), limit=10)
    coverage = summary.get("coverage", [])
    coverage_states: dict[str, int] = {}
    if isinstance(coverage, list):
        for row in coverage:
            if not isinstance(row, dict):
                continue
            state = str(row.get("state") or "unknown")
            coverage_states[state] = coverage_states.get(state, 0) + 1
    return {
        "scope": {"kind": "fleet"},
        "count_contract": "snapshot_summary counts cover the full loaded KG snapshot across all indexed repos.",
        "entity_count": len(kg.entities),
        "fact_count": len(kg.facts),
        "evidence_count": len(kg.evidence),
        "coverage_count": len(kg.coverage),
        "top_entity_kinds": entity_kinds,
        "top_predicates": predicates,
        "coverage_states": dict(sorted(coverage_states.items())),
    }


def _snapshot_inventory_packet(
    kg: KgSnapshot,
    *,
    anchors: dict[str, str | None],
    limit: int,
) -> JsonObject:
    repo = anchors.get("repo")
    repo_key = _normalize_repo_text(repo)
    scoped = bool(repo_key)
    coverage_rows = [
        row
        for row in kg.coverage
        if isinstance(row, dict) and (not scoped or _planning_context_coverage_row_matches_repo(row, repo_key))
    ]
    top_dependencies = _top_dependency_rows(kg, repo_key=repo_key if scoped else None, limit=limit)
    coverage_state_counts = _coverage_counts(coverage_rows, "state", limit=10)
    coverage_predicate_counts = _coverage_counts(coverage_rows, "predicate", limit=10)
    coverage_reason_counts = _coverage_counts(coverage_rows, "reason", limit=10)
    runtime_counts = _runtime_inventory_counts(kg, repo_key=repo_key if scoped else None)
    gap_samples = _coverage_gap_samples(coverage_rows, limit=limit)
    scope = {"kind": "repo", "repo": repo} if scoped else {"kind": "fleet"}
    return {
        "scope": scope,
        "count_contract": (
            f"inventory.summary counts are scoped to repo {repo}."
            if scoped
            else "inventory.summary counts cover the full loaded KG snapshot across all indexed repos."
        ),
        "summary": {
            "entity_count": _inventory_entity_count(kg, repo_key=repo_key if scoped else None),
            "fact_count": _inventory_fact_count(kg, repo_key=repo_key if scoped else None),
            "coverage_count": len(coverage_rows),
            "top_dependency_count": len(top_dependencies),
            "coverage_gap_sample_count": len(gap_samples),
            "section_limit": limit,
        },
        "top_dependencies": top_dependencies,
        "coverage": {
            "state_counts": coverage_state_counts,
            "predicate_counts": coverage_predicate_counts,
            "reason_counts": coverage_reason_counts,
            "gap_samples": gap_samples,
        },
        "runtime_counts": runtime_counts,
    }


def _top_dependency_rows(kg: KgSnapshot, *, repo_key: str | None, limit: int) -> list[JsonObject]:
    counts: dict[str, JsonObject] = {}
    for fact in kg.facts:
        if fact.get("predicate") != "IMPORTS":
            continue
        module = kg.entities_by_id.get(fact.get("subject_id"))
        package = kg.entities_by_id.get(fact.get("object_id"))
        if not module or not package or package.get("kind") != "ExternalPackage":
            continue
        if repo_key is not None and not _repo_text_matches(_planning_context_entity_repo(module), repo_key):
            continue
        qualifier = fact.get("qualifier", {})
        if not isinstance(qualifier, dict):
            qualifier = {}
        category = qualifier.get("category")
        if category in {"stdlib", "node_builtin", "unknown"}:
            continue
        name = str(package.get("identity", {}).get("name") or display_entity(package))
        row = counts.setdefault(
            name,
            {
                "name": name,
                "category": category,
                "import_root": qualifier.get("import_root"),
                "distribution_name": qualifier.get("distribution_name"),
                "importer_count": 0,
                "sample_evidence": [],
            },
        )
        row["importer_count"] += 1
        if len(row["sample_evidence"]) < 2:
            row["sample_evidence"].extend(kg.evidence_by_target.get(fact["fact_id"], [])[:1])
    return sorted(counts.values(), key=lambda row: (-int(row["importer_count"]), str(row["name"])))[:limit]


def _coverage_counts(rows: list[JsonObject], field: str, *, limit: int) -> JsonObject:
    counts: dict[str, int] = {}
    for row in rows:
        value = row.get(field)
        if value is None:
            continue
        key = str(value)
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit])


def _coverage_gap_samples(rows: list[JsonObject], *, limit: int) -> list[JsonObject]:
    samples = []
    for row in rows:
        state = str(row.get("state") or "")
        if state not in {"partially_instrumented", "uninstrumented"}:
            continue
        samples.append(
            {
                "state": row.get("state"),
                "predicate": row.get("predicate"),
                "reason": row.get("reason"),
                "scope_ref": row.get("scope_ref"),
            }
        )
        if len(samples) >= limit:
            break
    return samples


def _runtime_inventory_counts(kg: KgSnapshot, *, repo_key: str | None) -> JsonObject:
    entity_kinds = {"Service", "Endpoint", "EventChannel", "Domain", "DeployTarget"}
    predicate_kinds = {
        "EXPOSES_ENDPOINT",
        "CALLS_ENDPOINT",
        "PRODUCES_EVENT",
        "CONSUMES_EVENT",
        "REFERENCES_DOMAIN",
        "ROUTES_DOMAIN_TO_DEPLOY",
        "DEPLOYS_VIA_CONFIG",
    }
    entities: dict[str, int] = {}
    facts: dict[str, int] = {}
    candidate_deploy_link_count = 0
    for entity in kg.entities:
        kind = str(entity.get("kind"))
        if kind not in entity_kinds:
            continue
        if repo_key is not None and not _repo_text_matches(_planning_context_entity_repo(entity), repo_key):
            continue
        entities[kind] = entities.get(kind, 0) + 1
    for fact in kg.facts:
        predicate = str(fact.get("predicate"))
        if predicate not in predicate_kinds:
            continue
        if repo_key is not None and not _fact_touches_repo(kg, fact, repo_key):
            continue
        facts[predicate] = facts.get(predicate, 0) + 1
        if predicate == "DEPLOYS_VIA_CONFIG" and not _is_known_deploy_fact(fact):
            candidate_deploy_link_count += 1
    return {
        "entity_counts": dict(sorted(entities.items())),
        "fact_counts": dict(sorted(facts.items())),
        "fact_count_contract": (
            "fact_counts are raw scoped KG facts. DEPLOYS_VIA_CONFIG may include candidate facts; "
            "candidate_deploy_link_count reports that subset, while runtime_architecture.deploy_link_count is canonical-only."
        ),
        "candidate_deploy_link_count": candidate_deploy_link_count,
    }


def _inventory_entity_count(kg: KgSnapshot, *, repo_key: str | None) -> int:
    if repo_key is None:
        return len(kg.entities)
    return sum(1 for entity in kg.entities if _repo_text_matches(_planning_context_entity_repo(entity), repo_key))


def _inventory_fact_count(kg: KgSnapshot, *, repo_key: str | None) -> int:
    if repo_key is None:
        return len(kg.facts)
    return sum(1 for fact in kg.facts if _fact_touches_repo(kg, fact, repo_key))


def _fact_touches_repo(kg: KgSnapshot, fact: JsonObject, repo_key: str) -> bool:
    subject = kg.entities_by_id.get(fact.get("subject_id"))
    object_ = kg.entities_by_id.get(fact.get("object_id"))
    if subject and _repo_text_matches(_planning_context_entity_repo(subject), repo_key):
        return True
    if object_ and _repo_text_matches(_planning_context_entity_repo(object_), repo_key):
        return True
    qualifier = fact.get("qualifier", {})
    return isinstance(qualifier, dict) and _repo_text_matches(qualifier.get("consumer_repo"), repo_key)


def _planning_context_snapshot_scope(kg: KgSnapshot, anchors: dict[str, str | None]) -> JsonObject:
    repo = anchors.get("repo")
    if not repo:
        return {}
    repo_key = _normalize_repo_text(repo)
    entity_count = 0
    module_count = 0
    for entity in kg.entities:
        if not _planning_context_entity_in_repo_scope(entity, repo_key):
            continue
        entity_count += 1
        if entity.get("kind") == "CodeModule":
            module_count += 1
    fact_count = 0
    for fact in kg.facts:
        subject = kg.entities_by_id.get(fact.get("subject_id"))
        object_ = kg.entities_by_id.get(fact.get("object_id"))
        if subject and _planning_context_entity_in_repo_scope(subject, repo_key):
            fact_count += 1
            continue
        if object_ and _planning_context_entity_in_repo_scope(object_, repo_key):
            fact_count += 1
            continue
        qualifier = fact.get("qualifier", {})
        if isinstance(qualifier, dict) and _repo_text_matches(qualifier.get("consumer_repo"), repo_key):
            fact_count += 1
    facts_by_id = {fact.get("fact_id"): fact for fact in kg.facts}
    evidence_count = 0
    for row in kg.evidence:
        if not isinstance(row, dict):
            continue
        if _planning_context_evidence_in_repo_scope(row, repo_key, facts_by_id=facts_by_id, kg=kg):
            evidence_count += 1
    coverage_rows = [
        row
        for row in kg.coverage
        if isinstance(row, dict) and _planning_context_coverage_row_matches_repo(row, repo_key)
    ]
    coverage_states: dict[str, int] = {}
    coverage_predicates: dict[str, int] = {}
    for row in coverage_rows:
        state = str(row.get("state") or "unknown")
        predicate = str(row.get("predicate") or "unknown")
        coverage_states[state] = coverage_states.get(state, 0) + 1
        coverage_predicates[predicate] = coverage_predicates.get(predicate, 0) + 1
    return {
        "repo": repo,
        "scope": {"kind": "repo", "repo": repo},
        "count_contract": f"snapshot_scope counts are scoped to repo {repo}.",
        "entity_count": entity_count,
        "module_count": module_count,
        "fact_count": fact_count,
        "evidence_count": evidence_count,
        "coverage_count": len(coverage_rows),
        "coverage_states": dict(sorted(coverage_states.items())),
        "coverage_predicates": _top_count_map(coverage_predicates, limit=10),
    }


def _planning_context_entity_in_repo_scope(entity: JsonObject, repo_key: str) -> bool:
    if _repo_text_matches(_planning_context_entity_repo(entity), repo_key):
        return True
    # The Repo entity itself belongs to its own repo scope; its identity carries the repo
    # name rather than a repo field, so it would otherwise be excluded (off-by-one).
    if entity.get("kind") == "Repo":
        name = entity.get("identity", {}).get("name")
        return _repo_text_matches(name, repo_key)
    return False


def _planning_context_evidence_in_repo_scope(
    row: JsonObject, repo_key: str, *, facts_by_id: dict[object, JsonObject], kg: KgSnapshot
) -> bool:
    target_id = row.get("target_id")
    target_type = row.get("target_type")
    if target_type == "entity":
        entity = kg.entities_by_id.get(target_id)
        return bool(entity) and _planning_context_entity_in_repo_scope(entity, repo_key)
    if target_type == "fact":
        fact = facts_by_id.get(target_id)
        if not fact:
            return False
        subject = kg.entities_by_id.get(fact.get("subject_id"))
        object_ = kg.entities_by_id.get(fact.get("object_id"))
        if (bool(subject) and _planning_context_entity_in_repo_scope(subject, repo_key)) or (
            bool(object_) and _planning_context_entity_in_repo_scope(object_, repo_key)
        ):
            return True
        # Mirror fact_count's cross-repo consumer scoping so a fact counted in-repo via its
        # consumer_repo qualifier also has its evidence counted.
        qualifier = fact.get("qualifier", {})
        return isinstance(qualifier, dict) and _repo_text_matches(qualifier.get("consumer_repo"), repo_key)
    return False


def _planning_context_has_indexed_scope(snapshot_scope: JsonObject) -> bool:
    return any(
        isinstance(snapshot_scope.get(field), int) and int(snapshot_scope[field]) > 0
        for field in ("entity_count", "fact_count", "coverage_count")
    )


def _planning_context_coverage_row_matches_repo(row: JsonObject, repo_key: str) -> bool:
    scope_ref = row.get("scope_ref")
    if not isinstance(scope_ref, dict):
        return False
    return _repo_text_matches(scope_ref.get("repo"), repo_key)


def _top_count_map(raw_counts: object, *, limit: int) -> JsonObject:
    if not isinstance(raw_counts, dict):
        return {}
    rows = []
    for key, value in raw_counts.items():
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        rows.append((str(key), value))
    return dict(sorted(rows, key=lambda item: (-item[1], item[0]))[:limit])


def _planning_context_entry_points(groups: dict[str, list[JsonObject]]) -> list[JsonObject]:
    rows: list[JsonObject] = []
    for kind in (
        "services",
        "symbols",
        "endpoints",
        "endpoint_consumers",
        "event_channels",
        "candidate_or_unlinked_event_channels",
        "dependencies",
        "domains",
    ):
        for row in groups[kind]:
            entry = {key: value for key, value in row.items() if key != "evidence"}
            entry["section"] = kind
            rows.append(entry)
            if len(rows) >= PLANNING_CONTEXT_SECTION_LIMIT:
                return rows
    return rows


def _planning_context_related_facts(
    *,
    kg: KgSnapshot,
    services: list[JsonObject],
    symbols: list[JsonObject],
    dependencies: list[JsonObject],
    endpoints: list[JsonObject],
    endpoint_consumers: list[JsonObject],
    event_channels: list[JsonObject],
    candidate_or_unlinked_event_channels: list[JsonObject],
    domains: list[JsonObject],
    anchors: dict[str, str | None],
    status: str,
    dependency_importers: JsonObject,
    inventory: JsonObject,
    service_operational_surfaces: JsonObject,
    runtime_architecture: JsonObject | None,
    authz_surface: JsonObject | None,
    line: int | None = None,
) -> JsonObject:
    related = {
        "service_brief": _planning_context_service_brief(
            services,
            endpoints,
            endpoint_consumers,
            event_channels,
            candidate_or_unlinked_event_channels,
            domains,
        ),
        "symbol_impact": _planning_context_symbol_impact(kg, symbols, anchors=anchors, status=status, line=line),
        "dependency_importers": dependency_importers,
        "inventory": inventory,
        "service_operational_surfaces": service_operational_surfaces,
        "dependencies": dependencies[:PLANNING_CONTEXT_SECTION_LIMIT],
        "endpoints": endpoints[:PLANNING_CONTEXT_SECTION_LIMIT],
        "endpoint_consumers": endpoint_consumers[:PLANNING_CONTEXT_SECTION_LIMIT],
        "event_channels": event_channels[:PLANNING_CONTEXT_SECTION_LIMIT],
        "candidate_or_unlinked_event_channels": candidate_or_unlinked_event_channels[
            :PLANNING_CONTEXT_SECTION_LIMIT
        ],
        "deploy_mappings": [
            row
            for row in domains
            if row.get("predicate") == "ROUTES_DOMAIN_TO_DEPLOY" or _is_known_deploy_row(row)
        ][:PLANNING_CONTEXT_SECTION_LIMIT],
        "domains": domains[:PLANNING_CONTEXT_SECTION_LIMIT],
    }
    if runtime_architecture is not None:
        related["runtime_architecture"] = _planning_context_runtime_architecture_reference(runtime_architecture)
    if authz_surface is not None:
        related["authz_surface"] = _planning_context_authz_surface_reference(authz_surface)
    return related


def _planning_context_authz_surface_reference(authz_surface: JsonObject) -> JsonObject:
    reference: JsonObject = {
        "status": authz_surface.get("status"),
        "scope": authz_surface.get("scope", {}),
        "summary": authz_surface.get("summary", {}),
        "answerability": authz_surface.get("answerability", {}),
        "assembly_contract": authz_surface.get("assembly_contract"),
    }
    for key in AUTHZ_COMPACT_LIST_KEYS:
        rows = _json_object_list(authz_surface.get(key))
        if key == "review_leads":
            reference[key] = [
                _planning_context_authz_lead_reference(row)
                for row in rows[:PLANNING_CONTEXT_SECTION_LIMIT]
            ]
        elif key in {"endpoint_authorization", "missing_or_unknown"}:
            reference[key] = [
                _planning_context_authz_endpoint_reference(row)
                for row in rows[:PLANNING_CONTEXT_SECTION_LIMIT]
            ]
        elif key in {"applied_policies", "in_method_checks", "declared_policies"}:
            reference[key] = [
                _planning_context_authz_fact_reference(row)
                for row in rows[:PLANNING_CONTEXT_SECTION_LIMIT]
            ]
        elif key == "inspection_areas":
            reference[key] = [
                _planning_context_authz_inspection_area_reference(row)
                for row in rows[:PLANNING_CONTEXT_SECTION_LIMIT]
            ]
        else:
            reference[key] = rows[:PLANNING_CONTEXT_SECTION_LIMIT]
    return reference


def _planning_context_authz_inspection_area_reference(row: JsonObject) -> JsonObject:
    area = dict(row)
    refs = area.get("inspection_refs")
    if isinstance(refs, list):
        area["inspection_refs"] = refs[:COMPACT_AUTHZ_INSPECTION_REF_LIMIT]
        omitted = len(refs) - len(area["inspection_refs"])
        if omitted > 0:
            existing_omitted = area.get("omitted_inspection_ref_count")
            if isinstance(existing_omitted, bool) or not isinstance(existing_omitted, int):
                existing_omitted = 0
            area["omitted_inspection_ref_count"] = existing_omitted + omitted
            area["inspection_refs_truncated"] = True
    return area


def _planning_context_authz_lead_reference(row: JsonObject) -> JsonObject:
    policies = _json_object_list(row.get("policies"))
    checks = _json_object_list(row.get("checks"))
    return {
        "lead_type": row.get("lead_type"),
        "priority": row.get("priority"),
        "reason": row.get("reason"),
        "endpoint": _planning_context_authz_endpoint_identity(row.get("endpoint"), row.get("route")),
        "handler": _planning_context_authz_symbol_identity(row.get("handler")),
        "authz_status": row.get("authz_status"),
        "public_policy_present": row.get("public_policy_present", False),
        "policy_names": _planning_context_authz_qualifier_names(policies, "policy"),
        "check_names": _planning_context_authz_check_names(checks),
        "source_coordinates": _planning_context_authz_coordinates(row, policies, checks),
        "recommended_source_checks": row.get("recommended_source_checks", []),
    }


def _planning_context_authz_endpoint_reference(row: JsonObject) -> JsonObject:
    policies = _json_object_list(row.get("policies"))
    checks = _json_object_list(row.get("checks"))
    return {
        "endpoint": _planning_context_authz_endpoint_identity(row.get("endpoint"), row.get("route")),
        "handler": _planning_context_authz_symbol_identity(row.get("handler")),
        "authz_status": row.get("authz_status"),
        "public_policy_present": row.get("public_policy_present", False),
        "policy_count": len(policies),
        "check_count": len(checks),
        "policy_names": _planning_context_authz_qualifier_names(policies, "policy"),
        "check_names": _planning_context_authz_check_names(checks),
        "source_coordinates": _planning_context_authz_coordinates(row, policies, checks),
    }


def _planning_context_authz_fact_reference(row: JsonObject) -> JsonObject:
    return {
        "predicate": row.get("predicate"),
        "subject": _planning_context_authz_symbol_identity(row.get("subject")),
        "object": _planning_context_authz_symbol_identity(row.get("object")),
        "qualifier": _planning_context_authz_qualifier(row.get("qualifier")),
        "source_coordinates": _planning_context_authz_coordinates(row),
    }


def _planning_context_authz_endpoint_identity(endpoint: object, route: object) -> JsonObject:
    endpoint_row = endpoint if isinstance(endpoint, dict) else {}
    route_row = route if isinstance(route, dict) else {}
    path = endpoint_row.get("path")
    if not isinstance(path, str) or not path:
        path = route_row.get("path")
    return _drop_none(
        {
            "repo": endpoint_row.get("repo"),
            "method": route_row.get("method") or endpoint_row.get("method"),
            "path": path,
            "framework": route_row.get("framework"),
            "source_kind": route_row.get("source_kind"),
        }
    )


def _planning_context_authz_symbol_identity(value: object) -> JsonObject:
    if not isinstance(value, dict):
        return {}
    properties = value.get("properties")
    if not isinstance(properties, dict):
        properties = {}
    return _drop_none(
        {
            "repo": value.get("repo"),
            "path": value.get("path") or properties.get("path"),
            "line": value.get("line") or properties.get("line") or properties.get("start_line"),
            "module": value.get("module"),
            "qualname": value.get("qualname"),
            "symbol_kind": value.get("symbol_kind"),
            "name": value.get("name"),
            "kind": value.get("kind"),
        }
    )


def _planning_context_authz_coordinates(row: JsonObject, *related_groups: list[JsonObject]) -> list[JsonObject]:
    coordinates = list(_planning_context_row_source_coordinates(row))
    for ref in (
        _planning_context_coordinate_from_authz_symbol(row.get("handler")),
        _planning_context_coordinate_from_authz_symbol(row.get("subject")),
        _planning_context_coordinate_from_authz_symbol(row.get("object")),
    ):
        if ref is not None:
            coordinates.append(ref)
    for group in related_groups:
        for related in group:
            coordinates.extend(_planning_context_row_source_coordinates(related))
            for ref in (
                _planning_context_coordinate_from_authz_symbol(related.get("subject")),
                _planning_context_coordinate_from_authz_symbol(related.get("object")),
            ):
                if ref is not None:
                    coordinates.append(ref)
    deduped = []
    seen = set()
    for coordinate in coordinates:
        key = _coordinate_location_key(coordinate)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(coordinate)
        if len(deduped) >= 4:
            return deduped
    return deduped


def _planning_context_coordinate_from_authz_symbol(value: object) -> JsonObject | None:
    if not isinstance(value, dict):
        return None
    properties = value.get("properties")
    if not isinstance(properties, dict):
        properties = {}
    path = value.get("path") or properties.get("path")
    line = value.get("line") or properties.get("line") or properties.get("start_line")
    if not isinstance(path, str) or not path.strip():
        return None
    if isinstance(line, bool) or not isinstance(line, int) or line < 1:
        return None
    return {
        "repo": value.get("repo"),
        "provenance": "authz_symbol",
        "path": path,
        "line_start": line,
        "line_end": line,
    }


def _planning_context_authz_qualifier(value: object) -> JsonObject:
    if not isinstance(value, dict):
        return {}
    keys = ("access_level", "framework", "policy", "check", "source_kind", "guard_intent", "method", "path")
    return {key: value[key] for key in keys if key in value}


def _planning_context_authz_qualifier_names(rows: list[JsonObject], key: str) -> list[str]:
    names = []
    for row in rows:
        qualifier = row.get("qualifier")
        if isinstance(qualifier, dict) and isinstance(qualifier.get(key), str):
            names.append(qualifier[key])
    return sorted(set(names))


def _planning_context_authz_check_names(rows: list[JsonObject]) -> list[str]:
    names = []
    for key in ("check", "guard", "policy"):
        names.extend(_planning_context_authz_qualifier_names(rows, key))
    return sorted(set(names))


def _json_object_list(value: object) -> list[JsonObject]:
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, dict)]


def _drop_none(row: JsonObject) -> JsonObject:
    return {key: value for key, value in row.items() if value not in (None, [], {})}


def _planning_context_runtime_architecture_reference(runtime_architecture: JsonObject) -> JsonObject:
    answer_packet = runtime_architecture.get("answer_packet")
    if not isinstance(answer_packet, dict):
        answer_packet = {}
    reference = {
        "summary": runtime_architecture.get("summary", {}),
        "scope": runtime_architecture.get("scope", {}),
        "deploy_kind_counts": answer_packet.get("deploy_kind_counts", {}),
        "missing_fact_families": answer_packet.get("missing_fact_families", []),
        "evidence_contract": answer_packet.get("evidence_contract"),
        "read_top_level_field": "runtime_architecture.answer_packet",
        "read_for_deploy_runtime": "runtime_architecture.answer_packet.deploy_runtime_map",
        "read_for_candidate_or_unresolved_deploy": "runtime_architecture.answer_packet.unlinked_deploy_leads",
        "read_for_endpoint_consumers": "runtime_architecture.answer_packet.endpoint_consumer_map",
        "read_for_deploy_order": "runtime_architecture.answer_packet.deploy_order_guidance",
    }
    # Only surface the anchor-resolution contract when an actual gate is present (ambiguous/
    # unresolved anchor); on a resolved packet an empty {} reads as a false active gate.
    anchor_resolution_contract = runtime_architecture.get("anchor_resolution_contract")
    if isinstance(anchor_resolution_contract, dict) and anchor_resolution_contract:
        reference["anchor_resolution_contract"] = anchor_resolution_contract
    return reference


def _planning_context_service_brief(
    services: list[JsonObject],
    endpoints: list[JsonObject],
    endpoint_consumers: list[JsonObject],
    event_channels: list[JsonObject],
    candidate_or_unlinked_event_channels: list[JsonObject],
    domains: list[JsonObject],
) -> JsonObject:
    deploy_mappings = [
        row
        for row in domains
        if row.get("predicate") == "ROUTES_DOMAIN_TO_DEPLOY" or _is_known_deploy_row(row)
    ]
    return {
        "services": services[:PLANNING_CONTEXT_SECTION_LIMIT],
        "summary": {
            "service_count": len(services),
            "endpoint_fact_count": len(endpoints),
            "endpoint_consumer_fact_count": len(endpoint_consumers),
            "event_fact_count": len(event_channels),
            "candidate_or_unlinked_event_fact_count": len(candidate_or_unlinked_event_channels),
            "deploy_mapping_count": len(deploy_mappings),
        },
        "endpoints": endpoints[:PLANNING_CONTEXT_SECTION_LIMIT],
        "endpoint_consumers": endpoint_consumers[:PLANNING_CONTEXT_SECTION_LIMIT],
        "event_channels": event_channels[:PLANNING_CONTEXT_SECTION_LIMIT],
        "candidate_or_unlinked_event_channels": candidate_or_unlinked_event_channels[
            :PLANNING_CONTEXT_SECTION_LIMIT
        ],
        "deploy_mappings": deploy_mappings[:PLANNING_CONTEXT_SECTION_LIMIT],
    }


def _planning_context_symbol_impact(
    kg: KgSnapshot,
    symbols: list[JsonObject],
    *,
    anchors: dict[str, str | None],
    status: str,
    line: int | None = None,
) -> JsonObject:
    if status == "ambiguous" and anchors.get("symbol"):
        reverse_impact = kg.reverse_impact(
            anchors["symbol"] or "",
            path=anchors.get("path"),
            line=line if anchors.get("path") else None,
            depth=3,
            limit=max(PLANNING_CONTEXT_SECTION_LIMIT * 3, 15),
        )
        return {
            "status": "ambiguous",
            "reverse_impact": reverse_impact,
        }
    if status != "found" or not anchors.get("symbol") or len(symbols) != 1:
        return {"status": "not_computed", "reason": "symbol impact requires one resolved symbol anchor"}
    symbol_name = symbols[0].get("qualified_name") or symbols[0].get("qualname")
    if not isinstance(symbol_name, str) or not symbol_name:
        return {"status": "not_computed", "reason": "resolved symbol missing qualified name"}
    callers = kg.find_callers(
        symbol_name,
        path=_optional_symbol_path(symbols[0]),
        line=_optional_symbol_line(symbols[0]),
        limit=PLANNING_CONTEXT_SECTION_LIMIT,
    )
    reverse_impact = kg.reverse_impact(
        symbol_name,
        path=_optional_symbol_path(symbols[0]),
        line=_optional_symbol_line(symbols[0]),
        depth=3,
        limit=max(PLANNING_CONTEXT_SECTION_LIMIT * 3, 15),
    )
    return {
        "status": "found",
        "symbol": symbols[0],
        "direct_callers": list(callers.get("callers", [])),
        "reverse_impact": reverse_impact,
        "import_consumer_leads": callers.get("import_consumer_leads", {}),
        "direct_callees": list(
            kg.find_callees(
                symbol_name,
                path=_optional_symbol_path(symbols[0]),
                line=_optional_symbol_line(symbols[0]),
                limit=PLANNING_CONTEXT_SECTION_LIMIT,
            ).get("callees", [])
        ),
    }


def _planning_context_answerability(
    *,
    status: str,
    anchors: dict[str, str | None],
    groups: dict[str, list[JsonObject]],
    snapshot_scope: JsonObject | None = None,
) -> JsonObject:
    if status == "ambiguous":
        return {
            "status": "not_answerable",
            "missing_fact_families": ["unambiguous_primary_anchor"],
            "by_claim_family": {
                "primary_anchor_resolution": "not_answerable",
                "runtime_architecture": "not_answerable",
            },
            "recommended_followups": ["Refine the query with a structured anchor or source coordinate."],
        }
    if status == "not_found":
        if snapshot_scope is not None and _planning_context_has_indexed_scope(snapshot_scope):
            return {
                "status": "partial",
                "missing_fact_families": ["primary_anchor_behavioral_rows"],
                "answerable_fact_families": ["snapshot_inventory"],
                "by_claim_family": {
                    "inventory_summary": "answerable",
                    "behavioral_claims": "not_answerable",
                    "runtime_architecture": "not_answerable",
                },
                "recommended_followups": [
                    "Use snapshot_scope and snapshot_summary only for inventory counts; inspect source or retry with narrower anchors for behavioral claims."
                ],
            }
        return {
            "status": "not_answerable",
            "missing_fact_families": ["primary_anchor"],
            "by_claim_family": {
                "primary_anchor_resolution": "not_answerable",
            },
            "recommended_followups": ["Broaden or correct the supplied planning anchor."],
        }
    missing = _planning_context_missing_fact_families(anchors, groups)
    return {
        "status": "partial" if missing else "answerable",
        "missing_fact_families": missing,
        "by_claim_family": {
            "primary_anchor_resolution": "answerable",
            "matched_rows": "partial" if missing else "answerable",
        },
        "recommended_followups": _planning_context_answerability_followups(missing),
    }


def _planning_context_missing_fact_families(
    anchors: dict[str, str | None],
    groups: dict[str, list[JsonObject]],
) -> list[str]:
    missing = []
    if anchors.get("service") and not groups["services"]:
        missing.append("service_identity")
    if (anchors.get("symbol") or anchors.get("path")) and not groups["symbols"]:
        missing.append("symbol_identity")
    if (anchors.get("repo") or anchors.get("package")) and not groups["dependencies"]:
        missing.append("dependency_edges")
    if anchors.get("endpoint") and not groups["endpoints"]:
        missing.append("endpoint_facts")
    if anchors.get("event_channel") and not groups["event_channels"]:
        missing.append("event_facts")
    if anchors.get("domain") and not groups["domains"]:
        missing.append("domain_facts")
    return missing


def _planning_context_answerability_followups(missing: list[str]) -> list[str]:
    actions = []
    if "service_identity" in missing:
        actions.append("Use `search_services` to find candidate service slugs or retry with the exact service repo/name.")
    if "symbol_identity" in missing:
        actions.append("Retry with an exact qualified symbol name, or add `path` and `line` to disambiguate the source anchor.")
    if "dependency_edges" in missing:
        actions.append("Use `repo` or `package` alone to inspect dependency edges, or fall back to source inspection.")
    if "endpoint_facts" in missing:
        actions.append("Use endpoint-specific tools or source inspection to verify route ownership.")
    if "event_facts" in missing:
        actions.append("Use event producer/consumer tools or source inspection to verify event flow.")
    if "domain_facts" in missing:
        actions.append("Use domain lookup or source inspection to verify runtime host references.")
    return actions


def _planning_context_source_coordinates(*groups: list[JsonObject], limit: int) -> list[JsonObject]:
    coordinates: list[JsonObject] = []
    seen: set[tuple[object, object, object, object, object]] = set()
    for group in groups:
        for row in group:
            for coordinate in _planning_context_row_source_coordinates(row):
                key = (
                    coordinate.get("repo"),
                    coordinate.get("commit_sha"),
                    coordinate.get("path"),
                    coordinate.get("line_start"),
                    coordinate.get("line_end"),
                )
                if key in seen:
                    continue
                seen.add(key)
                coordinates.append(coordinate)
                if len(coordinates) >= limit:
                    return coordinates
    return coordinates


def _planning_context_row_source_coordinates(row: JsonObject) -> list[JsonObject]:
    coordinates: list[JsonObject] = []
    for evidence in row.get("evidence", []):
        if not isinstance(evidence, dict):
            continue
        bytes_ref = evidence.get("bytes_ref")
        if not isinstance(bytes_ref, dict):
            continue
        coordinate = _planning_context_coordinate_from_bytes_ref(bytes_ref, evidence=evidence)
        if coordinate is not None:
            coordinates.append(coordinate)
    coordinate = _planning_context_coordinate_from_row(row)
    if coordinate is not None:
        geometry_key = _coordinate_location_key(coordinate)
        if any(_coordinate_location_key(existing) == geometry_key for existing in coordinates):
            return coordinates
        coordinates.append(coordinate)
    return coordinates


def _coordinate_location_key(coordinate: JsonObject) -> tuple[object, object, object, object]:
    return (
        coordinate.get("repo"),
        coordinate.get("path"),
        coordinate.get("line_start"),
        coordinate.get("line_end"),
    )


def _planning_context_coordinate_from_bytes_ref(bytes_ref: JsonObject, *, evidence: JsonObject) -> JsonObject | None:
    path = bytes_ref.get("path")
    line_start = bytes_ref.get("line_start")
    if not isinstance(path, str) or not path.strip():
        return None
    if isinstance(line_start, bool) or not isinstance(line_start, int) or line_start < 1:
        return None
    line_end = bytes_ref.get("line_end")
    if isinstance(line_end, bool) or not isinstance(line_end, int) or line_end < line_start:
        line_end = line_start
    source_ref = evidence.get("source_ref") if isinstance(evidence.get("source_ref"), dict) else {}
    return {
        "repo": bytes_ref.get("repo") or source_ref.get("repo"),
        "commit_sha": bytes_ref.get("commit_sha") or source_ref.get("commit_sha"),
        "provenance": "bytes_ref",
        "path": path,
        "line_start": line_start,
        "line_end": line_end,
    }


def _planning_context_coordinate_from_row(row: JsonObject) -> JsonObject | None:
    path = row.get("path")
    line = row.get("line")
    if not isinstance(path, str) or not path.strip():
        return None
    if isinstance(line, bool) or not isinstance(line, int) or line < 1:
        return None
    end_line = row.get("end_line")
    if isinstance(end_line, bool) or not isinstance(end_line, int) or end_line < line:
        end_line = line
    return {
        "repo": row.get("repo"),
        "commit_sha": row.get("commit_sha"),
        "provenance": "row_geometry",
        "path": path,
        "line_start": line,
        "line_end": end_line,
    }


def _planning_context_evidence(*groups: list[JsonObject]) -> list[JsonObject]:
    evidence: list[JsonObject] = []
    seen: set[tuple[str, str]] = set()
    for group in groups:
        for row in group:
            for item in row.get("evidence", []):
                if not isinstance(item, dict):
                    continue
                key = (str(item.get("target_type", "")), str(item.get("target_id", "")))
                if key in seen:
                    continue
                seen.add(key)
                evidence.append(item)
                if len(evidence) >= 5:
                    return evidence
    return evidence


def _line_matches_symbol(line: int, symbol: JsonObject) -> bool:
    start = symbol.get("line")
    end = symbol.get("end_line")
    if not isinstance(start, int):
        return False
    line_end = end if isinstance(end, int) else start
    return start <= line <= line_end


def _planning_context_public_rows(rows: list[JsonObject]) -> list[JsonObject]:
    public_rows: list[JsonObject] = []
    for row in rows:
        public_rows.append({key: value for key, value in row.items() if not key.startswith("_")})
    return public_rows


def _planning_context_fact_result(kg: KgSnapshot, fact: JsonObject, subject: JsonObject, object_: JsonObject) -> JsonObject:
    return {
        **_fact_result(kg, fact, subject, object_),
        "_fact": fact,
        "_subject": subject,
        "_object": object_,
    }


def _planning_context_repo_matches(kg: KgSnapshot, repo: str) -> dict[str, list[JsonObject]]:
    matches: dict[str, list[JsonObject]] = {}
    for fact in kg.facts:
        if fact.get("predicate") != "RESOLVES_TO_REPO":
            continue
        qualifier = fact.get("qualifier", {})
        if not _repo_text_matches(qualifier.get("consumer_repo"), repo):
            continue
        package = kg.entities_by_id.get(fact["subject_id"])
        target_repo = kg.entities_by_id.get(fact["object_id"])
        if not package or not target_repo:
            continue
        matches.setdefault(fact["subject_id"], []).append(
            {
                **_planning_context_fact_result(kg, fact, package, target_repo),
            }
        )
    return matches


def _planning_context_package_matches(kg: KgSnapshot, package_name: str) -> dict[str, list[JsonObject]]:
    matches: dict[str, list[JsonObject]] = {}
    for fact in kg.facts:
        if fact.get("predicate") != "IMPORTS":
            continue
        package = kg.entities_by_id.get(fact["object_id"])
        module = kg.entities_by_id.get(fact["subject_id"])
        if not package or not module:
            continue
        if not kg.import_matches(fact, package, package_name):
            continue
        matches.setdefault(fact["object_id"], []).append(
            {
                **_planning_context_fact_result(kg, fact, module, package),
            }
        )
    return matches


def _planning_context_endpoint_matches(kg: KgSnapshot, path_query: str) -> dict[str, list[JsonObject]]:
    matches: dict[str, list[JsonObject]] = {}
    for fact in kg.facts:
        if fact.get("predicate") not in {"EXPOSES_ENDPOINT", "CALLS_ENDPOINT", "DOCUMENTS_ENDPOINT"}:
            continue
        subject = kg.entities_by_id.get(fact["subject_id"])
        endpoint = kg.entities_by_id.get(fact["object_id"])
        if not subject or not endpoint or endpoint.get("kind") != "Endpoint":
            continue
        path = str(endpoint.get("identity", {}).get("path", ""))
        if not _planning_context_endpoint_path_matches(path, path_query):
            continue
        matches.setdefault(fact["object_id"], []).append(
            {
                **_planning_context_fact_result(kg, fact, subject, endpoint),
            }
        )
    return matches


def _planning_context_event_matches(
    kg: KgSnapshot, query: str, *, known_only: bool = True
) -> dict[str, list[JsonObject]]:
    matches: dict[str, list[JsonObject]] = {}
    needle = query.lower()
    for fact in kg.facts:
        if not _is_event_predicate(fact.get("predicate")):
            continue
        subject = kg.entities_by_id.get(fact["subject_id"])
        channel = kg.entities_by_id.get(fact["object_id"])
        if not subject or not channel or channel.get("kind") != "EventChannel":
            continue
        is_known = _is_known_event_fact(fact, channel)
        if known_only and not is_known:
            continue
        if not known_only and is_known:
            continue
        identity = channel.get("identity", {})
        name = str(identity.get("channel_address") or identity.get("name") or "")
        if needle not in name.lower():
            continue
        matches.setdefault(fact["object_id"], []).append(
            {
                **_planning_context_fact_result(kg, fact, subject, channel),
            }
        )
    return matches


def _planning_context_domain_matches(kg: KgSnapshot, query: str) -> dict[str, list[JsonObject]]:
    matches: dict[str, list[JsonObject]] = {}
    needle = query.lower()
    for fact in kg.facts:
        if fact.get("predicate") not in {"REFERENCES_DOMAIN", "ROUTES_DOMAIN_TO_DEPLOY"}:
            continue
        subject = kg.entities_by_id.get(fact["subject_id"])
        domain = kg.entities_by_id.get(fact["object_id"])
        if not subject or not domain or domain.get("kind") != "Domain":
            continue
        domain_name = str(domain.get("identity", {}).get("name", ""))
        if needle not in domain_name.lower():
            continue
        matches.setdefault(fact["object_id"], []).append(
            {
                **_planning_context_fact_result(kg, fact, subject, domain),
            }
        )
    return matches


def _planning_context_collect_rows(rows_by_id: dict[str, list[JsonObject]], *, limit: int) -> list[JsonObject]:
    rows: list[JsonObject] = []
    for match_id in sorted(rows_by_id):
        rows.extend(rows_by_id[match_id])
    return rows[:limit]


def _planning_context_dedupe_rows(rows: list[JsonObject]) -> list[JsonObject]:
    deduped: list[JsonObject] = []
    seen: set[str] = set()
    for row in rows:
        key = str(row.get("fact_id") or row.get("service_id") or row.get("symbol_id") or row)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def _planning_context_exact_endpoint_rows(kg: KgSnapshot, query: str, limit: int) -> list[JsonObject]:
    needle = _normalize_endpoint_query(query)
    rows: list[JsonObject] = []
    for fact in kg.facts:
        if fact.get("predicate") not in {"EXPOSES_ENDPOINT", "CALLS_ENDPOINT", "DOCUMENTS_ENDPOINT"}:
            continue
        subject = kg.entities_by_id.get(fact["subject_id"])
        endpoint = kg.entities_by_id.get(fact["object_id"])
        if not subject or not endpoint or endpoint.get("kind") != "Endpoint":
            continue
        if _normalize_endpoint_query(str(endpoint.get("identity", {}).get("path", ""))) != needle:
            continue
        rows.append(_fact_result(kg, fact, subject, endpoint))
        if len(rows) >= limit:
            break
    return rows


def _planning_context_exact_event_rows(kg: KgSnapshot, query: str, limit: int) -> list[JsonObject]:
    needle = query.strip().lower()
    rows: list[JsonObject] = []
    for fact in kg.facts:
        if not _is_event_predicate(fact.get("predicate")):
            continue
        subject = kg.entities_by_id.get(fact["subject_id"])
        channel = kg.entities_by_id.get(fact["object_id"])
        if not subject or not channel or channel.get("kind") != "EventChannel":
            continue
        if not _is_known_event_fact(fact, channel):
            continue
        identity = channel.get("identity", {})
        candidates = {
            str(identity.get("channel_address") or "").lower(),
            str(identity.get("name") or "").lower(),
            _event_channel_search_text(channel).lower(),
        }
        if needle not in candidates:
            continue
        rows.append(_fact_result(kg, fact, subject, channel))
        if len(rows) >= limit:
            break
    return rows


def _planning_context_exact_domain_rows(kg: KgSnapshot, query: str, limit: int) -> list[JsonObject]:
    needle = query.strip().lower()
    rows: list[JsonObject] = []
    for fact in kg.facts:
        if fact.get("predicate") not in {"REFERENCES_DOMAIN", "ROUTES_DOMAIN_TO_DEPLOY"}:
            continue
        subject = kg.entities_by_id.get(fact["subject_id"])
        domain = kg.entities_by_id.get(fact["object_id"])
        if not subject or not domain or domain.get("kind") != "Domain":
            continue
        if str(domain.get("identity", {}).get("name", "")).strip().lower() != needle:
            continue
        rows.append(_fact_result(kg, fact, subject, domain))
        if len(rows) >= limit:
            break
    return rows


def _normalize_endpoint_query(value: str) -> str:
    return normalize_endpoint_path_shape(value)


def _planning_context_endpoint_path_matches(path: str, endpoint: str) -> bool:
    raw_needle = endpoint.strip().lower()
    raw_haystack = path.strip().lower()
    if raw_needle and raw_needle in raw_haystack:
        return True
    shape_needle = normalize_endpoint_path_shape(endpoint).lower()
    shape_haystack = normalize_endpoint_path_shape(path).lower()
    return bool(shape_needle and shape_needle in shape_haystack)


def _symbol_refinement_actions(symbols: list[JsonObject]) -> list[str]:
    actions = [
        "Provide `symbol` with an exact qualified name, or add `path` and `line` to disambiguate the code anchor.",
    ]
    if symbols:
        actions.append(f"Candidate symbol: {symbols[0].get('qualified_name') or symbols[0].get('display_name')}.")
    return actions


def _service_refinement_actions(services: list[JsonObject]) -> list[str]:
    actions = ["Provide `service` with an exact slug or repo name."]
    if services:
        actions.append(f"Candidate service: {services[0].get('slug') or services[0].get('name')}.")
    return actions


def _service_exact_match(query: str, service: JsonObject) -> bool:
    needle = query.strip().lower()
    identity = service.get("identity", {})
    return needle in {
        str(display_entity(service)).lower(),
        str(identity.get("slug") or "").lower(),
        str(identity.get("repo") or "").lower(),
        str(identity.get("namespace") or "").lower(),
    }


_TOOLS: dict[str, McpTool] = {
    "search_services": McpTool(
        name="search_services",
        description=(
            "Searches indexed Service entities by name, slug, namespace, repo, or stored properties. "
            "Use it when you need candidate services before drilling into a specific service brief or dependency path, and as the primary tool for repo/service identity questions (what service is this, its name/slug/namespace/owning repo). "
            "Treat packaging metadata such as a pyproject.toml package name as a candidate naming lead, not proof of the service identity; prefer the matched Service entity identity and repo link. "
            "Does not return endpoint topology, caller graphs, deploy blockers, or runtime health."
        ),
        input_schema=_object_schema({"query": _nullable_string_schema("Optional service search text."), "limit": _limit_schema()}),
        handler=_search_services,
    ),
    "get_service_brief": McpTool(
        name="get_service_brief",
        description=(
            "Returns a compact service brief plus related endpoint, path-shape-matched endpoint-consumer, event-channel, deploy-mapping, and operational domain/deploy-target candidate facts for one matched service. "
            "Use it after you know the target service and want a bounded operational summary of what the KG has linked to it. "
            "endpoint_consumers are static CALLS_ENDPOINT candidates matched by canonical endpoint path shape and compatible method; verify unresolved hosts/env before runtime or deploy claims. "
            "Read operational_surfaces.evidence_partition: known_linked uses exact repo-identity joins, unlinked_evidence is source leads only, and missing_contracts lists deploy/runtime claims the KG cannot prove. "
            "Treat operational_surfaces.deploy_link_facts / known DEPLOYS_VIA_CONFIG as service-to-deploy-target evidence; treat candidate_or_unlinked_deploy_links and unlinked domain routes as source leads, not deploy proof. "
            "Does not traverse caller graphs, compute downstream blast radius, or infer missing runtime/deploy contracts; if deploy mappings are absent, inspect manifests before making environment claims."
        ),
        input_schema=_object_schema(
            {"service": _string_schema("Service name, slug, namespace, or repo."), "limit": _limit_schema()},
            required=["service"],
        ),
        handler=_get_service_brief,
    ),
    "find_callers": McpTool(
        name="find_callers",
        description=(
            "Returns static CALLS edges whose downstream target matches the requested symbol, with optional path and line disambiguation. "
            "Use it when you need immediate reverse callers for a known function, method, or symbol in the indexed codebase. "
            "If status is ambiguous, do not treat the empty callers list as no callers; retry with disambiguation.retry_arguments or a candidate qualified_name. "
            "If status is not_found but import_consumer_leads is found, inspect those importing modules and importer_module_symbols; they are source-inspection leads, not proven CALLS edges. "
            "Does not include transitive closure, runtime dispatch, cross-repo execution paths, endpoint/service-level rollups, or unresolved external-package call sites. "
            "A not_found result is not proof of absence; inspect source before finalizing."
        ),
        input_schema=_object_schema(_symbol_properties(), required=["symbol"]),
        handler=_find_callers,
    ),
    "reverse_impact": McpTool(
        name="reverse_impact",
        description=(
            "Returns a bounded reverse dependency head-start packet from a resolved symbol anchor. "
            "It walks incoming static CALLS recursively, groups affected symbols by depth, bridges Python __init__ methods to containing class instantiations, and includes terminal import_consumer_leads as source-inspection leads. "
            "Use this instead of chaining repeated find_callers calls when the task needs transitive upstream callers, caller-impact tiers, entry-point leads, or source_inspection_areas for a symbol anchor. "
            "If status is ambiguous, do not treat the empty edge list as no impact; use candidate_impact_previews and disambiguation.retry_arguments, or include_all=true only for exploratory aggregation. "
            "summary.affected_symbol_count counts only callable affected symbols; module/notebook/script call sites are reported separately in call_site_leads and must not be added to the affected symbol total. "
            "Large packets are bounded: when output_budget is present the detail rows were compacted to a coordinate-bearing head start, so inspect source coordinates or call a narrower anchor for omitted detail. "
            "Terminal import leads are not runtime-call proof; verify source before claiming endpoint or cross-repo execution."
        ),
        input_schema=_object_schema(
            {**_symbol_properties(), "depth": {"type": "integer", "minimum": 1, "maximum": 6, "default": 3}},
            required=["symbol"],
        ),
        handler=_reverse_impact,
    ),
    "find_callees": McpTool(
        name="find_callees",
        description=(
            "Returns static CALLS edges whose upstream subject matches the requested symbol, with optional path and line disambiguation. "
            "Use it when you want the immediate downstream call surface of a known symbol before expanding to blast radius. "
            "If status is ambiguous, do not treat the empty callees list as no callees; retry with disambiguation.retry_arguments or a candidate qualified_name. "
            "Does not return reverse callers, transitive closure, runtime-only invocations, service and endpoint boundaries, or unresolved external-package calls. "
            "A not_found result is not proof of absence; inspect source before finalizing."
        ),
        input_schema=_object_schema(_symbol_properties(), required=["symbol"]),
        handler=_find_callees,
    ),
    "get_event_consumers": McpTool(
        name="get_event_consumers",
        description=(
            "Returns facts whose subject consumes an event channel matching the provided queue, topic, ARN, or channel substring. "
            "Use it when you know the event channel and want the indexed static consumers attached to that channel. "
            "Does not infer delivery guarantees, runtime subscribers, message schemas, time-window usage, cross-environment broker state, or the client SDK/API call used to consume the channel; do not state the consuming mechanism unless source confirms it."
        ),
        input_schema=_object_schema(
            {"channel": _string_schema("Event channel name, queue, topic, or ARN substring."), "limit": _limit_schema()},
            required=["channel"],
        ),
        handler=_get_event_consumers,
    ),
    "get_event_producers": McpTool(
        name="get_event_producers",
        description=(
            "Returns facts whose subject produces an event channel matching the provided queue, topic, ARN, or channel substring. "
            "Use it when you know the event channel and need the indexed static producers that emit onto it. "
            "Does not prove messages were published at runtime or in a time window, identify consumers, recover schema and deployment guarantees, or reveal the client SDK/API call used to produce the channel; do not state the producing mechanism unless source confirms it."
        ),
        input_schema=_object_schema(
            {"channel": _string_schema("Event channel name, queue, topic, or ARN substring."), "limit": _limit_schema()},
            required=["channel"],
        ),
        handler=_get_event_producers,
    ),
    "blast_radius": McpTool(
        name="blast_radius",
        description=(
            "Returns downstream static CALLS closure from an anchor symbol up to `depth`. "
            "Use only when you know the exact edit-site symbol and want to enumerate intra-repo callees. "
            "If status is ambiguous, do not treat the empty edge list as no impact; retry with disambiguation.retry_arguments or a candidate qualified_name. "
            "Does not include reverse callers, cross-repo edges, service or endpoint boundaries, runtime calls, or unresolved external-package calls. "
            "A not_found result is not proof of absence; inspect source before finalizing."
        ),
        input_schema=_object_schema(
            {**_symbol_properties(), "depth": {"type": "integer", "minimum": 1, "maximum": 6, "default": 1}},
            required=["symbol"],
        ),
        handler=_blast_radius,
    ),
    "deploy_blockers_for": McpTool(
        name="deploy_blockers_for",
        description=(
            "Returns deploy-blocker information for a named service when the current KG implements that contract. "
            "Use it only when you need explicit deploy-blocker facts for a known service and can tolerate refusal when the graph lacks that relation. "
            "Does not infer blockers from callers, events, config drift, or undeclared operational dependencies; unsupported results require manifest/source inspection."
        ),
        input_schema=_object_schema({"service": _string_schema("Service name or slug."), "limit": _limit_schema()}, required=["service"]),
        handler=_deploy_blockers_for,
    ),
    "planning_context": McpTool(
        name="planning_context",
        description=(
            "Returns bounded planning context for a fleet or one structured anchor such as a symbol, service, repo, package, endpoint, event channel, or domain. "
            "Use it first for broad cross-repo service discovery, runtime architecture, domain-routing, dependency, planning, or impact-map questions before selecting narrower MCP tools or looping over search_services/get_service_brief. "
            "Includes additive grouped context: summary, snapshot_summary, snapshot_scope, inventory, entry_points, related_facts, source_coordinates with provenance, and answerability metadata. "
            "Use snapshot_summary.count_contract, snapshot_scope.count_contract, and inventory.count_contract to keep fleet-wide counts separate from repo-scoped counts. "
            "runtime_architecture assembles typed domain, deploy, endpoint, client, and event facts into runtime_building_blocks, domain_routing_map, deploy_runtime_map, unlinked_deploy_leads, endpoint_consumer_map, deploy_order_guidance, deploy_kind_counts split by component vs unlinked route leads, and an answer_packet without promoting unlinked evidence. "
            "runtime_architecture.summary.client_endpoint_call_count is path-shape-scoped candidate fact count; subtract or inspect endpoint_consumer_missing_method_drop_count before treating it as usable consumer evidence. "
            "When a planning anchor is ambiguous or not_found, runtime_architecture.summary.answer_packet_mode is investigation_brief_only and runtime maps/counts are omitted from the answer path until the agent retries with narrower anchors. "
            "For runtime architecture answers, include verified runtime_architecture.answer_packet.investigation_brief.unlinked_runtime_leads such as API Gateway hostnames, private IPs, and static-site CNAME domains as referenced runtime targets with a caveat, not as proven route mappings; treat runtime_architecture.answer_packet.unlinked_deploy_leads the same way for candidate or unresolved deploy evidence. "
            "For symbol impact anchors, read related_facts.symbol_impact.reverse_impact; it is a bounded reverse-caller head start with constructor bridges and terminal import leads for targeted source inspection. "
            "For ownership questions, read ownership_context.answer_packet; package authors and package maintainers are candidates only and must not be promoted to service owner unless an explicit ownership source is present. "
            "For security/authz questions, read top-level authz_surface.review_leads plus inspection_areas/inspection_index when present, related_facts.authz_surface as a compact reference, or get_service_brief.authz_surface; it separates endpoint handler bindings, applied policies, in-method checks, unsupported_scopes, and missing/unknown authz instead of treating missing policy as public access. "
            "For service anchors, includes bounded endpoint_consumers from structured endpoint path/method matches when available. "
            "For service operational evidence, read service_operational_surfaces.evidence_partition and keep known_linked, unlinked_evidence, and missing_contracts separate. "
            "Treat service_operational_surfaces.deploy_link_facts / known DEPLOYS_VIA_CONFIG and deploy_runtime_units as service-to-deploy-target evidence; candidate_or_unlinked_deploy_links and unlinked domain routes are source leads only. deploy_order_guidance is practical consumer-compatibility inference, not a canonical deploy-blocker fact. "
            "For dependency anchors, includes grouped importer evidence and does not attach fleet runtime_architecture or authz_surface. Runtime architecture is included only for fleet, unresolved raw-query investigation, repo, service, endpoint, event-channel, or domain anchors; authz_surface is included only for fleet, unresolved raw-query investigation, repo, service, or endpoint anchors. "
            "For inventory questions, includes top dependencies and coverage gap samples. "
            "Top-level result rows honor limit; nested planning packets are capped by summary.section_limit to stay compact. "
            "Calling it with no anchor returns a fleet packet with compact service identities plus runtime_architecture.answer_packet. "
            "Output is bounded with a compact fleet cap and a larger anchored-detail cap; when truncated, output_budget.omitted_counts, output_budget.backfilled_counts, output_budget.advice, and inspection_areas describe what was omitted and how to retrieve detail via narrower anchors. "
            "For exact caller, callee, service-brief, or event producer/consumer questions, prefer the exact primitive tool. "
            "Does not expand free-form natural language, call an LLM, or fan one query across multiple ambiguous resolver paths."
        ),
        input_schema=_object_schema(_planning_context_properties()),
        handler=_planning_context,
    ),
    "review_context": McpTool(
        name="review_context",
        description=(
            "Returns bounded review context for one repo plus a changed-file set by composing review_lead_status, review_leads, review_answer_packet, diff_anchors, changed_surface, changed_file_symbols, exact changed_symbols, direct callers/callees, transitive_callers, runtime_surfaces, framework_impact, application_impact, source_coordinates, and answerability metadata. "
            "Read review_lead_status first as the PR-review usage gate: coverage_status=useful means the compact packet has symbol-anchor, changed-symbol, or impact evidence, while low_coverage means fall back to direct source review and use review_leads/diff_anchors as coordinates only. "
            "Read review_answer_packet.top_diff_anchors / diff_anchors first as the PR changed-range/file anchors; detailed review rows are capped by summary.detail_limit even when a larger limit is requested. "
            "Read repo_resolution before interpreting missing anchors; single-repo checkout snapshots may safely resolve owner/repo arguments to a local snapshot repo identity when changed files overlap the snapshot, while ambiguous or no-overlap cases fail closed. "
            "review_answer_packet.top_changed_symbols contains range-overlap symbols only, while review_answer_packet.changed_file_symbol_inventory carries file inventory when no ranges are supplied. "
            "When changed_ranges are omitted, top-level changed_symbols and review_answer_packet.top_changed_symbols are empty and the changed-file symbol inventory is exposed via changed_file_symbols; that inventory is source-inspection context, not proof every symbol changed. Inspect the diff before saying a function was touched. "
            "When changed ranges produce only file anchors and no changed symbols or direct impact edges, review_answer_packet.packet_mode is diff_anchor_only and broad app/runtime/framework sections plus verbose contracts/evidence are omitted by default; pass include_unlinked_leads=true only when broad unlinked namespace/name leads are worth the extra context. "
            "When the prompt names impact categories, pass requested_surfaces such as ui_screens, scheduled_jobs, sqs_consumers, delivery_workers, tracking_paths, schemas, or contracts so surface_status can separate inventory_context, unlinked_lead, and missing evidence. "
            "Broad categories such as services and deployables are covered by other review packet sections; owner/maintainer requests are reported as ownership_context coverage gaps pointing to planning_context.ownership_context. "
            "Top-level direct_callers, direct_callees, and repo_dependencies remain available for compatibility. "
            "runtime_surfaces includes bounded path-shape-matched endpoint_consumers for endpoints exposed by the review repo when static CALLS_ENDPOINT facts exist. "
            "framework_impact includes parser-backed support facts for Django/Celery model fields, model relations, serializers, view/model bindings, tasks, and bounded model relationship paths when present. "
            "authz_surface is available from planning_context/get_service_brief for endpoint-to-handler permission evidence; use source inspection for dynamic middleware or framework defaults not represented in the packet. "
            "application_impact groups changed app/package namespace surfaces into API/model/serializer/worker/scheduled-job sections, app-scoped runtime facts, and unlinked cross-repo name leads that require separate verification when those sections are present or explicitly requested. "
            "review_hypotheses contains hypothesis_id-tagged candidates with risk_type, confidence, and evidence_refs. "
            "Use it when you know the changed files and need deterministic static review context before drilling into narrower MCP tools. "
            "Large packets are bounded: when output_budget is present the detail rows were compacted to a coordinate-bearing head start, so inspect source coordinates or call narrower changed_ranges/exact tools for omitted detail. "
            "Does not infer deploy blockers unless explicitly requested, summarize diffs with an LLM, or invent cross-repo and runtime-only impact. "
            "Read review_hypotheses as candidate source-inspection leads, not proven bugs. If using a hypothesis in a review finding, include its hypothesis_id and supporting lead_id values in the finding metadata when the review harness supports attribution."
        ),
        input_schema=_object_schema(_review_context_properties(), required=["repo", "changed_files"]),
        handler=_review_context,
    ),
}
