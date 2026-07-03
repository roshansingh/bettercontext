from __future__ import annotations

from copy import deepcopy

from source.kg.core.models import JsonObject, canonical_json
from source.kg.product.evidence_score import rank_rows, score_key
from source.kg.product.review_attribution import review_lead_counts


# Fleet runtime architecture questions need a compact head-start packet that
# still carries known routes plus high-value unlinked leads. Keep this below the
# server-side MCP spill threshold while avoiding 20k packet starvation.
PLANNING_CONTEXT_MAX_CHARS = 40_000
# Anchored packets serve detailed follow-up questions, so they preserve more evidence than
# the fleet packet, but stay well below the host spill threshold. The scorer-driven hard-cap
# pass guarantees this ceiling holds even on real multi-repo snapshots.
PLANNING_CONTEXT_ANCHORED_MAX_CHARS = 60_000
# review_context and reverse_impact return detailed static rows that can balloon
# past the host spill threshold on real repos. When a packet exceeds these caps,
# the verbose detail rows are compacted to bounded, coordinate-bearing head-start
# rows so the agent inspects source instead of doing saved-file archaeology.
REVIEW_CONTEXT_MAX_CHARS = 40_000
REVERSE_IMPACT_MAX_CHARS = 40_000
# get_service_brief.operational_surfaces is unbounded today and balloons on real
# multi-repo snapshots; bound it with the same head-start discipline. Slightly above the
# 40k detail cap to cover the brief's fixed-overhead floor (service/summary/contracts/authz)
# while staying far below the host spill threshold.
SERVICE_BRIEF_MAX_CHARS = 45_000
COMPACT_RUNTIME_COMPONENT_LIMIT = 4
COMPACT_RUNTIME_ROUTE_LIMIT = 15
COMPACT_RUNTIME_HEADSTART_LIMIT = 8
COMPACT_RUNTIME_LEAD_LIMIT = 8
COMPACT_RUNTIME_DEPLOY_UNIT_LIMIT = 2
COMPACT_RUNTIME_SOURCE_CHECK_LIMIT = 15
COMPACT_AUTHZ_INSPECTION_REF_LIMIT = 3
AUTHZ_COMPACT_LIST_KEYS = (
    "review_leads",
    "inspection_areas",
    "inspection_index",
    "endpoint_authorization",
    "applied_policies",
    "in_method_checks",
    "declared_policies",
    "missing_or_unknown",
    "unsupported_scopes",
)
RELATED_FACT_SECTION_KEYS = frozenset(
    {
        "service_brief",
        "symbol_impact",
        "dependency_importers",
        "inventory",
        "service_operational_surfaces",
        "runtime_architecture",
        "authz_surface",
        "dependencies",
        "endpoints",
        "endpoint_consumers",
        "event_channels",
        "candidate_or_unlinked_event_channels",
        "deploy_mappings",
        "domains",
    }
)

_RUNTIME_COMPONENTS_PATH = "runtime_architecture.answer_packet.runtime_building_blocks"
_RUNTIME_ROUTES_PATH = "runtime_architecture.answer_packet.domain_routing_map"
_RUNTIME_DEPLOY_UNITS_PATH = "runtime_architecture.answer_packet.deploy_runtime_map"
_RUNTIME_DEPLOY_LEADS_PATH = "runtime_architecture.answer_packet.unlinked_deploy_leads"
_RUNTIME_CONSUMERS_PATH = "runtime_architecture.answer_packet.endpoint_consumer_map"
_RUNTIME_DEPLOY_GUIDANCE_PATH = "runtime_architecture.answer_packet.deploy_order_guidance"
_RUNTIME_INVESTIGATION_BRIEF_PATH = "runtime_architecture.answer_packet.investigation_brief"
_BUDGET_BACKFILL_LIST_PATHS: tuple[tuple[str, ...], ...] = (
    ("runtime_architecture", "answer_packet", "investigation_brief", "runtime_anchors"),
    ("runtime_architecture", "answer_packet", "investigation_brief", "known_routes"),
    ("runtime_architecture", "answer_packet", "investigation_brief", "unlinked_runtime_leads"),
    ("runtime_architecture", "answer_packet", "investigation_brief", "deploy_units"),
    ("runtime_architecture", "answer_packet", "investigation_brief", "unlinked_deploy_leads"),
    ("runtime_architecture", "answer_packet", "investigation_brief", "consumer_links"),
    ("runtime_architecture", "answer_packet", "investigation_brief", "recommended_source_checks"),
    *(("authz_surface", key) for key in AUTHZ_COMPACT_LIST_KEYS),
    *(("related_facts", "authz_surface", key) for key in AUTHZ_COMPACT_LIST_KEYS),
    ("runtime_architecture", "answer_packet", "runtime_building_blocks"),
    ("runtime_architecture", "answer_packet", "domain_routing_map"),
    ("runtime_architecture", "answer_packet", "deploy_runtime_map"),
    ("runtime_architecture", "answer_packet", "unlinked_deploy_leads"),
    ("runtime_architecture", "answer_packet", "endpoint_consumer_map"),
    ("runtime_architecture", "answer_packet", "deploy_order_guidance"),
    ("related_facts", "service_brief", "services"),
    ("related_facts", "service_brief", "endpoints"),
    ("related_facts", "service_brief", "endpoint_consumers"),
    ("related_facts", "service_brief", "event_channels"),
    ("related_facts", "service_brief", "candidate_or_unlinked_event_channels"),
    ("related_facts", "service_brief", "deploy_mappings"),
    ("related_facts", "dependencies"),
    ("related_facts", "endpoints"),
    ("related_facts", "endpoint_consumers"),
    ("related_facts", "event_channels"),
    ("related_facts", "candidate_or_unlinked_event_channels"),
    ("related_facts", "deploy_mappings"),
    ("related_facts", "domains"),
    ("related_facts", "symbol_impact", "direct_callers"),
    ("related_facts", "symbol_impact", "direct_callees"),
    ("related_facts", "symbol_impact", "reverse_impact", "tiers"),
    ("related_facts", "symbol_impact", "reverse_impact", "edges"),
    ("related_facts", "symbol_impact", "reverse_impact", "terminal_import_consumer_leads"),
    ("related_facts", "symbol_impact", "reverse_impact", "truncated_terminal_symbols"),
    ("related_facts", "symbol_impact", "reverse_impact", "source_inspection_areas"),
)
_REVIEW_BUDGET_BACKFILL_LIST_PATHS: tuple[tuple[str, ...], ...] = (
    # Hypotheses are highest-priority backfill: they are the reviewer's primary signal and
    # compacted rows are small, so they should be restored before broad context rows.
    ("review_hypotheses",),
    ("review_answer_packet", "top_review_hypotheses"),
    ("review_leads", "changed_symbols"),
    ("review_leads", "direct_callers"),
    ("review_leads", "direct_callees"),
    ("review_leads", "transitive_callers"),
    ("review_leads", "source_coordinates"),
    ("changed_symbols",),
    ("changed_file_symbols",),
    ("direct_callers",),
    ("direct_callees",),
    ("direct_callers_of_changed_symbols",),
    ("direct_callees_from_changed_symbols",),
    ("transitive_callers",),
    ("source_coordinates",),
    ("diff_anchors",),
    ("review_answer_packet", "top_diff_anchors"),
    ("review_answer_packet", "top_changed_symbols"),
    ("review_answer_packet", "changed_file_symbol_inventory"),
    ("review_answer_packet", "top_direct_callers"),
    ("review_answer_packet", "top_direct_callees"),
    ("review_answer_packet", "top_transitive_callers"),
)
_PLANNING_BUDGET_ADVICE = (
    "Use runtime_architecture.answer_packet.investigation_brief as the source-inspection head start, then use narrower "
    "planning_context anchors such as repo+service, domain+repo, endpoint, path, or line to retrieve omitted runtime detail."
)
_PLANNING_GENERIC_BUDGET_ADVICE = (
    "Use the returned related_facts, inspection_areas, and narrower planning_context anchors such as "
    "repo, service, package, path, or line to retrieve omitted detail."
)
_TRUNCATED_DETAIL_ACTION = (
    "When truncated detail matters, call a narrower planning_context anchor or inspect returned refs/search terms instead "
    "of relying on saved-packet exploration."
)


def enforce_planning_context_budget(
    result: JsonObject,
    *,
    max_chars: int = PLANNING_CONTEXT_MAX_CHARS,
    preserve_planning_sections: bool = False,
) -> JsonObject:
    """Bound a planning_context packet to ``max_chars``.

    The section-limit pipeline (truncate -> fallback -> minimal -> backfill) handles the
    common case. On real multi-repo snapshots that pipeline can still overshoot (fat authz
    rows, many sections), so a deterministic scorer-driven hard-cap pass is applied to
    whatever it returns: the lowest-signal rows of the largest leaf lists are demoted to a
    coordinate-bearing inspection area until the packet fits. In the rare case where
    non-row content alone exceeds the cap, the packet is returned with
    output_budget.exceeded_after_minimization set rather than dropping required fields.
    """
    budgeted = _enforce_planning_context_budget_core(
        result, max_chars=max_chars, preserve_planning_sections=preserve_planning_sections
    )
    if _current_chars(budgeted) > max_chars:
        budgeted = _planning_signal_hard_cap(budgeted, max_chars=max_chars)
    return budgeted


def _hard_cap_anchor(result: JsonObject) -> str | None:
    """The packet's most specific structured anchor, for anchor-relative row ranking.

    Uses structured anchor fields (never the free-form NL query, to avoid keyword matching).
    """
    anchors = result.get("anchors")
    if not isinstance(anchors, dict):
        return None
    for field in ("symbol", "service", "endpoint", "event_channel", "domain", "package", "path", "repo"):
        value = anchors.get(field)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _review_hard_cap_anchor(result: JsonObject) -> str | None:
    """Pick a structured review anchor for budget ranking without using free-form text."""
    review_leads = result.get("review_leads")
    if isinstance(review_leads, dict):
        for field in ("changed_symbols", "direct_callers", "direct_callees", "transitive_callers"):
            for row in _list_value(review_leads.get(field)):
                anchor = _row_anchor_string(row)
                if anchor:
                    return anchor
    diff_anchors = result.get("diff_anchors")
    for row in _list_value(diff_anchors):
        anchor = _row_anchor_string(row)
        if anchor:
            return anchor
    for field in ("repo", "requested_repo"):
        value = result.get(field)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _row_anchor_string(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if not isinstance(value, dict):
        return None
    for key in ("qualified_name", "qualname", "display_name", "name", "path", "repo"):
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            return item.strip()
    for key in ("caller_symbol", "callee_symbol", "subject", "object", "symbol"):
        anchor = _row_anchor_string(value.get(key))
        if anchor:
            return anchor
    return None


def _planning_signal_hard_cap(result: JsonObject, *, max_chars: int) -> JsonObject:
    return _signal_hard_cap(
        result,
        max_chars=max_chars,
        anchor=_hard_cap_anchor(result),
        area="planning_budget_overflow",
        reason="Lowest-signal planning rows dropped to fit the packet budget; inspect the cited coordinates.",
    )


def _review_signal_hard_cap(result: JsonObject, *, max_chars: int) -> JsonObject:
    budgeted = _signal_hard_cap(
        result,
        max_chars=max_chars,
        anchor=_review_hard_cap_anchor(result),
        area="review_budget_overflow",
        reason="Lowest-signal review rows dropped to fit the packet budget; inspect the cited coordinates.",
    )
    _sync_review_lead_status_from_packet(budgeted)
    return budgeted


def _signal_hard_cap(
    result: JsonObject,
    *,
    max_chars: int,
    anchor: str | None,
    area: str,
    reason: str,
) -> JsonObject:
    result = deepcopy(result)
    overflow: list[JsonObject] = []
    truncated: set[str] = set()
    guard = 0
    # Reserve room for the overflow inspection area appended after the loop, so adding it
    # never pushes the packet back over the cap.
    target_budget = max(1, max_chars - _HARD_CAP_AREA_RESERVE)
    while _current_chars(result) > target_budget and guard < 5_000:
        guard += 1
        target = _largest_row_list(result)
        if target is None:
            break
        label, rows = target
        ranked = rank_rows([row for row in rows if isinstance(row, dict)], anchor=anchor)
        keep = len(ranked) // 2 if len(ranked) > 1 else 0
        overflow.extend(ranked[keep:])
        rows[:] = ranked[:keep]
        truncated.add(label)
    area = _overflow_inspection_area(
        overflow,
        area=area,
        reason=reason,
        omitted_count=len(overflow),
    )
    if area:
        existing = [row for row in _list_value(result.get("inspection_areas")) if isinstance(row, dict)]
        result["inspection_areas"] = existing + [area]
    budget = result.get("output_budget")
    if isinstance(budget, dict):
        budget["truncated"] = True
        budget["hard_capped"] = True
        budget["truncated_sections"] = sorted(set(budget.get("truncated_sections") or []) | truncated)
        # Only non-row content (e.g. a single oversized string field) can keep the packet
        # over budget once every row list is exhausted.
        if _current_chars(result) > max_chars:
            budget["exceeded_after_minimization"] = True
        else:
            budget.pop("exceeded_after_minimization", None)
    return result


_HARD_CAP_AREA_RESERVE = 4_000
# Tree keys whose lists are contracts/metadata, never row payloads to shrink.
# Contracts/metadata: protected anywhere in the tree (small, never row payloads).
_HARD_CAP_PROTECTED_KEYS = frozenset(
    {"output_budget", "packet_contract", "claim_contract", "scope_contract", "answerability", "next_actions"}
)
# The common evidence index is already bounded by the minimal packet and carries its own
# truncation markers; protect it only at the TOP level — same-named nested lists (e.g.
# authz_surface.inspection_areas) are still shrinkable content.
_HARD_CAP_PROTECTED_TOPLEVEL = frozenset(
    {"proven_facts", "candidate_leads", "coverage_gaps", "inspection_areas"}
)


def _largest_row_list(node: object) -> tuple[str, list] | None:
    """Find the largest list-of-dicts anywhere in the packet (section-agnostic).

    Returns ``(dotted_label, list_ref)`` for the heaviest row list so the hard-cap pass can
    shrink whatever dominates — authz, related_facts, service_operational_surfaces, runtime —
    without maintaining a per-section allowlist. The returned list reference is mutable in
    place. Iterative stack walk to keep the traversal explicit.
    """
    best_size = 0
    best: tuple[str, list] | None = None
    stack: list[tuple[str, object]] = [("", node)]
    while stack:
        label, current = stack.pop()
        if isinstance(current, dict):
            for key, value in current.items():
                if key in _HARD_CAP_PROTECTED_KEYS or (not label and key in _HARD_CAP_PROTECTED_TOPLEVEL):
                    continue
                child_label = f"{label}.{key}" if label else key
                if isinstance(value, list) and value and any(isinstance(x, dict) for x in value):
                    size = len(canonical_json(value))
                    if size > best_size:
                        best_size, best = size, (child_label, value)
                stack.append((child_label, value))
        elif isinstance(current, list):
            for index, item in enumerate(current):
                stack.append((f"{label}[{index}]", item))
    return best


def _enforce_planning_context_budget_core(
    result: JsonObject,
    *,
    max_chars: int = PLANNING_CONTEXT_MAX_CHARS,
    preserve_planning_sections: bool = False,
) -> JsonObject:
    measured_chars = len(canonical_json(result))
    if measured_chars <= max_chars:
        return result

    original_counts = _runtime_answer_counts(result)
    original_result = deepcopy(result)
    result = deepcopy(original_result)
    for component_limit, route_limit in (
        (COMPACT_RUNTIME_COMPONENT_LIMIT, COMPACT_RUNTIME_ROUTE_LIMIT),
        (1, COMPACT_RUNTIME_ROUTE_LIMIT),
        (0, COMPACT_RUNTIME_ROUTE_LIMIT),
        (0, max(1, COMPACT_RUNTIME_ROUTE_LIMIT // 2)),
    ):
        _truncate_runtime_answer(result, component_limit=component_limit, route_limit=route_limit)
        _attach_budget_metadata(
            result,
            measured_chars=measured_chars,
            max_chars=max_chars,
            original_counts=original_counts,
        )
        post_chars = _current_chars(result)
        if post_chars <= max_chars:
            return _backfill_planning_context(
                result,
                original_result,
                measured_chars=measured_chars,
                max_chars=max_chars,
                original_counts=original_counts,
            )

    for component_limit, route_limit in (
        (COMPACT_RUNTIME_COMPONENT_LIMIT, COMPACT_RUNTIME_ROUTE_LIMIT),
        (1, COMPACT_RUNTIME_ROUTE_LIMIT),
        (0, COMPACT_RUNTIME_ROUTE_LIMIT),
        (0, max(1, COMPACT_RUNTIME_ROUTE_LIMIT // 2)),
        (1, 1),
    ):
        fallback_source = deepcopy(original_result)
        _truncate_runtime_answer(fallback_source, component_limit=component_limit, route_limit=route_limit)
        fallback = _planning_context_fallback(fallback_source, preserve_planning_sections=preserve_planning_sections)
        _attach_budget_metadata(
            fallback,
            measured_chars=measured_chars,
            max_chars=max_chars,
            original_counts=original_counts,
            fallback=True,
        )
        fallback_post_chars = _current_chars(fallback)
        if fallback_post_chars <= max_chars:
            return _backfill_planning_context(
                fallback,
                original_result,
                measured_chars=measured_chars,
                max_chars=max_chars,
                original_counts=original_counts,
                fallback=True,
            )

    minimal_source = deepcopy(original_result)
    _truncate_runtime_answer(minimal_source, component_limit=0, route_limit=max(1, COMPACT_RUNTIME_ROUTE_LIMIT))
    minimal = _planning_context_fallback(minimal_source, preserve_planning_sections=preserve_planning_sections)
    _minimize_runtime_answer_rows(minimal)
    _attach_budget_metadata(
        minimal,
        measured_chars=measured_chars,
        max_chars=max_chars,
        original_counts=original_counts,
        fallback=True,
        minimized=True,
    )
    if _current_chars(minimal) <= max_chars:
        return minimal

    final = _minimal_valid_packet(original_result)
    _attach_budget_metadata(
        final,
        measured_chars=measured_chars,
        max_chars=max_chars,
        original_counts=original_counts,
        fallback=True,
        minimized=True,
    )
    if _current_chars(final) > max_chars:
        final["output_budget"]["exceeded_after_minimization"] = True
    return final


_REVIEW_RELATION_DETAIL_FIELDS = (
    "direct_callers",
    "direct_callees",
    "direct_callers_of_changed_symbols",
    "direct_callees_from_changed_symbols",
    "transitive_callers",
    "repo_dependencies",
)
_REVIEW_SYMBOL_DETAIL_FIELDS = ("changed_symbols", "changed_file_symbols")
_REVIEW_ANCHOR_DETAIL_FIELDS = ("diff_anchors",)
_DETAIL_BUDGET_COUNT_RULE = (
    " summary.*_count fields remain the authoritative totals and output_budget.truncated_sections lists which detail arrays "
    "were sampled. Inspect source for the rows not shown; never add the sampled-away rows to the summary totals."
)
_REVIEW_BUDGET_ADVICE = (
    "Detail rows were compacted to bounded, coordinate-bearing head-start rows. Inspect the cited source coordinates or "
    "call review_context again with narrower changed_ranges, or use the exact find_callers/find_callees/reverse_impact "
    "tools for a specific symbol, to recover the rows not shown." + _DETAIL_BUDGET_COUNT_RULE
)
_REVERSE_IMPACT_BUDGET_ADVICE = (
    "Detail rows were compacted to bounded, coordinate-bearing head-start rows. Inspect the cited source coordinates, use "
    "source_inspection_areas, or call reverse_impact again with a narrower depth or an exact anchor to recover the rows not shown."
    " summary.*_returned_count reports how many rows are shown versus the *_count totals."
    + _DETAIL_BUDGET_COUNT_RULE
)
_DETAIL_BUDGET_ACTION = (
    "When omitted detail matters, inspect the returned source coordinates/inspection_areas or call a narrower MCP anchor "
    "instead of relying on saved-packet exploration."
)


_REVIEW_DETAIL_ROW_LIMITS = (COMPACT_RUNTIME_HEADSTART_LIMIT, 4, 2, 1)
_REVERSE_DETAIL_ROW_LIMITS = (COMPACT_RUNTIME_HEADSTART_LIMIT, 4, 2, 1)


def enforce_review_context_budget(
    result: JsonObject, *, max_chars: int = REVIEW_CONTEXT_MAX_CHARS
) -> JsonObject:
    """Compact oversized review_context detail rows to a bounded head start.

    The curated review_answer_packet, summary, scope/claim contracts, surface_status,
    answerability, and common evidence fields are preserved. Verbose static-detail
    arrays (callers/callees/transitive/changed-file inventory/runtime/application
    surfaces) are compacted to bounded coordinate-bearing rows, with the sampled arrays
    listed in output_budget.truncated_sections, so
    the agent inspects source rather than doing saved-file archaeology. Row limits tighten
    across passes until the packet fits; if non-row content alone still exceeds the cap, the
    packet is returned with output_budget.exceeded_after_minimization set.

    Priority order (highest keep-priority first):
      1. review_lead_status
      2. review_hypotheses (top 1-3; top 1 is the floor when any were generated)
      3. review_leads rows with lead_ids
      4. source coordinates for hypotheses/leads
      5. compact diff anchors
      6. inspection_areas
      7. broad application/runtime/framework context
    """
    measured = len(canonical_json(result))
    original_hypotheses = [
        row for row in _list_value(result.get("review_hypotheses")) if isinstance(row, dict)
    ]
    # Pre-budget review_leads snapshot: funds the P1 cluster-coverage repair and the
    # truthful P2 omitted counts, both computed against everything the producer returned.
    original_review_leads = (
        deepcopy(result["review_leads"]) if isinstance(result.get("review_leads"), dict) else {}
    )
    if measured <= max_chars:
        # Sync mirror and emit status even when no compaction is needed so every packet
        # carries review_hypothesis_status regardless of packet size.
        _finalize_review_hypothesis_budget(
            result, original_hypotheses, original_review_leads=original_review_leads, max_chars=max_chars
        )
        return result
    # P1: Cluster-aware ordering BEFORE the row-limit passes — _compact_review_detail
    # samples rows[:limit] and backfill restores in list order, so the interleaved order
    # (one anchor per cluster, rank-ordered, before any second row) is what makes those
    # passes keep >= 1 changed-symbol anchor per changed-file cluster. Copy the containers
    # the interleave reassigns so the caller's packet is never mutated.
    result = dict(result)
    if isinstance(result.get("review_leads"), dict):
        result["review_leads"] = dict(result["review_leads"])
    _interleave_changed_symbols_by_cluster(result)
    for row_limit in _REVIEW_DETAIL_ROW_LIMITS:
        compact, truncated_sections = _compact_review_detail(result, limit=row_limit)
        _attach_detail_budget_metadata(
            compact,
            measured_chars=measured,
            max_chars=max_chars,
            advice=_REVIEW_BUDGET_ADVICE,
            truncated_sections=truncated_sections,
        )
        if len(canonical_json(compact)) <= max_chars:
            backfilled = _backfill_review_context(
                compact,
                result,
                measured_chars=measured,
                max_chars=max_chars,
                truncated_sections=truncated_sections,
            )
            _finalize_review_hypothesis_budget(
                backfilled, original_hypotheses, original_review_leads=original_review_leads, max_chars=max_chars
            )
            return backfilled
    # Even the tightest pass overshot (rare: dominated by non-row content); signal it like
    # the planning path by shrinking the largest low-signal row lists before degrading to a
    # lead-only packet.
    compact = _review_signal_hard_cap(compact, max_chars=max_chars)
    compact = _protect_review_hypotheses_floor(compact, original_hypotheses, max_chars=max_chars)
    if len(canonical_json(compact)) <= max_chars:
        _finalize_review_hypothesis_budget(
            compact, original_hypotheses, original_review_leads=original_review_leads, max_chars=max_chars
        )
        return compact
    compact = _review_lead_only_budget_packet(
        compact,
        measured_chars=measured,
        max_chars=max_chars,
        truncated_sections=truncated_sections,
        original_hypotheses=original_hypotheses,
    )
    _finalize_review_hypothesis_budget(
        compact, original_hypotheses, original_review_leads=original_review_leads, max_chars=max_chars
    )
    if isinstance(compact.get("output_budget"), dict) and len(canonical_json(compact)) > max_chars:
        compact["output_budget"]["exceeded_after_minimization"] = True
    return compact


def _compact_review_detail(result: JsonObject, *, limit: int) -> tuple[JsonObject, set[str]]:
    compact = dict(result)
    truncated_sections: set[str] = set()
    for field in _REVIEW_RELATION_DETAIL_FIELDS:
        rows = result.get(field)
        if isinstance(rows, list):
            kept = _compact_relation_rows(rows, limit=limit)
            _record_truncated(truncated_sections, field, original=len(rows), kept=len(kept))
            compact[field] = kept
    for field in _REVIEW_SYMBOL_DETAIL_FIELDS:
        rows = result.get(field)
        if isinstance(rows, list):
            kept = [_compact_symbol(row) for row in rows[:limit] if isinstance(row, dict)]
            _record_truncated(truncated_sections, field, original=len(rows), kept=len(kept))
            compact[field] = kept
    for field in _REVIEW_ANCHOR_DETAIL_FIELDS:
        rows = result.get(field)
        if isinstance(rows, list):
            kept = [_compact_diff_anchor(row) for row in rows[:limit] if isinstance(row, dict)]
            _record_truncated(truncated_sections, field, original=len(rows), kept=len(kept))
            compact[field] = kept
    for dict_field in ("impact", "runtime_surfaces"):
        nested = result.get(dict_field)
        if not isinstance(nested, dict):
            continue
        compacted: JsonObject = {}
        for key, value in nested.items():
            if isinstance(value, list):
                kept_rows = _compact_relation_rows(value, limit=limit)
                _record_truncated(truncated_sections, f"{dict_field}.{key}", original=len(value), kept=len(kept_rows))
                compacted[key] = kept_rows
            else:
                compacted[key] = value
        compact[dict_field] = compacted
    for nested_field in ("framework_impact", "application_impact"):
        nested = result.get(nested_field)
        if isinstance(nested, dict):
            compact[nested_field] = _compact_nested_detail(
                nested, limit=limit, truncated_sections=truncated_sections, label=nested_field
            )
    source_coordinates = result.get("source_coordinates")
    if isinstance(source_coordinates, list):
        kept_coords = [
            _compact_coordinate(row) if isinstance(row, dict) else row
            for row in source_coordinates[:COMPACT_RUNTIME_SOURCE_CHECK_LIMIT]
        ]
        _record_truncated(truncated_sections, "source_coordinates", original=len(source_coordinates), kept=len(kept_coords))
        compact["source_coordinates"] = kept_coords
    changed_surface = result.get("changed_surface")
    if isinstance(changed_surface, dict):
        surface = dict(changed_surface)
        symbols = changed_surface.get("symbols")
        if isinstance(symbols, list):
            kept_symbols = [_compact_symbol(row) for row in symbols[:limit] if isinstance(row, dict)]
            _record_truncated(truncated_sections, "changed_surface.symbols", original=len(symbols), kept=len(kept_symbols))
            surface["symbols"] = kept_symbols
        compact["changed_surface"] = surface
    evidence = result.get("evidence")
    if isinstance(evidence, list):
        kept_evidence = evidence[:limit]
        _record_truncated(truncated_sections, "evidence", original=len(evidence), kept=len(kept_evidence))
        compact["evidence"] = kept_evidence
    review_hypotheses = result.get("review_hypotheses")
    if isinstance(review_hypotheses, list):
        kept_hypotheses = [_compact_review_hypothesis(row) for row in review_hypotheses[:limit] if isinstance(row, dict)]
        _record_truncated(truncated_sections, "review_hypotheses", original=len(review_hypotheses), kept=len(kept_hypotheses))
        compact["review_hypotheses"] = kept_hypotheses
    answer_packet = result.get("review_answer_packet")
    if isinstance(answer_packet, dict):
        compact["review_answer_packet"] = _compact_review_answer_packet(
            answer_packet, limit=limit, truncated_sections=truncated_sections
        )
    _sync_compact_review_leads(compact, result, truncated_sections=truncated_sections)
    return compact, truncated_sections


def _sync_compact_review_leads(
    compact: JsonObject,
    original: JsonObject,
    *,
    truncated_sections: set[str],
) -> None:
    review_leads = original.get("review_leads")
    if not isinstance(review_leads, dict):
        return
    synced = dict(review_leads)
    for field in ("changed_symbols", "direct_callers", "direct_callees", "transitive_callers", "source_coordinates"):
        rows = compact.get(field)
        if isinstance(rows, list):
            _record_truncated(
                truncated_sections,
                f"review_leads.{field}",
                original=len(review_leads.get(field, [])) if isinstance(review_leads.get(field), list) else 0,
                kept=len(rows),
            )
            synced[field] = rows
    changed_files = review_leads.get("changed_files")
    if isinstance(changed_files, list) and len(changed_files) > COMPACT_RUNTIME_SOURCE_CHECK_LIMIT:
        synced["changed_files"] = changed_files[:COMPACT_RUNTIME_SOURCE_CHECK_LIMIT]
        truncated_sections.add("review_leads.changed_files")
    compact["review_leads"] = synced
    status = original.get("review_lead_status")
    if not isinstance(status, dict):
        return
    compact["review_lead_status"] = dict(status)
    _sync_review_lead_status_from_packet(compact)


def _backfill_review_context(
    result: JsonObject,
    original_result: JsonObject,
    *,
    measured_chars: int,
    max_chars: int,
    truncated_sections: set[str],
) -> JsonObject:
    candidate = deepcopy(result)
    backfilled_counts: dict[str, int] = {}
    for path in _REVIEW_BUDGET_BACKFILL_LIST_PATHS:
        candidate, added = _backfill_review_list_path(
            candidate,
            original_result,
            path,
            measured_chars=measured_chars,
            max_chars=max_chars,
            truncated_sections=truncated_sections,
            backfilled_counts=backfilled_counts,
        )
        if added:
            backfilled_counts[_path_label(path)] = backfilled_counts.get(_path_label(path), 0) + added
    final_truncated_sections = _review_truncated_sections_after_backfill(
        candidate,
        original_result,
        truncated_sections,
    )
    _sync_review_lead_status_from_packet(candidate)
    _attach_detail_budget_metadata(
        candidate,
        measured_chars=measured_chars,
        max_chars=max_chars,
        advice=_REVIEW_BUDGET_ADVICE,
        truncated_sections=final_truncated_sections,
    )
    if backfilled_counts and isinstance(candidate.get("output_budget"), dict):
        candidate["output_budget"]["backfilled_counts"] = {
            key: value for key, value in sorted(backfilled_counts.items()) if value > 0
        }
    return candidate


def _review_truncated_sections_after_backfill(
    candidate: JsonObject,
    original_result: JsonObject,
    truncated_sections: set[str],
) -> set[str]:
    remaining = set(truncated_sections)
    for path in _REVIEW_BUDGET_BACKFILL_LIST_PATHS:
        source = _nested_list(original_result, path)
        target = _nested_list(candidate, path)
        if source is not None and target is not None and len(target) >= len(source):
            remaining.discard(_path_label(path))
    return remaining


def _backfill_review_list_path(
    candidate: JsonObject,
    original_result: JsonObject,
    path: tuple[str, ...],
    *,
    measured_chars: int,
    max_chars: int,
    truncated_sections: set[str],
    backfilled_counts: dict[str, int],
) -> tuple[JsonObject, int]:
    source = _nested_list(original_result, path)
    target = _nested_list(candidate, path)
    if source is None or target is None or len(target) >= len(source):
        return candidate, 0
    added = 0
    for row in source[len(target) :]:
        trial_base = deepcopy(candidate)
        trial_target = _nested_list(trial_base, path)
        if trial_target is None:
            break
        compact_row = _compact_review_backfill_row(path, row)
        if not compact_row:
            continue
        trial_target.append(compact_row)
        proposed_counts = dict(backfilled_counts)
        proposed_counts[_path_label(path)] = proposed_counts.get(_path_label(path), 0) + added + 1
        _sync_review_lead_status_from_packet(trial_base)
        trial_truncated_sections = _review_truncated_sections_after_backfill(
            trial_base,
            original_result,
            truncated_sections,
        )
        _attach_detail_budget_metadata(
            trial_base,
            measured_chars=measured_chars,
            max_chars=max_chars,
            advice=_REVIEW_BUDGET_ADVICE,
            truncated_sections=trial_truncated_sections,
        )
        if proposed_counts and isinstance(trial_base.get("output_budget"), dict):
            trial_base["output_budget"]["backfilled_counts"] = {
                key: value for key, value in sorted(proposed_counts.items()) if value > 0
            }
        if _current_chars(trial_base) > max_chars:
            break
        candidate = trial_base
        added += 1
    return candidate, added


def _review_lead_only_budget_packet(
    result: JsonObject,
    *,
    measured_chars: int,
    max_chars: int,
    truncated_sections: set[str],
    original_hypotheses: list[JsonObject] | None = None,
) -> JsonObject:
    compact, lead_truncated = _compact_review_detail(result, limit=1)
    truncated_sections = set(truncated_sections) | lead_truncated
    packet = compact.get("review_answer_packet") if isinstance(compact.get("review_answer_packet"), dict) else {}
    lead_packet: JsonObject = {
        "status": packet.get("status", compact.get("status")),
        "packet_mode": "lead_only",
        "repo_resolution": packet.get("repo_resolution", compact.get("repo_resolution", {})),
        "review_lead_status": compact.get("review_lead_status", {}),
        "summary": packet.get("summary", compact.get("summary", {})),
        "top_diff_anchors": packet.get("top_diff_anchors", []),
        "top_changed_symbols": packet.get("top_changed_symbols", []),
        "top_direct_callers": packet.get("top_direct_callers", []),
        "top_direct_callees": packet.get("top_direct_callees", []),
        "top_transitive_callers": packet.get("top_transitive_callers", []),
    }
    lead_only: JsonObject = {
        "tool": compact.get("tool", "review_context"),
        "status": compact.get("status"),
        "repo": compact.get("repo"),
        "requested_repo": compact.get("requested_repo"),
        "repo_resolution": compact.get("repo_resolution", {}),
        "summary": compact.get("summary", {}),
        "review_answer_packet": {key: value for key, value in lead_packet.items() if value not in (None, [], {})},
        "review_lead_status": compact.get("review_lead_status", {}),
        "review_leads": compact.get("review_leads", {}),
        "diff_anchors": compact.get("diff_anchors", []),
        "changed_symbols": compact.get("changed_symbols", []),
        "changed_file_symbols": compact.get("changed_file_symbols", []),
        "direct_callers": compact.get("direct_callers", []),
        "direct_callees": compact.get("direct_callees", []),
        "transitive_callers": compact.get("transitive_callers", []),
        "source_coordinates": compact.get("source_coordinates", []),
        "answerability": compact.get("answerability", {}),
        "coverage_warnings": compact.get("coverage_warnings", []),
        "unsupported_scopes": compact.get("unsupported_scopes", []),
        "unsupported_review_scopes": compact.get("unsupported_review_scopes", []),
        "next_actions": compact.get("next_actions", []),
    }
    # Use original_hypotheses as fallback when the compact pass evicted all hypotheses
    # (can happen when the last detail pass reduced them to an empty list).
    hyp_source = compact.get("review_hypotheses") or (original_hypotheses or [])
    compacted_hypotheses = [
        _compact_review_hypothesis(row)
        for row in hyp_source
        if isinstance(row, dict)
    ]
    if compacted_hypotheses:
        lead_only["review_hypotheses"] = compacted_hypotheses
    _sync_review_lead_status_from_packet(lead_only)
    _attach_detail_budget_metadata(
        lead_only,
        measured_chars=measured_chars,
        max_chars=max_chars,
        advice=_REVIEW_BUDGET_ADVICE,
        truncated_sections=truncated_sections,
    )
    if isinstance(lead_only.get("output_budget"), dict):
        lead_only["output_budget"]["lead_only"] = True
    if _current_chars(lead_only) > max_chars:
        lead_only = _review_signal_hard_cap(lead_only, max_chars=max_chars)
        lead_only = _protect_review_hypotheses_floor(lead_only, original_hypotheses or [], max_chars=max_chars)
        if isinstance(lead_only.get("output_budget"), dict):
            lead_only["output_budget"]["lead_only"] = True
    return lead_only


def _sync_review_lead_status_from_packet(result: JsonObject) -> None:
    """Sync review_lead_status counts from the current review_leads rows.

    N1 coherence rule: answer-packet top_* rows are always a subset of review_leads rows;
    returned counts count review_leads. After syncing counts, clip any answer-packet top_*
    list to only the rows whose lead_id still exists in the corresponding review_leads list
    — so top_changed_symbols ⊆ review_leads.changed_symbols, etc. Rows without a lead_id
    are kept as-is (they cannot be cross-referenced). This is the single authoritative
    coherence checkpoint called after every eviction step.
    """
    status = result.get("review_lead_status")
    if not isinstance(status, dict):
        return
    review_leads = result.get("review_leads")
    if not isinstance(review_leads, dict):
        review_leads = {}
    synced_status = dict(status)
    changed_symbols = review_leads.get("changed_symbols") if isinstance(review_leads.get("changed_symbols"), list) else []
    direct_callers = review_leads.get("direct_callers") if isinstance(review_leads.get("direct_callers"), list) else []
    direct_callees = review_leads.get("direct_callees") if isinstance(review_leads.get("direct_callees"), list) else []
    transitive_callers = (
        review_leads.get("transitive_callers") if isinstance(review_leads.get("transitive_callers"), list) else []
    )
    source_coordinates = (
        review_leads.get("source_coordinates") if isinstance(review_leads.get("source_coordinates"), list) else []
    )
    diff_anchors = result.get("diff_anchors") if isinstance(result.get("diff_anchors"), list) else []
    kept_changed_anchor_count = sum(
        1 for row in diff_anchors if isinstance(row, dict) and row.get("anchor_type") == "symbol"
    )
    kept_file_anchor_count = sum(1 for row in diff_anchors if isinstance(row, dict) and row.get("anchor_type") == "file")
    original_changed_anchor_count = status.get("changed_anchor_count")
    original_file_anchor_count = status.get("file_anchor_count")
    changed_anchor_count = max(
        kept_changed_anchor_count,
        original_changed_anchor_count
        if isinstance(original_changed_anchor_count, int) and not isinstance(original_changed_anchor_count, bool)
        else 0,
    )
    file_anchor_count = max(
        kept_file_anchor_count,
        original_file_anchor_count
        if isinstance(original_file_anchor_count, int) and not isinstance(original_file_anchor_count, bool)
        else 0,
    )
    # Budgeted review_lead_status counts describe the lead rows still shown in the packet.
    # The authoritative unbounded totals remain in summary.*_count fields.
    synced_status.update(
        {
            "changed_anchor_count": changed_anchor_count,
            "changed_symbol_count": len(changed_symbols),
            "direct_impact_count": len(direct_callers) + len(direct_callees),
            "transitive_impact_count": len(transitive_callers),
            "source_coordinate_count": len(source_coordinates),
            "file_anchor_count": file_anchor_count,
        }
    )
    # Preserve the pre-compaction available counts; recompute returned from current packet.
    available = status.get("available")
    if isinstance(available, dict):
        synced_status["available"] = available
    synced_status["returned"] = review_lead_counts(review_leads)
    result["review_lead_status"] = synced_status
    packet = result.get("review_answer_packet")
    if isinstance(packet, dict):
        packet["review_lead_status"] = synced_status
        # N1: clip answer-packet top_* to the rows mirrored in review_leads.
        # Rows without a lead_id have no cross-reference and are kept as-is.
        _clip_answer_packet_top_to_review_leads(packet, review_leads)


# Map from answer-packet field → review_leads field for N1 coherence clipping.
_ANSWER_PACKET_TO_REVIEW_LEADS_FIELD = {
    "top_changed_symbols": "changed_symbols",
    "top_direct_callers": "direct_callers",
    "top_direct_callees": "direct_callees",
    "top_transitive_callers": "transitive_callers",
}


def _clip_answer_packet_top_to_review_leads(
    answer_packet: JsonObject, review_leads: JsonObject
) -> None:
    """N1: Clip each answer-packet top_* list to only rows present in review_leads.

    Rows without a lead_id are kept as-is (they carry no cross-reference). Rows with a
    lead_id are kept only if that lead_id appears in the corresponding review_leads list.
    This prevents the incoherence where top_changed_symbols shows a symbol that was
    evicted from review_leads.changed_symbols, which would make returned counts lie.
    """
    for ap_field, rl_field in _ANSWER_PACKET_TO_REVIEW_LEADS_FIELD.items():
        ap_rows = answer_packet.get(ap_field)
        if not isinstance(ap_rows, list):
            continue
        rl_rows = review_leads.get(rl_field)
        if not isinstance(rl_rows, list):
            # review_leads field absent or non-list → all rows without lead_id survive.
            surviving_ids: set[str] = set()
        else:
            surviving_ids = {
                row["lead_id"]
                for row in rl_rows
                if isinstance(row, dict) and isinstance(row.get("lead_id"), str) and row["lead_id"]
            }
        clipped = [
            row
            for row in ap_rows
            if not isinstance(row, dict)
            or not isinstance(row.get("lead_id"), str)
            or not row.get("lead_id")
            or row["lead_id"] in surviving_ids
        ]
        answer_packet[ap_field] = clipped


def enforce_reverse_impact_budget(
    result: JsonObject, *, max_chars: int = REVERSE_IMPACT_MAX_CHARS
) -> JsonObject:
    """Compact oversized reverse_impact detail rows to a bounded head start.

    Summary counts (which already separate callable affected symbols from terminal
    import leads), answerability, claim/packet contracts, and common evidence fields
    are preserved. Edges, tiers, affected symbols, and terminal/truncated leads are
    compacted to bounded coordinate-bearing rows; sampled arrays are listed in
    output_budget.truncated_sections and summary.*_returned_count is synced to the rows
    shown. Row limits tighten across passes until the packet fits; if non-row content alone
    still exceeds the cap, the packet is returned with output_budget.exceeded_after_minimization set.
    """
    measured = len(canonical_json(result))
    if measured <= max_chars:
        return result
    for row_limit in _REVERSE_DETAIL_ROW_LIMITS:
        compact, truncated_sections = _compact_reverse_detail(result, limit=row_limit)
        _attach_detail_budget_metadata(
            compact,
            measured_chars=measured,
            max_chars=max_chars,
            advice=_REVERSE_IMPACT_BUDGET_ADVICE,
            truncated_sections=truncated_sections,
        )
        if len(canonical_json(compact)) <= max_chars:
            return compact
    # Even the tightest pass overshot (rare: dominated by non-row content); signal it like
    # the planning path rather than silently returning an over-budget packet.
    if isinstance(compact.get("output_budget"), dict):
        compact["output_budget"]["exceeded_after_minimization"] = True
    return compact


def _compact_reverse_detail(result: JsonObject, *, limit: int) -> tuple[JsonObject, set[str]]:
    compact = dict(result)
    truncated_sections: set[str] = set()
    edges = result.get("edges")
    if isinstance(edges, list):
        kept = _compact_relation_rows(edges, limit=limit)
        _record_truncated(truncated_sections, "edges", original=len(edges), kept=len(kept))
        compact["edges"] = kept
    tiers = result.get("tiers")
    if isinstance(tiers, list):
        kept_tiers = _compact_reverse_impact_tiers(tiers)[:limit]
        _record_truncated(truncated_sections, "tiers", original=len(tiers), kept=len(kept_tiers))
        compact["tiers"] = kept_tiers
    affected = result.get("affected_symbols")
    if isinstance(affected, list):
        kept = _compact_reverse_impact_symbols(affected)[:limit]
        _record_truncated(truncated_sections, "affected_symbols", original=len(affected), kept=len(kept))
        compact["affected_symbols"] = kept
    call_site_leads = result.get("call_site_leads")
    if isinstance(call_site_leads, list):
        kept = _compact_reverse_impact_symbols(call_site_leads)[:limit]
        _record_truncated(truncated_sections, "call_site_leads", original=len(call_site_leads), kept=len(kept))
        compact["call_site_leads"] = kept
    roots = result.get("roots")
    if isinstance(roots, list):
        kept_roots = [_compact_symbol(row) for row in roots[:limit] if isinstance(row, dict)]
        _record_truncated(truncated_sections, "roots", original=len(roots), kept=len(kept_roots))
        compact["roots"] = kept_roots
    bridges = result.get("constructor_bridges")
    if isinstance(bridges, list):
        kept_bridges = _compact_constructor_bridges(bridges)[:limit]
        _record_truncated(truncated_sections, "constructor_bridges", original=len(bridges), kept=len(kept_bridges))
        compact["constructor_bridges"] = kept_bridges
    leads = result.get("terminal_import_consumer_leads")
    if isinstance(leads, list):
        kept_leads = _compact_terminal_import_leads(leads)[:limit]
        _record_truncated(truncated_sections, "terminal_import_consumer_leads", original=len(leads), kept=len(kept_leads))
        compact["terminal_import_consumer_leads"] = kept_leads
    truncated_terminals = result.get("truncated_terminal_symbols")
    if isinstance(truncated_terminals, list):
        kept_terminals = _compact_truncated_terminal_symbols(truncated_terminals)[:limit]
        _record_truncated(
            truncated_sections, "truncated_terminal_symbols", original=len(truncated_terminals), kept=len(kept_terminals)
        )
        compact["truncated_terminal_symbols"] = kept_terminals
    previews = result.get("candidate_impact_previews")
    if isinstance(previews, list):
        kept_previews = _compact_candidate_impact_previews(previews)
        _record_truncated(truncated_sections, "candidate_impact_previews", original=len(previews), kept=len(kept_previews))
        compact["candidate_impact_previews"] = kept_previews
    source = result.get("source")
    if isinstance(source, dict):
        compact["source"] = _compact_reverse_impact_source(source)
    # Keep summary returned-counts consistent with the rows actually shown so the
    # authoritative totals never contradict the displayed sample (e.g. total=8 while
    # only 2 rows are shown is reported as returned=2, not as 6 "omitted" extras).
    summary = result.get("summary")
    if isinstance(summary, dict):
        synced = dict(summary)
        for field, key in (
            ("affected_symbols", "affected_symbol_returned_count"),
            ("call_site_leads", "call_site_lead_returned_count"),
            ("constructor_bridges", "constructor_bridge_returned_count"),
            ("truncated_terminal_symbols", "truncated_terminal_symbol_returned_count"),
        ):
            if isinstance(compact.get(field), list):
                synced[key] = len(compact[field])
        # terminal_import_lead_returned_count sums the per-row returned counts, so recompute
        # it from the kept lead rows rather than the pre-budget total.
        if isinstance(compact.get("terminal_import_consumer_leads"), list):
            synced["terminal_import_lead_returned_count"] = sum(
                _safe_non_bool_int(row.get("import_consumer_leads", {}).get("returned_count"))
                for row in compact["terminal_import_consumer_leads"]
                if isinstance(row, dict)
            )
        compact["summary"] = synced
    return compact, truncated_sections


def _compact_reverse_impact_source(value: JsonObject) -> JsonObject:
    # Preserve recovery fields (confidence, query, and the coordinate_mismatch retry hint)
    # so an over-budget packet for a wrong path/line still tells the agent how to retry.
    keys = ("status", "query", "reason", "message", "candidate_count", "confidence", "coordinate_mismatch")
    compact = {key: value[key] for key in keys if key in value}
    resolved = value.get("resolved_symbol")
    if isinstance(resolved, dict):
        compact["resolved_symbol"] = _compact_symbol(resolved)
    candidates = value.get("candidates")
    if isinstance(candidates, list):
        compact["candidates"] = [_compact_symbol(row) for row in candidates[:COMPACT_RUNTIME_HEADSTART_LIMIT] if isinstance(row, dict)]
    return compact


def _record_truncated(truncated_sections: set[str], field: str, *, original: int, kept: int) -> None:
    if original > kept:
        truncated_sections.add(field)


def _signal_ranked_sections(
    sections: list[tuple[str, list, str | None]],
    *,
    char_budget: int,
    anchor: str | None,
) -> tuple[dict[str, list[JsonObject]], list[JsonObject], set[str]]:
    """Allocate one char budget across several named lists by structural score.

    Rows from every section compete in a single best-first ranking (via evidence_score),
    so the strongest evidence is kept in full regardless of which section it came from and
    the weakest is demoted — instead of each section truncating uniformly. Returns the kept
    rows per section, the demoted rows (for a coordinate-bearing inspection area), and the
    set of section names that lost rows.
    """
    candidates: list[tuple[int, str, JsonObject, str | None]] = []
    order = 0
    for name, rows, linkage in sections:
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict):
                candidates.append((order, name, row, linkage))
                order += 1
    candidates.sort(
        key=lambda item: (score_key(item[2], anchor=anchor, linkage=item[3]), -item[0]),
        reverse=True,
    )
    kept: dict[str, list[JsonObject]] = {name: [] for name, _, _ in sections}
    demoted: list[JsonObject] = []
    truncated: set[str] = set()
    used = 0
    for _, name, row, _linkage in candidates:
        cost = len(canonical_json(row))
        if used + cost <= char_budget:
            kept[name].append(row)
            used += cost
        else:
            demoted.append(row)
            truncated.add(name)
    return kept, demoted, truncated


def enforce_service_brief_budget(
    result: JsonObject, *, max_chars: int = SERVICE_BRIEF_MAX_CHARS
) -> JsonObject:
    """Bound an oversized get_service_brief by signal-ranking its operational surfaces.

    The service fact sheet, summary, evidence partition labels, and contracts are kept; the
    bulky operational-surface lists compete in one best-first ranking and overflow is demoted
    to a coordinate-bearing inspection area, never dropped or reduced to a bare count.
    """
    measured = len(canonical_json(result))
    if measured <= max_chars:
        return result
    # The section char budget tightens across passes until the whole packet fits, so the
    # overhead of non-ranked fields (service, summary, contracts, authz) is accounted for
    # without fragile pre-estimation.
    for section_budget in (max_chars * 3 // 4, max_chars // 2, max_chars // 4, max_chars // 8):
        compact = _compact_service_brief_surfaces(result, section_budget=section_budget, measured=measured, max_chars=max_chars)
        if len(canonical_json(compact)) <= max_chars:
            return compact
    # Even the tightest pass overshot (rare: dominated by non-row content); signal it like
    # the planning path rather than silently returning an over-budget packet.
    if isinstance(compact.get("output_budget"), dict):
        compact["output_budget"]["exceeded_after_minimization"] = True
    return compact


def _compact_service_brief_surfaces(
    result: JsonObject, *, section_budget: int, measured: int, max_chars: int
) -> JsonObject:
    compact = dict(result)
    surfaces = result.get("operational_surfaces")
    overflow: list[JsonObject] = []
    truncated_sections: set[str] = set()
    if isinstance(surfaces, dict):
        compact_surfaces = dict(surfaces)
        partition = surfaces.get("evidence_partition")
        sections: list[tuple[str, list, str | None]] = []
        if isinstance(partition, dict):
            sections.append(("evidence_partition.known_linked", partition.get("known_linked", []), "known_linked"))
            sections.append(("evidence_partition.unlinked_evidence", partition.get("unlinked_evidence", []), "unlinked"))
        sections.append(("direct_domain_references", surfaces.get("direct_domain_references", []), "candidate"))
        sections.append(("unlinked_domain_route_samples", surfaces.get("unlinked_domain_route_samples", []), "unlinked"))
        sections.append(("endpoint_consumers", surfaces.get("endpoint_consumers", []), "candidate"))
        sections.append(("deploy_target_candidates", surfaces.get("deploy_target_candidates", []), "candidate"))
        sections.append(("domain_route_candidates", surfaces.get("domain_route_candidates", []), "candidate"))
        sections.append(("deploy_link_facts", surfaces.get("deploy_link_facts", []), "known_linked"))
        sections.append(("deploy_runtime_units", surfaces.get("deploy_runtime_units", []), "known_linked"))
        kept, demoted, truncated = _signal_ranked_sections(sections, char_budget=section_budget, anchor=None)
        if isinstance(partition, dict):
            compact_partition = dict(partition)
            compact_partition["known_linked"] = kept.get("evidence_partition.known_linked", [])
            compact_partition["unlinked_evidence"] = kept.get("evidence_partition.unlinked_evidence", [])
            compact_surfaces["evidence_partition"] = compact_partition
        for field in (
            "direct_domain_references",
            "unlinked_domain_route_samples",
            "endpoint_consumers",
            "deploy_target_candidates",
            "domain_route_candidates",
            "deploy_link_facts",
            "deploy_runtime_units",
        ):
            if field in compact_surfaces:
                compact_surfaces[field] = kept.get(field, [])
        compact["operational_surfaces"] = compact_surfaces
        overflow = demoted
        # Full packet paths (e.g. operational_surfaces.evidence_partition.known_linked), so
        # truncated_sections entries are addressable and consistent with the other budgeters.
        truncated_sections = {f"operational_surfaces.{name}" for name in truncated}
    endpoints = result.get("endpoints")
    if isinstance(endpoints, list):
        kept_endpoints = _compact_relation_rows(endpoints, limit=COMPACT_RUNTIME_HEADSTART_LIMIT)
        if len(endpoints) > len(kept_endpoints):
            truncated_sections.add("endpoints")
        compact["endpoints"] = kept_endpoints
    authz = result.get("authz_surface")
    if isinstance(authz, dict):
        compact_authz = _compact_authz_surface(authz)
        if len(canonical_json(compact_authz)) < len(canonical_json(authz)):
            truncated_sections.add("authz_surface")
        compact["authz_surface"] = compact_authz
    inspection_area = _overflow_inspection_area(
        overflow,
        area="service_operational_surface_overflow",
        reason="Operational-surface rows beyond the service-brief budget; inspect the cited coordinates.",
        omitted_count=len(overflow),
    )
    if inspection_area:
        existing = [row for row in _list_value(result.get("inspection_areas")) if isinstance(row, dict)]
        compact["inspection_areas"] = existing + [inspection_area]
    _attach_detail_budget_metadata(
        compact,
        measured_chars=measured,
        max_chars=max_chars,
        advice=(
            "Operational-surface rows were signal-ranked: the strongest (known_linked, source-cited) rows are kept and "
            "weaker rows demoted to inspection_areas with coordinates. Inspect those or call narrower tools for the rest."
        ),
        truncated_sections=truncated_sections,
    )
    return compact


def _compact_nested_detail(
    value: JsonObject, *, limit: int, truncated_sections: set[str] | None = None, label: str = ""
) -> JsonObject:
    """Bound every inner list of a nested detail packet without reshaping rows.

    Records each truncated inner list in ``truncated_sections`` (when supplied) under a
    dotted ``label.key`` path so the budget metadata reflects every sampled array.
    """
    compact: JsonObject = {}
    for key, item in value.items():
        if isinstance(item, list):
            kept = item[:limit]
            if truncated_sections is not None and len(item) > len(kept):
                truncated_sections.add(f"{label}.{key}" if label else key)
            compact[key] = kept
        elif isinstance(item, dict):
            inner_compact: JsonObject = {}
            for inner_key, inner in item.items():
                if isinstance(inner, list):
                    inner_kept = inner[:limit]
                    if truncated_sections is not None and len(inner) > len(inner_kept):
                        path = f"{label}.{key}.{inner_key}" if label else f"{key}.{inner_key}"
                        truncated_sections.add(path)
                    inner_compact[inner_key] = inner_kept
                else:
                    inner_compact[inner_key] = inner
            compact[key] = inner_compact
        else:
            compact[key] = item
    return compact


def _compact_review_answer_nested_detail(
    value: JsonObject, *, limit: int, truncated_sections: set[str] | None = None, label: str = ""
) -> JsonObject:
    compact: JsonObject = {}
    for key, item in value.items():
        if isinstance(item, list):
            kept = [_compact_review_nested_row(row) if isinstance(row, dict) else row for row in item[:limit]]
            if truncated_sections is not None and len(item) > len(kept):
                truncated_sections.add(f"{label}.{key}" if label else key)
            compact[key] = kept
        elif isinstance(item, dict):
            compact[key] = _compact_review_answer_nested_detail(
                item,
                limit=limit,
                truncated_sections=truncated_sections,
                label=f"{label}.{key}" if label else key,
            )
        else:
            compact[key] = item
    return compact


def _compact_review_answer_packet(
    value: JsonObject, *, limit: int, truncated_sections: set[str] | None = None
) -> JsonObject:
    compact = dict(value)
    anchors = value.get("top_diff_anchors")
    if isinstance(anchors, list):
        kept_anchors = [_compact_diff_anchor(row) for row in anchors[:limit] if isinstance(row, dict)]
        if truncated_sections is not None:
            _record_truncated(
                truncated_sections,
                "review_answer_packet.top_diff_anchors",
                original=len(anchors),
                kept=len(kept_anchors),
            )
        compact["top_diff_anchors"] = kept_anchors
    for field in ("changed_file_symbol_inventory", "top_changed_symbols"):
        rows = value.get(field)
        if isinstance(rows, list):
            kept = [_compact_symbol(row) for row in rows[:limit] if isinstance(row, dict)]
            if truncated_sections is not None:
                _record_truncated(truncated_sections, f"review_answer_packet.{field}", original=len(rows), kept=len(kept))
            compact[field] = kept
    for field in ("top_direct_callers", "top_direct_callees", "top_transitive_callers"):
        rows = value.get(field)
        if isinstance(rows, list):
            kept_rows = _compact_relation_rows(rows, limit=limit)
            if truncated_sections is not None:
                _record_truncated(
                    truncated_sections,
                    f"review_answer_packet.{field}",
                    original=len(rows),
                    kept=len(kept_rows),
                )
            compact[field] = kept_rows
    for field in ("surface_status", "inspection_areas"):
        rows = value.get(field)
        if isinstance(rows, list):
            kept = [_compact_review_nested_row(row) for row in rows[:limit] if isinstance(row, dict)]
            if truncated_sections is not None:
                _record_truncated(truncated_sections, f"review_answer_packet.{field}", original=len(rows), kept=len(kept))
            compact[field] = kept
    top_hypotheses = value.get("top_review_hypotheses")
    if isinstance(top_hypotheses, list):
        kept_hypotheses = [_compact_review_hypothesis(row) for row in top_hypotheses[:limit] if isinstance(row, dict)]
        if truncated_sections is not None:
            _record_truncated(
                truncated_sections,
                "review_answer_packet.top_review_hypotheses",
                original=len(top_hypotheses),
                kept=len(kept_hypotheses),
            )
        compact["top_review_hypotheses"] = kept_hypotheses
    for field in ("runtime", "framework", "application", "runtime_surfaces", "framework_impact", "application_impact"):
        nested = value.get(field)
        if isinstance(nested, dict):
            compact[field] = _compact_review_answer_nested_detail(
                nested,
                limit=limit,
                truncated_sections=truncated_sections,
                label=f"review_answer_packet.{field}",
            )
    return compact


def _attach_detail_budget_metadata(
    result: JsonObject,
    *,
    measured_chars: int,
    max_chars: int,
    advice: str,
    truncated_sections: set[str],
) -> None:
    result["output_budget"] = {
        "truncated": True,
        "minimized": True,
        "measured_chars": measured_chars,
        "max_chars": max_chars,
        "truncated_sections": sorted(truncated_sections),
        "advice": advice,
    }
    actions = [str(action) for action in _list_value(result.get("next_actions")) if str(action).strip()]
    actions.append(_DETAIL_BUDGET_ACTION)
    result["next_actions"] = _dedupe_strings(actions)


def _runtime_answer_counts(result: JsonObject) -> dict[str, int]:
    runtime = result.get("runtime_architecture")
    if not isinstance(runtime, dict):
        return {"runtime_building_blocks": 0, "domain_routing_map": 0}
    summary = runtime.get("summary")
    if not isinstance(summary, dict):
        summary = {}
    answer_packet = runtime.get("answer_packet")
    if not isinstance(answer_packet, dict):
        answer_packet = {}
    components = answer_packet.get("runtime_building_blocks")
    routes = answer_packet.get("domain_routing_map")
    return {
        "investigation_brief": _int_count(
            summary.get("investigation_brief_anchor_count"),
            fallback=_investigation_brief_anchor_count(answer_packet.get("investigation_brief")),
        ),
        "runtime_building_blocks": _int_count(
            summary.get("runtime_building_block_count"),
            fallback=len(components) if isinstance(components, list) else 0,
        ),
        "domain_routing_map": _int_count(
            summary.get("domain_routing_map_count"),
            fallback=len(routes) if isinstance(routes, list) else 0,
        ),
        "deploy_runtime_map": _int_count(
            summary.get("deploy_runtime_unit_count"),
            fallback=_answer_list_len(answer_packet, "deploy_runtime_map"),
        ),
        "unlinked_deploy_leads": _int_count(
            summary.get("candidate_or_unlinked_deploy_lead_count"),
            fallback=_answer_list_len(answer_packet, "unlinked_deploy_leads"),
        ),
        "endpoint_consumer_map": _int_count(
            summary.get("endpoint_consumer_map_count"),
            fallback=_answer_list_len(answer_packet, "endpoint_consumer_map"),
        ),
        "deploy_order_guidance": _int_count(
            summary.get("deploy_order_guidance_count"),
            fallback=_answer_list_len(answer_packet, "deploy_order_guidance"),
        ),
    }


def _int_count(value: object, *, fallback: int) -> int:
    if isinstance(value, bool):
        return fallback
    if isinstance(value, int) and value >= 0:
        return value
    return fallback


def _truncate_runtime_answer(result: JsonObject, *, component_limit: int, route_limit: int) -> None:
    runtime = result.get("runtime_architecture")
    if not isinstance(runtime, dict):
        return
    answer_packet = runtime.get("answer_packet")
    if not isinstance(answer_packet, dict):
        return
    components = answer_packet.get("runtime_building_blocks")
    if isinstance(components, list):
        answer_packet["runtime_building_blocks"] = components[:component_limit]
    routes = answer_packet.get("domain_routing_map")
    if isinstance(routes, list):
        answer_packet["domain_routing_map"] = routes[:route_limit]
    for key in ("deploy_runtime_map", "unlinked_deploy_leads", "endpoint_consumer_map", "deploy_order_guidance"):
        rows = answer_packet.get(key)
        if isinstance(rows, list):
            answer_packet[key] = rows[:route_limit]


def _attach_budget_metadata(
    result: JsonObject,
    *,
    measured_chars: int,
    max_chars: int,
    original_counts: dict[str, int],
    fallback: bool = False,
    minimized: bool = False,
    finalize: bool = True,
) -> None:
    shown_counts = _runtime_answer_shown_counts(result)
    omitted_counts = {
        key: max(0, original_counts.get(key, 0) - shown_counts.get(key, 0))
        for key in (
            "investigation_brief",
            "runtime_building_blocks",
            "domain_routing_map",
            "deploy_runtime_map",
            "unlinked_deploy_leads",
            "endpoint_consumer_map",
            "deploy_order_guidance",
        )
    }
    truncated_sections = []
    if omitted_counts["investigation_brief"] > 0:
        truncated_sections.append(_RUNTIME_INVESTIGATION_BRIEF_PATH)
    if omitted_counts["runtime_building_blocks"] > 0:
        truncated_sections.append(_RUNTIME_COMPONENTS_PATH)
    if omitted_counts["domain_routing_map"] > 0:
        truncated_sections.append(_RUNTIME_ROUTES_PATH)
    if omitted_counts["deploy_runtime_map"] > 0:
        truncated_sections.append(_RUNTIME_DEPLOY_UNITS_PATH)
    if omitted_counts["unlinked_deploy_leads"] > 0:
        truncated_sections.append(_RUNTIME_DEPLOY_LEADS_PATH)
    if omitted_counts["endpoint_consumer_map"] > 0:
        truncated_sections.append(_RUNTIME_CONSUMERS_PATH)
    if omitted_counts["deploy_order_guidance"] > 0:
        truncated_sections.append(_RUNTIME_DEPLOY_GUIDANCE_PATH)
    advice = _planning_budget_advice(result)
    if fallback:
        advice += " The response also dropped non-essential planning sections to preserve a valid JSON packet."
    if minimized:
        advice += " Oversized row payloads were minimized to status, identity, and evidence coordinates."
    result["output_budget"] = {
        "truncated": True,
        "measured_chars": measured_chars,
        "max_chars": max_chars,
        "omitted_counts": omitted_counts,
        "truncated_sections": truncated_sections,
        "advice": advice,
        "fallback": fallback,
        "minimized": minimized,
    }
    if finalize:
        _append_budget_next_action(result, max_chars=max_chars)


def _planning_budget_advice(result: JsonObject) -> str:
    if isinstance(result.get("runtime_architecture"), dict):
        return _PLANNING_BUDGET_ADVICE
    return _PLANNING_GENERIC_BUDGET_ADVICE


def _backfill_planning_context(
    result: JsonObject,
    original_result: JsonObject,
    *,
    measured_chars: int,
    max_chars: int,
    original_counts: dict[str, int],
    fallback: bool = False,
) -> JsonObject:
    candidate = deepcopy(result)
    backfilled_counts: dict[str, int] = {}
    for path in _BUDGET_BACKFILL_LIST_PATHS:
        candidate, added = _backfill_list_path(
            candidate,
            original_result,
            path,
            measured_chars=measured_chars,
            max_chars=max_chars,
            original_counts=original_counts,
            backfilled_counts=backfilled_counts,
            fallback=fallback,
        )
        if added:
            backfilled_counts[_path_label(path)] = backfilled_counts.get(_path_label(path), 0) + added
    return _with_budget_metadata(
        candidate,
        measured_chars=measured_chars,
        max_chars=max_chars,
        original_counts=original_counts,
        backfilled_counts=backfilled_counts,
        fallback=fallback,
    )


def _backfill_list_path(
    candidate: JsonObject,
    original_result: JsonObject,
    path: tuple[str, ...],
    *,
    measured_chars: int,
    max_chars: int,
    original_counts: dict[str, int],
    backfilled_counts: dict[str, int],
    fallback: bool,
) -> tuple[JsonObject, int]:
    source = _nested_list(original_result, path)
    target = _nested_list(candidate, path)
    if source is None or target is None or len(target) >= len(source):
        return candidate, 0
    added = 0
    for row in source[len(target) :]:
        trial_base = deepcopy(candidate)
        trial_target = _nested_list(trial_base, path)
        if trial_target is None:
            break
        trial_target.append(_compact_backfill_row(path, row))
        proposed_counts = dict(backfilled_counts)
        proposed_counts[_path_label(path)] = proposed_counts.get(_path_label(path), 0) + added + 1
        trial = _with_budget_metadata(
            trial_base,
            measured_chars=measured_chars,
            max_chars=max_chars,
            original_counts=original_counts,
            backfilled_counts=proposed_counts,
            fallback=fallback,
        )
        if _current_chars(trial) > max_chars:
            break
        candidate = trial
        added += 1
    return candidate, added


def _with_budget_metadata(
    result: JsonObject,
    *,
    measured_chars: int,
    max_chars: int,
    original_counts: dict[str, int],
    backfilled_counts: dict[str, int],
    fallback: bool,
) -> JsonObject:
    payload = deepcopy(result)
    _attach_budget_metadata(
        payload,
        measured_chars=measured_chars,
        max_chars=max_chars,
        original_counts=original_counts,
        fallback=fallback,
        finalize=False,
    )
    if backfilled_counts:
        payload["output_budget"]["backfilled_counts"] = {
            key: value for key, value in sorted(backfilled_counts.items()) if value > 0
        }
    _append_budget_next_action(payload, max_chars=max_chars)
    return payload


def _nested_list(payload: JsonObject, path: tuple[str, ...]) -> list[object] | None:
    current: object = payload
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current if isinstance(current, list) else None


def _path_label(path: tuple[str, ...]) -> str:
    return ".".join(path)


def _compact_backfill_row(path: tuple[str, ...], row: object) -> object:
    if not isinstance(row, dict):
        return deepcopy(row)
    if path[:3] == ("runtime_architecture", "answer_packet", "investigation_brief"):
        if path[-1] == "recommended_source_checks":
            return _compact_source_check(row)
        return _compact_headstart_row(row)
    if path[:2] == ("runtime_architecture", "answer_packet"):
        return _minimal_runtime_row(row)
    if path[:2] == ("authz_surface", "inspection_areas") or path[:3] == (
        "related_facts",
        "authz_surface",
        "inspection_areas",
    ):
        compacted = _compact_authz_inspection_areas([row])
        return compacted[0] if compacted else {}
    if path[:1] == ("authz_surface",) or path[:2] == ("related_facts", "authz_surface"):
        return deepcopy(row)
    if path[:2] == ("related_facts", "symbol_impact"):
        if path[-1] == "tiers":
            compacted_tiers = _compact_reverse_impact_tiers([row])
            return compacted_tiers[0] if compacted_tiers else {}
        if path[-1] == "terminal_import_consumer_leads":
            compacted_leads = _compact_terminal_import_leads([row])
            return compacted_leads[0] if compacted_leads else {}
        if path[-1] == "truncated_terminal_symbols":
            compacted_symbols = _compact_truncated_terminal_symbols([row])
            return compacted_symbols[0] if compacted_symbols else {}
        if path[-1] == "source_inspection_areas":
            return deepcopy(row)
        return _compact_headstart_or_relation_row(row)
    if path[:1] == ("related_facts",):
        return _compact_headstart_or_relation_row(row)
    return deepcopy(row)


def _current_chars(result: JsonObject) -> int:
    return len(canonical_json(result))


def _runtime_answer_shown_counts(result: JsonObject) -> dict[str, int]:
    runtime = result.get("runtime_architecture")
    if not isinstance(runtime, dict):
        return {"runtime_building_blocks": 0, "domain_routing_map": 0}
    answer_packet = runtime.get("answer_packet")
    if not isinstance(answer_packet, dict):
        return {"runtime_building_blocks": 0, "domain_routing_map": 0}
    components = answer_packet.get("runtime_building_blocks")
    routes = answer_packet.get("domain_routing_map")
    return {
        "investigation_brief": _investigation_brief_anchor_count(answer_packet.get("investigation_brief")),
        "runtime_building_blocks": len(components) if isinstance(components, list) else 0,
        "domain_routing_map": len(routes) if isinstance(routes, list) else 0,
        "deploy_runtime_map": _answer_list_len(answer_packet, "deploy_runtime_map"),
        "unlinked_deploy_leads": _answer_list_len(answer_packet, "unlinked_deploy_leads"),
        "endpoint_consumer_map": _answer_list_len(answer_packet, "endpoint_consumer_map"),
        "deploy_order_guidance": _answer_list_len(answer_packet, "deploy_order_guidance"),
    }


def _planning_context_fallback(result: JsonObject, *, preserve_planning_sections: bool) -> JsonObject:
    runtime = result.get("runtime_architecture")
    compact_runtime: JsonObject = {}
    if isinstance(runtime, dict):
        answer_packet = runtime.get("answer_packet")
        compact_answer: JsonObject = {}
        if isinstance(answer_packet, dict):
            compact_answer = {
                "investigation_brief": _compact_investigation_brief(answer_packet.get("investigation_brief")),
            }
            for key in (
                "runtime_building_blocks",
                "domain_routing_map",
                "deploy_runtime_map",
                "unlinked_deploy_leads",
                "endpoint_consumer_map",
                "deploy_order_guidance",
            ):
                if key in answer_packet:
                    compact_answer[key] = _list_value(answer_packet.get(key))
            for key, default in (
                ("deploy_kind_counts", {}),
                ("missing_fact_families", []),
                ("evidence_contract", None),
                ("omitted_answer_sections", []),
            ):
                if key in answer_packet:
                    compact_answer[key] = answer_packet.get(key, default)
        compact_runtime = {
            "scope": runtime.get("scope", {}),
            "summary": runtime.get("summary", {}),
            "answer_packet": compact_answer,
            "assembly_contract": runtime.get("assembly_contract"),
        }
        anchor_resolution_contract = runtime.get("anchor_resolution_contract")
        if isinstance(anchor_resolution_contract, dict) and anchor_resolution_contract:
            compact_runtime["anchor_resolution_contract"] = anchor_resolution_contract
    fallback = {
        "tool": result.get("tool"),
        "status": result.get("status"),
        "query": result.get("query"),
        "summary": result.get("summary", {}),
        "snapshot_summary": result.get("snapshot_summary", {}),
        "snapshot_scope": result.get("snapshot_scope", {}),
        "ownership_context": _compact_ownership_context(result.get("ownership_context", {})),
        "related_facts": _compact_related_facts(result.get("related_facts", {})),
        "anchors": result.get("anchors", {}),
        "packet_contract": result.get("packet_contract", {}),
        "proven_facts": _compact_common_fact_index(result.get("proven_facts"), omitted_field="omitted_proven_fact_sources"),
        "candidate_leads": _compact_common_fact_index(
            result.get("candidate_leads"), omitted_field="omitted_candidate_lead_sources"
        ),
        "coverage_gaps": _compact_common_coverage_gaps(result.get("coverage_gaps")),
        "inspection_areas": _compact_common_inspection_areas(result.get("inspection_areas")),
        "answerability": result.get("answerability", {}),
        "coverage_warnings": result.get("coverage_warnings", []),
        "unsupported_scopes": result.get("unsupported_scopes", []),
        "next_actions": result.get("next_actions", []),
    }
    if compact_runtime:
        fallback["runtime_architecture"] = compact_runtime
    compact_authz = _compact_authz_surface(result.get("authz_surface", {}))
    if compact_authz:
        fallback["authz_surface"] = compact_authz
    if preserve_planning_sections:
        fallback.update(
            {
                "inventory": result.get("inventory", {}),
                "service_operational_surfaces": _compact_service_operational_surfaces(
                    result.get("service_operational_surfaces", {})
                ),
                "services": result.get("services", []),
                "symbols": result.get("symbols", []),
                "dependencies": result.get("dependencies", []),
                "endpoints": result.get("endpoints", []),
                "endpoint_consumers": result.get("endpoint_consumers", []),
                "event_channels": result.get("event_channels", []),
                "candidate_or_unlinked_event_channels": result.get("candidate_or_unlinked_event_channels", []),
                "domains": result.get("domains", []),
                "entry_points": result.get("entry_points", {}),
                "related_facts": _compact_related_facts(result.get("related_facts", {})),
                "source_coordinates": result.get("source_coordinates", []),
            }
        )
    return {key: value for key, value in fallback.items() if value is not None}


def _compact_related_facts(value: object) -> JsonObject:
    if not isinstance(value, dict):
        return {}
    compact: JsonObject = {}
    inspection_areas: list[JsonObject] = []
    service_brief, service_brief_areas = _compact_service_brief(value.get("service_brief"))
    if service_brief:
        compact["service_brief"] = service_brief
        inspection_areas.extend(service_brief_areas)
    dependency_importers, dependency_areas = _compact_dependency_importers(value.get("dependency_importers"))
    if dependency_importers:
        compact["dependency_importers"] = dependency_importers
        inspection_areas.extend(dependency_areas)
    inventory, inventory_areas = _compact_inventory(value.get("inventory"))
    if inventory:
        compact["inventory"] = inventory
        inspection_areas.extend(inventory_areas)
    service_surfaces = _compact_service_operational_surfaces(value.get("service_operational_surfaces"))
    if service_surfaces:
        compact["service_operational_surfaces"] = service_surfaces
    runtime_reference = _compact_runtime_architecture_reference(value.get("runtime_architecture"))
    if runtime_reference:
        compact["runtime_architecture"] = runtime_reference
    symbol_impact = value.get("symbol_impact")
    if isinstance(symbol_impact, dict):
        compact["symbol_impact"] = _compact_symbol_impact(symbol_impact)
    authz = value.get("authz_surface")
    if isinstance(authz, dict):
        compact["authz_surface"] = _compact_authz_surface(authz)
    for key in (
        "dependencies",
        "endpoints",
        "endpoint_consumers",
        "event_channels",
        "candidate_or_unlinked_event_channels",
        "deploy_mappings",
        "domains",
    ):
        rows, areas = _compact_budgeted_rows(
            value.get(key),
            limit=COMPACT_RUNTIME_HEADSTART_LIMIT,
            row_compactor=_compact_headstart_or_relation_row,
            area=f"related_facts.{key}",
            reason=f"Additional related_facts.{key} rows did not fit in the compact head-start packet.",
        )
        if rows:
            compact[key] = rows
        inspection_areas.extend(areas)
    unknown_keys = sorted(
        key
        for key in value
        if key not in RELATED_FACT_SECTION_KEYS
    )
    if unknown_keys:
        inspection_areas.append(
            {
                "area": "related_facts.omitted_unknown_sections",
                "reason": "Unknown related_facts sections were omitted from budget fallback instead of passing raw payloads through.",
                "trigger": "budget_unknown_section",
                "inspection_refs": [],
                "search_terms": unknown_keys,
                "omitted_section_count": len(unknown_keys),
            }
        )
    if inspection_areas:
        compact["inspection_areas"] = _dedupe_budget_rows(inspection_areas)[:COMPACT_RUNTIME_SOURCE_CHECK_LIMIT]
    return compact


def _compact_service_brief(value: object) -> tuple[JsonObject, list[JsonObject]]:
    if not isinstance(value, dict):
        return {}, []
    compact: JsonObject = {
        "status": value.get("status"),
        "summary": value.get("summary", {}),
    }
    inspection_areas: list[JsonObject] = []
    for key, compactor in (
        ("services", _compact_entity_ref),
        ("endpoints", _compact_headstart_or_relation_row),
        ("endpoint_consumers", _compact_headstart_or_relation_row),
        ("event_channels", _compact_headstart_or_relation_row),
        ("candidate_or_unlinked_event_channels", _compact_headstart_or_relation_row),
        ("deploy_mappings", _compact_headstart_or_relation_row),
    ):
        rows, areas = _compact_budgeted_rows(
            value.get(key),
            limit=COMPACT_RUNTIME_HEADSTART_LIMIT,
            row_compactor=compactor,
            area=f"related_facts.service_brief.{key}",
            reason=f"Additional service brief {key} rows did not fit in the compact head-start packet.",
        )
        if rows:
            compact[key] = rows
        inspection_areas.extend(areas)
    return {key: item for key, item in compact.items() if item not in (None, [], {})}, inspection_areas


def _compact_dependency_importers(value: object) -> tuple[JsonObject, list[JsonObject]]:
    if not isinstance(value, dict):
        return {}, []
    compact: JsonObject = {
        "status": value.get("status"),
        "summary": value.get("summary", {}),
        "package_count": value.get("package_count"),
        "importer_count": value.get("importer_count"),
        "repo_counts": value.get("repo_counts", {}),
        "truncated": value.get("truncated"),
    }
    inspection_areas: list[JsonObject] = []
    for key in ("packages", "importers"):
        rows, areas = _compact_budgeted_rows(
            value.get(key),
            limit=COMPACT_RUNTIME_HEADSTART_LIMIT,
            row_compactor=_compact_headstart_or_relation_row,
            area=f"related_facts.dependency_importers.{key}",
            reason=f"Additional dependency importer {key} rows did not fit in the compact head-start packet.",
        )
        if rows:
            compact[key] = rows
        inspection_areas.extend(areas)
    return {key: item for key, item in compact.items() if item not in (None, [], {})}, inspection_areas


def _compact_inventory(value: object) -> tuple[JsonObject, list[JsonObject]]:
    if not isinstance(value, dict):
        return {}, []
    compact: JsonObject = {
        "scope": value.get("scope", {}),
        "count_contract": value.get("count_contract"),
        "summary": value.get("summary", {}),
        "runtime_counts": value.get("runtime_counts", {}),
    }
    inspection_areas: list[JsonObject] = []
    rows, areas = _compact_budgeted_rows(
        value.get("top_dependencies"),
        limit=COMPACT_RUNTIME_HEADSTART_LIMIT,
        row_compactor=_compact_headstart_or_relation_row,
        area="related_facts.inventory.top_dependencies",
        reason="Additional inventory top dependency rows did not fit in the compact head-start packet.",
    )
    if rows:
        compact["top_dependencies"] = rows
    inspection_areas.extend(areas)
    coverage = value.get("coverage")
    if isinstance(coverage, dict):
        gap_samples, gap_areas = _compact_budgeted_rows(
            coverage.get("gap_samples"),
            limit=COMPACT_RUNTIME_HEADSTART_LIMIT,
            row_compactor=_compact_headstart_or_relation_row,
            area="related_facts.inventory.coverage.gap_samples",
            reason="Additional coverage gap samples did not fit in the compact head-start packet.",
        )
        compact["coverage"] = {
            "state_counts": coverage.get("state_counts", {}),
            "predicate_counts": coverage.get("predicate_counts", {}),
            "reason_counts": coverage.get("reason_counts", {}),
            "gap_samples": gap_samples,
        }
        inspection_areas.extend(gap_areas)
    return {key: item for key, item in compact.items() if item not in (None, [], {})}, inspection_areas


def _compact_runtime_architecture_reference(value: object) -> JsonObject:
    if not isinstance(value, dict):
        return {}
    answer_packet = value.get("answer_packet")
    if not isinstance(answer_packet, dict):
        answer_packet = {}
    compact = {
        "summary": value.get("summary", {}),
        "scope": value.get("scope", {}),
        "deploy_kind_counts": answer_packet.get("deploy_kind_counts", {}),
        "missing_fact_families": answer_packet.get("missing_fact_families", []),
        "evidence_contract": answer_packet.get("evidence_contract"),
        "read_top_level_field": "runtime_architecture.answer_packet",
        "read_for_deploy_runtime": "runtime_architecture.answer_packet.deploy_runtime_map",
        "read_for_candidate_or_unresolved_deploy": "runtime_architecture.answer_packet.unlinked_deploy_leads",
        "read_for_endpoint_consumers": "runtime_architecture.answer_packet.endpoint_consumer_map",
        "read_for_deploy_order": "runtime_architecture.answer_packet.deploy_order_guidance",
    }
    return {key: item for key, item in compact.items() if item not in (None, [], {})}


def _compact_budgeted_rows(
    value: object,
    *,
    limit: int,
    row_compactor: object,
    area: str,
    reason: str,
) -> tuple[list[JsonObject], list[JsonObject]]:
    rows = [row for row in _list_value(value) if isinstance(row, dict)]
    compact_rows = [_call_row_compactor(row_compactor, row) for row in rows[:limit]]
    overflow = rows[limit:]
    inspection_area = _overflow_inspection_area(
        overflow,
        area=area,
        reason=reason,
        omitted_count=len(overflow),
    )
    return [row for row in compact_rows if row], ([inspection_area] if inspection_area else [])


def _call_row_compactor(row_compactor: object, row: JsonObject) -> JsonObject:
    if callable(row_compactor):
        compacted = row_compactor(row)
        return compacted if isinstance(compacted, dict) else {}
    return {}


def _compact_headstart_or_relation_row(row: JsonObject) -> JsonObject:
    compact = _minimal_runtime_row(row)
    if compact:
        return compact
    compact = _compact_headstart_row(row)
    if compact:
        return compact
    compact = _compact_entity_ref(row)
    if compact:
        return compact
    keys = (
        "name",
        "package",
        "import_root",
        "distribution_name",
        "importer_count",
        "state",
        "predicate",
        "reason",
        "scope_ref",
    )
    return {key: row[key] for key in keys if key in row and row[key] is not None}


def _overflow_inspection_area(
    rows: list[JsonObject],
    *,
    area: str,
    reason: str,
    omitted_count: int,
) -> JsonObject:
    if omitted_count <= 0:
        return {}
    refs: list[JsonObject] = []
    search_terms: list[str] = []
    for row in rows:
        refs.extend(_inspection_refs_from_budget_row(row))
        search_terms.extend(_search_terms_from_budget_row(row))
        if len(refs) >= COMPACT_RUNTIME_SOURCE_CHECK_LIMIT and len(search_terms) >= COMPACT_RUNTIME_SOURCE_CHECK_LIMIT:
            break
    inspection_area: JsonObject = {
        "area": area,
        "reason": reason,
        "trigger": "budget_truncated",
        "inspection_refs": _dedupe_budget_rows(refs)[:COMPACT_RUNTIME_SOURCE_CHECK_LIMIT],
        "search_terms": _dedupe_strings(search_terms)[:COMPACT_RUNTIME_SOURCE_CHECK_LIMIT],
        "omitted_row_count": omitted_count,
    }
    if len(refs) > COMPACT_RUNTIME_SOURCE_CHECK_LIMIT:
        inspection_area["inspection_refs_truncated"] = True
        inspection_area["omitted_inspection_ref_count"] = len(refs) - COMPACT_RUNTIME_SOURCE_CHECK_LIMIT
    return inspection_area


def _inspection_refs_from_budget_row(row: JsonObject) -> list[JsonObject]:
    refs: list[JsonObject] = []
    for key in ("source_coordinates", "evidence_coordinates"):
        value = row.get(key)
        if isinstance(value, list):
            refs.extend(_compact_coordinate(item) for item in value if isinstance(item, dict))
    refs.extend(_source_coordinates(row))
    direct_ref = _compact_coordinate(row)
    if direct_ref:
        refs.append(direct_ref)
    for key in ("subject", "object", "caller_symbol", "callee_symbol", "symbol", "handler", "endpoint", "domain"):
        nested = row.get(key)
        if isinstance(nested, dict):
            nested_ref = _compact_coordinate(nested)
            if nested_ref:
                refs.append(nested_ref)
    return [ref for ref in refs if ref]


def _search_terms_from_budget_row(row: JsonObject) -> list[str]:
    terms: list[str] = []
    for key in ("name", "display_name", "qualified_name", "qualname", "package", "path", "domain", "target"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            terms.append(value.strip())
        elif isinstance(value, dict):
            terms.extend(_search_terms_from_budget_row(value))
    for key in ("subject", "object", "caller_symbol", "callee_symbol", "symbol", "handler", "endpoint"):
        nested = row.get(key)
        if isinstance(nested, dict):
            terms.extend(_search_terms_from_budget_row(nested))
        elif isinstance(nested, str) and nested.strip():
            terms.append(nested.strip())
    return terms


def _dedupe_budget_rows(rows: list[JsonObject]) -> list[JsonObject]:
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
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def _compact_symbol_impact(value: JsonObject) -> JsonObject:
    compact: JsonObject = {
        "status": value.get("status"),
        "symbol": _compact_symbol(value.get("symbol")),
        "direct_callers": _compact_relation_rows(value.get("direct_callers"), limit=COMPACT_RUNTIME_HEADSTART_LIMIT),
        "reverse_impact": _compact_reverse_impact(value.get("reverse_impact")),
        "import_consumer_leads": _compact_import_consumer_leads(value.get("import_consumer_leads")),
        "direct_callees": _compact_relation_rows(value.get("direct_callees"), limit=COMPACT_RUNTIME_HEADSTART_LIMIT),
    }
    if value.get("reason") is not None:
        compact["reason"] = value.get("reason")
    return {key: item for key, item in compact.items() if item not in (None, [], {})}


def _compact_reverse_impact(value: object) -> JsonObject:
    if not isinstance(value, dict):
        return {}
    terminal_import_leads = _compact_terminal_import_leads(value.get("terminal_import_consumer_leads"))
    truncated_terminal_symbols = _compact_truncated_terminal_symbols(value.get("truncated_terminal_symbols"))
    source_inspection_areas = _list_value(value.get("source_inspection_areas"))[:COMPACT_RUNTIME_HEADSTART_LIMIT]
    summary = _compact_reverse_impact_summary(value.get("summary"))
    if terminal_import_leads:
        summary["terminal_import_lead_returned_count"] = len(terminal_import_leads)
        summary["terminal_import_lead_total_in_returned_rows"] = sum(
            _safe_non_bool_int(row.get("import_consumer_leads", {}).get("lead_count"))
            for row in terminal_import_leads
        )
    if truncated_terminal_symbols:
        summary["truncated_terminal_symbol_returned_count"] = len(truncated_terminal_symbols)
    compact: JsonObject = {
        "status": value.get("status"),
        "mode": value.get("mode"),
        "depth": value.get("depth"),
        "summary": summary,
        "roots": [_compact_symbol(row) for row in _list_value(value.get("roots"))[:COMPACT_RUNTIME_HEADSTART_LIMIT]],
        "tiers": _compact_reverse_impact_tiers(value.get("tiers")),
        "edges": _compact_relation_rows(value.get("edges"), limit=COMPACT_RUNTIME_HEADSTART_LIMIT),
        "constructor_bridges": _compact_constructor_bridges(value.get("constructor_bridges")),
        "terminal_import_consumer_leads": terminal_import_leads,
        "truncated_terminal_symbols": truncated_terminal_symbols,
        "source_inspection_areas": source_inspection_areas,
        "affected_symbols": _compact_reverse_impact_symbols(value.get("affected_symbols")),
        "call_site_leads": _compact_reverse_impact_symbols(value.get("call_site_leads")),
        "candidate_impact_previews": _compact_candidate_impact_previews(value.get("candidate_impact_previews")),
        "ambiguity_guidance": value.get("ambiguity_guidance"),
        "disambiguation": _compact_disambiguation(value.get("disambiguation")),
        "answerability": value.get("answerability", {}),
        "contract": value.get("contract"),
    }
    return {key: item for key, item in compact.items() if item not in (None, [], {})}


def _compact_reverse_impact_summary(value: object) -> JsonObject:
    if not isinstance(value, dict):
        return {}
    keys = (
        "root_symbol_count",
        "affected_symbol_count",
        "affected_symbol_returned_count",
        "affected_symbols_truncated",
        "call_site_lead_count",
        "call_site_lead_returned_count",
        "edge_count",
        "constructor_bridge_count",
        "constructor_bridge_returned_count",
        "constructor_bridges_truncated",
        "terminal_import_lead_count",
        "terminal_import_lead_returned_count",
        "terminal_import_lead_total_in_returned_rows",
        "terminal_import_leads_truncated",
        "truncated_terminal_symbol_count",
        "truncated_terminal_symbol_returned_count",
        "max_depth_terminal_count",
        "truncated",
        "walk_truncated",
        "roots_unexpanded_count",
        "section_limit",
        "max_depth",
        "edge_multiplicity",
        "affected_symbol_multiplicity",
        "tier_symbol_multiplicity",
        "affected_root_projection",
    )
    return {key: value[key] for key in keys if key in value}


def _compact_reverse_impact_tiers(value: object) -> list[JsonObject]:
    tiers = []
    for tier in _list_value(value)[:COMPACT_RUNTIME_HEADSTART_LIMIT]:
        if not isinstance(tier, dict):
            continue
        symbols = []
        for row in _list_value(tier.get("symbols"))[:COMPACT_RUNTIME_HEADSTART_LIMIT]:
            if not isinstance(row, dict):
                continue
            symbols.append(
                {
                    "depth": row.get("depth"),
                    "symbol": _compact_symbol(row.get("symbol")),
                    "root_symbol": _compact_symbol(row.get("root_symbol")),
                    "root_symbols": [
                        _compact_symbol(symbol)
                        for symbol in _list_value(row.get("root_symbols"))[:COMPACT_RUNTIME_HEADSTART_LIMIT]
                    ],
                }
            )
        tiers.append(
            {
                "depth": tier.get("depth"),
                "symbol_count": tier.get("symbol_count", len(symbols)),
                "symbols": symbols,
            }
        )
    return tiers


def _compact_reverse_impact_symbols(value: object) -> list[JsonObject]:
    symbols = []
    for row in _list_value(value)[:COMPACT_RUNTIME_HEADSTART_LIMIT]:
        if not isinstance(row, dict):
            continue
        symbols.append(
            {
                "depth": row.get("depth"),
                "symbol": _compact_symbol(row.get("symbol")),
                "root_symbol": _compact_symbol(row.get("root_symbol")),
                "root_symbols": [
                    _compact_symbol(symbol)
                    for symbol in _list_value(row.get("root_symbols"))[:COMPACT_RUNTIME_HEADSTART_LIMIT]
                ],
            }
        )
    return symbols


def _compact_constructor_bridges(value: object) -> list[JsonObject]:
    bridges = []
    for row in _list_value(value)[:COMPACT_RUNTIME_HEADSTART_LIMIT]:
        if not isinstance(row, dict):
            continue
        bridges.append(_compact_constructor_bridge(row))
    return bridges


def _compact_constructor_bridge(value: object) -> JsonObject:
    if not isinstance(value, dict):
        return {}
    return {
        "depth": value.get("depth"),
        "bridge_kind": value.get("bridge_kind"),
        "reason": value.get("reason"),
        "from_init": _compact_symbol(value.get("from_init")),
        "to_class": _compact_symbol(value.get("to_class")),
        "root_symbol": _compact_symbol(value.get("root_symbol")),
    }


def _compact_terminal_import_leads(value: object) -> list[JsonObject]:
    rows = []
    for row in _list_value(value)[:COMPACT_RUNTIME_HEADSTART_LIMIT]:
        if not isinstance(row, dict):
            continue
        rows.append(
            {
                "depth": row.get("depth"),
                "terminal_reason": row.get("terminal_reason"),
                "for_symbol": _compact_symbol(row.get("for_symbol")),
                "root_symbol": _compact_symbol(row.get("root_symbol")),
                "import_consumer_leads": _compact_import_consumer_leads(row.get("import_consumer_leads")),
            }
        )
    return rows


def _compact_truncated_terminal_symbols(value: object) -> list[JsonObject]:
    rows = []
    for row in _list_value(value)[:COMPACT_RUNTIME_HEADSTART_LIMIT]:
        if not isinstance(row, dict):
            continue
        rows.append(
            {
                "depth": row.get("depth"),
                "terminal_reason": row.get("terminal_reason"),
                "symbol": _compact_symbol(row.get("symbol")),
                "root_symbol": _compact_symbol(row.get("root_symbol")),
                "inspection_hint": row.get("inspection_hint"),
            }
        )
    return rows


def _compact_import_consumer_leads(value: object) -> JsonObject:
    if not isinstance(value, dict):
        return {}
    return {
        "status": value.get("status"),
        "lead_count": value.get("lead_count"),
        "returned_count": value.get("returned_count"),
        "contract": value.get("contract"),
        "leads": [
            _compact_import_consumer_lead(row)
            for row in _list_value(value.get("leads"))[:COMPACT_RUNTIME_HEADSTART_LIMIT]
            if isinstance(row, dict)
        ],
    }


def _compact_import_consumer_lead(row: JsonObject) -> JsonObject:
    # Import-consumer leads are source-inspection leads, not affected symbols. The
    # full importer_module_symbols inventory is the single largest contributor to
    # oversized reverse-impact packets, so compact it to a count plus a few sample
    # names and keep the importing module's coordinates for targeted inspection.
    # Count from the raw rows and compact only the kept sample, so a large module is not
    # fully materialized just to drop all but two entries.
    raw_symbols = [symbol for symbol in _list_value(row.get("importer_module_symbols")) if isinstance(symbol, dict)]
    sample_symbols = [_compact_symbol(symbol) for symbol in raw_symbols[:2]]
    compact: JsonObject = {
        "lead_kind": row.get("lead_kind"),
        "repo_relation": row.get("repo_relation"),
        "match": row.get("match", {}),
        "importer": _compact_entity_ref(row.get("importer")),
        "imported_module": _compact_entity_ref(row.get("imported_module")),
        "imported_symbol": _compact_symbol(row.get("imported_symbol")),
        # Keep the existing importer_module_symbols field name (truncated to a sample) for
        # schema compatibility, plus a count; the full inventory is the dominant source of
        # packet bloat and is recoverable by inspecting the cited module coordinates.
        "importer_module_symbols": sample_symbols,
        "importer_module_symbol_count": len(raw_symbols),
        "source_coordinates": _source_coordinates(row.get("fact")),
        "interpretation": row.get("interpretation"),
    }
    return compact


def _compact_candidate_impact_previews(value: object) -> list[JsonObject]:
    previews = []
    for row in _list_value(value)[:COMPACT_RUNTIME_HEADSTART_LIMIT]:
        if not isinstance(row, dict):
            continue
        previews.append(
            {
                "symbol": _compact_symbol(row.get("symbol")),
                "impact_preview_rank": row.get("impact_preview_rank"),
                "selection_basis": row.get("selection_basis"),
                "direct_caller_count": row.get("direct_caller_count"),
                "caller_samples": _compact_relation_rows(
                    row.get("caller_samples"),
                    limit=COMPACT_AUTHZ_INSPECTION_REF_LIMIT,
                ),
                "retry_arguments": row.get("retry_arguments", {}),
            }
        )
    return previews


def _compact_disambiguation(value: object) -> JsonObject:
    if not isinstance(value, dict):
        return {}
    retry_arguments = value.get("retry_arguments")
    if isinstance(retry_arguments, list):
        compact_retry_arguments: object = retry_arguments[:COMPACT_RUNTIME_HEADSTART_LIMIT]
    elif isinstance(retry_arguments, dict):
        compact_retry_arguments = retry_arguments
    else:
        compact_retry_arguments = []
    return {
        "status": value.get("status"),
        "reason": value.get("reason"),
        "message": value.get("message"),
        "candidate_count": value.get("candidate_count"),
        "candidates": [
            _compact_symbol(row)
            for row in _list_value(value.get("candidates"))[:COMPACT_RUNTIME_HEADSTART_LIMIT]
        ],
        "retry_arguments": compact_retry_arguments,
    }


def _safe_non_bool_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    return value if isinstance(value, int) else 0


def _compact_relation_rows(value: object, *, limit: int) -> list[JsonObject]:
    rows = []
    for row in _list_value(value)[:limit]:
        if not isinstance(row, dict):
            continue
        compact_row = {
            "lead_id": row.get("lead_id"),
            "lead_kind": row.get("lead_kind"),
            "predicate": row.get("predicate"),
            "depth": row.get("depth"),
            "traversal": row.get("traversal"),
            "subject": _compact_relation_endpoint(row.get("subject")),
            "object": _compact_relation_endpoint(row.get("object")),
            "caller_symbol": _compact_relation_endpoint(row.get("caller_symbol")),
            "callee_symbol": _compact_relation_endpoint(row.get("callee_symbol")),
            "source_coordinates": _source_coordinates(row),
        }
        bridge = _compact_constructor_bridge(row.get("via_constructor_bridge"))
        if bridge:
            compact_row["via_constructor_bridge"] = bridge
        rows.append({k: v for k, v in compact_row.items() if v is not None})
    return rows


def _compact_relation_endpoint(value: object) -> object:
    if isinstance(value, str):
        return value.strip() if value.strip() else {}
    compact = _compact_symbol(value)
    if compact:
        return compact
    compact = _compact_entity_ref(value)
    if compact:
        return compact
    return {}


def _compact_review_backfill_row(path: tuple[str, ...], row: object) -> object:
    if not isinstance(row, dict):
        return deepcopy(row)
    if path[-1] in {"review_hypotheses", "top_review_hypotheses"}:
        return _compact_review_hypothesis(row)
    if path[-1] in {
        "direct_callers",
        "direct_callees",
        "direct_callers_of_changed_symbols",
        "direct_callees_from_changed_symbols",
        "transitive_callers",
        "top_direct_callers",
        "top_direct_callees",
        "top_transitive_callers",
        "repo_dependencies",
    }:
        compacted = _compact_relation_rows([row], limit=1)
        return compacted[0] if compacted else {}
    if path[-1] in {"changed_symbols", "changed_file_symbols", "top_changed_symbols", "changed_file_symbol_inventory"}:
        return _compact_symbol(row)
    if path[-1] in {"diff_anchors", "top_diff_anchors"}:
        return _compact_diff_anchor(row)
    if path[-1] == "source_coordinates":
        return _compact_coordinate(row)
    return _compact_review_nested_row(row)


def _compact_review_nested_row(row: JsonObject) -> JsonObject:
    if _is_call_graph_relation_row(row):
        compacted = _compact_relation_rows([row], limit=1)
        return compacted[0] if compacted else {}
    compact = _compact_headstart_or_relation_row(row)
    if not compact:
        compact = _compact_symbol(row)
    if not compact:
        compact = _compact_entity_ref(row)
    if not compact:
        compact = {key: row[key] for key in ("status", "reason", "predicate", "repo", "path", "name") if key in row}
    coordinates = _source_coordinates(row)
    if coordinates and "source_coordinates" not in compact:
        compact["source_coordinates"] = coordinates
    return compact


def _is_call_graph_relation_row(row: JsonObject) -> bool:
    return any(
        key in row and row[key] is not None
        for key in ("caller_symbol", "callee_symbol", "depth", "traversal", "via_constructor_bridge")
    )


def _compact_symbol(value: object) -> JsonObject:
    if not isinstance(value, dict):
        return {}
    keys = (
        "lead_id",
        "lead_kind",
        "symbol_id",
        "display_name",
        "qualified_name",
        "repo",
        "module",
        "qualname",
        "symbol_kind",
        "path",
        "line",
        "end_line",
        "kind",
        "name",
    )
    return {key: value[key] for key in keys if key in value and value[key] is not None}


def _compact_entity_ref(value: object) -> JsonObject:
    if not isinstance(value, dict):
        return {}
    keys = ("entity_id", "display_name", "kind", "repo", "module", "path", "name")
    return {key: value[key] for key in keys if key in value and value[key] is not None}


def _source_coordinates(value: object) -> list[JsonObject]:
    if not isinstance(value, dict):
        return []
    coordinates = []
    for evidence in _list_value(value.get("evidence")):
        if not isinstance(evidence, dict):
            continue
        bytes_ref = evidence.get("bytes_ref")
        if not isinstance(bytes_ref, dict):
            continue
        coordinates.append(_compact_coordinate(bytes_ref))
        if len(coordinates) >= COMPACT_AUTHZ_INSPECTION_REF_LIMIT:
            break
    return [row for row in coordinates if row]


def _minimize_runtime_answer_rows(result: JsonObject) -> None:
    answer_packet = _runtime_answer_packet(result)
    if answer_packet is None:
        return
    for key in (
        "investigation_brief",
        "runtime_building_blocks",
        "domain_routing_map",
        "deploy_runtime_map",
        "unlinked_deploy_leads",
        "endpoint_consumer_map",
        "deploy_order_guidance",
    ):
        rows = answer_packet.get(key)
        if not isinstance(rows, list):
            continue
        answer_packet[key] = [_minimal_runtime_row(row) for row in rows if isinstance(row, dict)]
    brief = answer_packet.get("investigation_brief")
    if isinstance(brief, dict):
        answer_packet["investigation_brief"] = _compact_investigation_brief(brief)


def _runtime_answer_packet(result: JsonObject) -> JsonObject | None:
    runtime = result.get("runtime_architecture")
    if not isinstance(runtime, dict):
        return None
    answer_packet = runtime.get("answer_packet")
    if not isinstance(answer_packet, dict):
        return None
    return answer_packet


def _minimal_runtime_row(row: JsonObject) -> JsonObject:
    keys = (
        "status",
        "reason",
        "component_id",
        "name",
        "repo",
        "runtime_categories",
        "deploy_kinds",
        "domain",
        "target",
        "services",
        "source",
        "service",
        "candidate_services",
        "subject",
        "object",
        "predicate",
        "qualifier",
        "match_basis",
        "provider",
        "provider_endpoint",
        "matched_provider_endpoint",
        "endpoint",
        "consumer",
        "consumers",
        "consumer_count",
        "deploy_target",
        "deploy_kind",
        "deploy_details",
        "ingress_or_domain_routes",
        "route_source_kind",
        "backend_service",
        "backend_service_ports",
        "ingress_path",
        "recommendation",
        "basis",
        "missing_fact_families",
        "evidence_coordinates",
        "interpretation",
    )
    minimal = {key: row[key] for key in keys if key in row}
    for nested_key in ("consumers", "ingress_or_domain_routes"):
        nested = minimal.get(nested_key)
        if isinstance(nested, list):
            minimal[nested_key] = [_minimal_runtime_row(item) for item in nested if isinstance(item, dict)]
    return minimal


def _compact_service_operational_surfaces(value: object) -> JsonObject:
    if not isinstance(value, dict):
        return {}
    deploy_order = value.get("deploy_order_guidance")
    if not isinstance(deploy_order, dict):
        deploy_order = {}
    partition = value.get("evidence_partition")
    if not isinstance(partition, dict):
        partition = {}
    missing_contracts = partition.get("missing_contracts")
    if not isinstance(missing_contracts, dict):
        missing_contracts = {}
    return {
        "status": value.get("status"),
        "summary": value.get("summary", {}),
        "missing_fact_families": value.get("missing_fact_families", []),
        "evidence_buckets": value.get("evidence_buckets", []),
        "evidence_partition": {
            "known_linked": _compact_partition_counts(partition.get("known_linked")),
            "unlinked_evidence": _compact_partition_counts(partition.get("unlinked_evidence")),
            "missing_contracts": {
                "status": missing_contracts.get("status"),
                "items": missing_contracts.get("items", []),
            },
        },
        "deploy_runtime_units": [
            _minimal_runtime_row(row)
            for row in _list_value(value.get("deploy_runtime_units"))
            if isinstance(row, dict)
        ],
        "deploy_order_guidance": {
            "status": deploy_order.get("status"),
            "inference_contract": deploy_order.get("inference_contract"),
            "proven_endpoint_consumers": [
                _minimal_runtime_row(row)
                for row in _list_value(deploy_order.get("proven_endpoint_consumers"))
                if isinstance(row, dict)
            ],
            "practical_deploy_order": [
                _minimal_runtime_row(row)
                for row in _list_value(deploy_order.get("practical_deploy_order"))
                if isinstance(row, dict)
            ],
            "truncated": deploy_order.get("truncated"),
        },
        "direct_domain_references": _minimal_runtime_rows(value.get("direct_domain_references")),
        "domain_route_candidates": _minimal_runtime_rows(value.get("domain_route_candidates")),
        "deploy_target_candidates": _minimal_runtime_rows(value.get("deploy_target_candidates")),
        "deploy_link_facts": _minimal_runtime_rows(value.get("deploy_link_facts")),
        "endpoint_consumers": _minimal_runtime_rows(value.get("endpoint_consumers")),
        "unlinked_domain_route_samples": _minimal_runtime_rows(value.get("unlinked_domain_route_samples")),
        "candidate_or_unlinked_deploy_links": _minimal_runtime_rows(value.get("candidate_or_unlinked_deploy_links")),
        "coverage_note": value.get("coverage_note"),
    }


def _minimal_runtime_rows(value: object, *, limit: int = 2) -> list[JsonObject]:
    return [_minimal_runtime_row(row) for row in _list_value(value)[:limit] if isinstance(row, dict)]


def _compact_ownership_context(value: object) -> JsonObject:
    if not isinstance(value, dict):
        return {}
    if not value:
        return {}
    answer_packet = value.get("answer_packet")
    compact_answer = {
        "can_answer_owner": None,
        "service_identity": None,
        "proven_owner": None,
        "owner_candidates": [],
        "final_answer_guidance": None,
        "unsupported_promotions": [],
    }
    if isinstance(answer_packet, dict):
        compact_answer = {
            "can_answer_owner": answer_packet.get("can_answer_owner"),
            "service_identity": answer_packet.get("service_identity"),
            "proven_owner": answer_packet.get("proven_owner"),
            "owner_candidates": _list_value(answer_packet.get("owner_candidates"))[:3],
            "final_answer_guidance": answer_packet.get("final_answer_guidance"),
            "unsupported_promotions": _list_value(answer_packet.get("unsupported_promotions"))[:3],
        }
    return {
        "status": value.get("status"),
        "scope": value.get("scope", {}),
        "evidence_contract": value.get("evidence_contract"),
        "answer_packet": compact_answer,
        "proven_owners": _list_value(value.get("proven_owners"))[:3],
        "candidate_maintainers": _list_value(value.get("candidate_maintainers"))[:3],
        "missing_fact_families": value.get("missing_fact_families", []),
        "recommended_source_checks": _list_value(value.get("recommended_source_checks"))[:3],
    }


def _compact_authz_surface(value: object) -> JsonObject:
    if not isinstance(value, dict):
        return {}
    if not value:
        return {}
    compact = {
        "status": value.get("status"),
        "scope": value.get("scope", {}),
        "summary": value.get("summary", {}),
        "answerability": value.get("answerability", {}),
        "assembly_contract": value.get("assembly_contract"),
    }
    for key in AUTHZ_COMPACT_LIST_KEYS:
        rows = _list_value(value.get(key))
        if key == "inspection_areas":
            compact[key] = _compact_authz_inspection_areas(rows[:COMPACT_RUNTIME_HEADSTART_LIMIT])
        elif key == "inspection_index":
            compact[key] = rows[:COMPACT_RUNTIME_SOURCE_CHECK_LIMIT]
        else:
            compact[key] = rows[:COMPACT_RUNTIME_HEADSTART_LIMIT]
    return compact


def _compact_authz_inspection_areas(rows: list[object]) -> list[JsonObject]:
    compact = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        area = dict(row)
        refs = _list_value(area.get("inspection_refs"))
        if refs:
            area["inspection_refs"] = refs[:COMPACT_AUTHZ_INSPECTION_REF_LIMIT]
            omitted = len(refs) - len(area["inspection_refs"])
            if omitted > 0:
                existing_omitted = area.get("omitted_inspection_ref_count")
                if isinstance(existing_omitted, bool) or not isinstance(existing_omitted, int):
                    existing_omitted = 0
                area["omitted_inspection_ref_count"] = existing_omitted + omitted
                area["inspection_refs_truncated"] = True
        compact.append(area)
    return compact


def _compact_investigation_brief(value: object) -> JsonObject:
    if not isinstance(value, dict):
        return {}
    compact = {
        "purpose": value.get("purpose"),
        "usage": value.get("usage"),
        "runtime_anchors": [
            _compact_headstart_row(row)
            for row in _list_value(value.get("runtime_anchors"))[:COMPACT_RUNTIME_HEADSTART_LIMIT]
            if isinstance(row, dict)
        ],
        "known_routes": [
            _compact_headstart_row(row)
            for row in _list_value(value.get("known_routes"))[:COMPACT_RUNTIME_HEADSTART_LIMIT]
            if isinstance(row, dict)
        ],
        "unlinked_runtime_leads": [
            _compact_headstart_row(row)
            for row in _list_value(value.get("unlinked_runtime_leads"))[:COMPACT_RUNTIME_LEAD_LIMIT]
            if isinstance(row, dict)
        ],
        "deploy_units": [
            _compact_headstart_row(row)
            for row in _list_value(value.get("deploy_units"))[:COMPACT_RUNTIME_DEPLOY_UNIT_LIMIT]
            if isinstance(row, dict)
        ],
        "unlinked_deploy_leads": [
            _compact_headstart_row(row)
            for row in _list_value(value.get("unlinked_deploy_leads"))[:COMPACT_RUNTIME_LEAD_LIMIT]
            if isinstance(row, dict)
        ],
        "consumer_links": [
            _compact_headstart_row(row)
            for row in _list_value(value.get("consumer_links"))[:COMPACT_RUNTIME_DEPLOY_UNIT_LIMIT]
            if isinstance(row, dict)
        ],
        "recommended_source_checks": [
            _compact_source_check(row)
            for row in _list_value(value.get("recommended_source_checks"))[:COMPACT_RUNTIME_SOURCE_CHECK_LIMIT]
            if isinstance(row, dict)
        ],
        "missing_fact_families": value.get("missing_fact_families", []),
    }
    return {key: item for key, item in compact.items() if item not in (None, [], {})}


def _compact_headstart_row(row: JsonObject) -> JsonObject:
    keys = (
        "anchor_kind",
        "status",
        "kind",
        "name",
        "repo",
        "slug",
        "type",
        "path",
        "service",
        "runtime_categories",
        "deploy_kinds",
        "counts",
        "domain",
        "target",
        "source",
        "services",
        "deploy_kind",
        "route_source_kind",
        "deploy_target",
        "deploy_details",
        "domains",
        "provider",
        "provider_endpoint",
        "consumers",
        "consumer_count",
        "match_basis",
        "evidence_coordinates",
        "source_coordinates",
        "interpretation",
    )
    compact = {key: row[key] for key in keys if key in row}
    for nested_key in (
        "services",
        "domains",
        "consumers",
    ):
        nested = compact.get(nested_key)
        if isinstance(nested, list):
            compact[nested_key] = [
                _compact_headstart_row(item) if isinstance(item, dict) else item
                for item in nested
            ]
    for coordinate_key in ("evidence_coordinates", "source_coordinates"):
        coordinates = compact.get(coordinate_key)
        if isinstance(coordinates, list):
            compact[coordinate_key] = [
                _compact_coordinate(row)
                for row in coordinates
                if isinstance(row, dict)
            ]
    return compact


def _compact_source_check(row: JsonObject) -> JsonObject:
    keys = ("reason", "anchor", "repo", "path", "line_start", "line_end")
    return {key: row[key] for key in keys if key in row}


def _compact_coordinate(row: JsonObject) -> JsonObject:
    keys = ("lead_id", "lead_kind", "repo", "path", "line_start", "line_end")
    return {key: row[key] for key in keys if key in row}


def _compact_review_hypothesis(row: JsonObject) -> JsonObject:
    compact: JsonObject = {}
    for key in ("hypothesis_id", "risk_type", "confidence", "why"):
        if key in row:
            compact[key] = row[key]
    evidence_refs = row.get("evidence_refs")
    if isinstance(evidence_refs, list):
        compact["evidence_refs"] = evidence_refs[:4]
    source_checks = row.get("source_checks")
    if isinstance(source_checks, list):
        compact["source_checks"] = source_checks[:2]
    supporting_lead_ids = row.get("supporting_lead_ids")
    if isinstance(supporting_lead_ids, list):
        compact["supporting_lead_ids"] = supporting_lead_ids[:5]
    return compact


def _protect_review_hypotheses_floor(
    result: JsonObject, original_hypotheses: list[JsonObject], *, max_chars: int
) -> JsonObject:
    """Ensure at least 1 compacted hypothesis survives when any were originally generated.

    Called after hard-cap or signal-cap passes that may have evicted all hypotheses while
    keeping broad context rows. If the packet has no hypotheses but the original had some,
    inserts the top compacted hypothesis in both the top-level list and the answer-packet
    mirror, and, when the insertion pushes the packet over ``max_chars``, evicts rows from
    the largest other row list until it fits again — broad context is dropped before the
    last hypothesis, per the review budget priority order.
    """
    if not original_hypotheses:
        return result
    current = result.get("review_hypotheses")
    if isinstance(current, list) and current:
        return result
    # Restore top 1 compacted hypothesis.
    top_compact = _compact_review_hypothesis(original_hypotheses[0])
    result = deepcopy(result)
    result["review_hypotheses"] = [top_compact]
    # Restore mirror floor too.
    answer_packet = result.get("review_answer_packet")
    if isinstance(answer_packet, dict):
        mirror = answer_packet.get("top_review_hypotheses")
        if not isinstance(mirror, list) or not mirror:
            answer_packet["top_review_hypotheses"] = [top_compact]
    truncated: set[str] = set()
    budget = result.get("output_budget")
    if isinstance(budget, dict):
        truncated = set(budget.get("truncated_sections") or [])
        if len(original_hypotheses) > 1:
            truncated.add("review_hypotheses")
            truncated.add("review_answer_packet.top_review_hypotheses")
        else:
            truncated.discard("review_hypotheses")
            truncated.discard("review_answer_packet.top_review_hypotheses")
    # Compensating eviction: the restored hypotheses must never push the packet over the cap.
    evicted = _evict_review_rows_to_fit(result, max_chars=max_chars)
    truncated |= evicted
    if evicted:
        _sync_review_lead_status_from_packet(result)
    if isinstance(budget, dict):
        budget["truncated_sections"] = sorted(truncated)
    return result


_REVIEW_BROAD_CONTEXT_KEYS = frozenset(
    {
        "application_impact",
        "framework_impact",
        "runtime_surfaces",
        "impact",
    }
)


def _is_broad_context_label(label: str) -> bool:
    """Return True if the dotted path label is a broad application/runtime/framework section.

    Both the top-level keys (e.g. ``application_impact``) and their mirrors inside
    ``review_answer_packet`` (e.g. ``review_answer_packet.application``) qualify as
    broad-context victims, so hypotheses can be funded before inspecting signal-heavy
    lead/coordinate/anchor sections.
    """
    root = label.split(".")[0]
    if root in _REVIEW_BROAD_CONTEXT_KEYS:
        return True
    if root == "review_answer_packet":
        inner = label.split(".", 2)[1] if label.count(".") >= 1 else ""
        broad_ap_keys = {"application", "framework", "runtime", "runtime_surfaces", "framework_impact", "application_impact"}
        return inner in broad_ap_keys
    return False


def _evict_broad_context_to_fit(result: JsonObject, *, max_chars: int) -> set[str]:
    """Evict rows from broad application/runtime/framework sections until the packet fits.

    N2 targeted eviction: only broad-context lists (application_impact, framework_impact,
    runtime_surfaces, and their answer-packet mirrors) are shrunk. Hypothesis lists, lead
    rows, coordinates, and anchors are untouched. Returns the labels of lists that lost rows.
    """
    evicted: set[str] = set()
    guard = 0
    while _current_chars(result) > max_chars and guard < 5_000:
        guard += 1
        # Protect hypotheses while finding the broad-context victim
        protected = result.pop("review_hypotheses", None)
        answer_packet = result.get("review_answer_packet")
        protected_mirror = None
        if isinstance(answer_packet, dict):
            protected_mirror = answer_packet.pop("top_review_hypotheses", None)
        # Only consider broad-context lists as victims
        target = _largest_broad_context_row_list(result)
        if protected is not None:
            result["review_hypotheses"] = protected
        if isinstance(answer_packet, dict) and protected_mirror is not None:
            answer_packet["top_review_hypotheses"] = protected_mirror
        if target is None:
            break
        label, rows = target
        rows.pop()
        evicted.add(label)
    return evicted


def _largest_broad_context_row_list(node: object) -> tuple[str, list] | None:
    """Find the largest broad-context (application/runtime/framework) list in the packet.

    Only searches inside ``_REVIEW_BROAD_CONTEXT_KEYS`` sections and their
    ``review_answer_packet.*`` mirrors, leaving lead/coordinate/anchor sections untouched.
    """
    best_size = 0
    best: tuple[str, list] | None = None
    stack: list[tuple[str, object]] = [("", node)]
    while stack:
        label, current = stack.pop()
        if isinstance(current, dict):
            for key, value in current.items():
                if key in _HARD_CAP_PROTECTED_KEYS or (not label and key in _HARD_CAP_PROTECTED_TOPLEVEL):
                    continue
                child_label = f"{label}.{key}" if label else key
                if isinstance(value, list) and value and any(isinstance(x, dict) for x in value):
                    if _is_broad_context_label(child_label):
                        size = len(canonical_json(value))
                        if size > best_size:
                            best_size, best = size, (child_label, value)
                # Only recurse into broad-context subtrees (or root dict)
                if not label or _is_broad_context_label(child_label) or label == "review_answer_packet":
                    stack.append((child_label, value))
        elif isinstance(current, list):
            for index, item in enumerate(current):
                stack.append((f"{label}[{index}]", item))
    return best


def _is_review_leads_label(label: str) -> bool:
    """Return True if the dotted label is a review_leads or answer-packet top_* field.

    These are the lead rows that pair with answer-packet mirror rows. N1: they are
    evicted last (after all other non-hypothesis victims are exhausted) so the pair
    survives; when they must be evicted, the coherent drop of both sides is handled
    by the subsequent _sync_review_lead_status_from_packet call.
    """
    root = label.split(".")[0]
    if root == "review_leads":
        return True
    if root == "review_answer_packet":
        inner = label.split(".", 2)[1] if label.count(".") >= 1 else ""
        return inner in {"top_changed_symbols", "top_direct_callers", "top_direct_callees", "top_transitive_callers"}
    return False


def _largest_non_lead_row_list(node: object) -> tuple[str, list] | None:
    """Find the largest row-list that is NOT a review_leads/top_* lead section or hypothesis list.

    Used by N1 to prefer evicting other sections (broad context, anchors, etc.) before
    touching the lead pairs that must stay coherent.
    """
    best_size = 0
    best: tuple[str, list] | None = None
    stack: list[tuple[str, object]] = [("", node)]
    while stack:
        label, current = stack.pop()
        if isinstance(current, dict):
            for key, value in current.items():
                if key in _HARD_CAP_PROTECTED_KEYS or (not label and key in _HARD_CAP_PROTECTED_TOPLEVEL):
                    continue
                child_label = f"{label}.{key}" if label else key
                if isinstance(value, list) and value and any(isinstance(x, dict) for x in value):
                    if not _is_review_leads_label(child_label):
                        size = len(canonical_json(value))
                        if size > best_size:
                            best_size, best = size, (child_label, value)
                stack.append((child_label, value))
        elif isinstance(current, list):
            for index, item in enumerate(current):
                stack.append((f"{label}[{index}]", item))
    return best


def _evict_review_rows_to_fit(result: JsonObject, *, max_chars: int) -> set[str]:
    """Evict rows from the largest non-hypothesis row list until the packet fits.

    N1 (flip): review_leads and answer-packet top_* lead rows are evicted last —
    only after all other non-hypothesis victims (broad context, anchors, diff rows)
    are exhausted. When a review_leads row must be evicted as a last resort, the
    subsequent _sync_review_lead_status_from_packet call clips the corresponding
    top_* mirror entry too (coherent drop of both sides). The subset invariant
    (top_* ⊆ review_leads) is maintained throughout.

    Both ``review_hypotheses`` and ``review_answer_packet.top_review_hypotheses`` are
    protected: broad context rows are dropped before the last hypothesis, per the review
    budget priority order. Mutates ``result`` in place and returns the labels of lists that
    lost rows.
    """
    evicted: set[str] = set()
    guard = 0
    while _current_chars(result) > max_chars and guard < 5_000:
        guard += 1
        protected = result.pop("review_hypotheses", None)
        answer_packet = result.get("review_answer_packet")
        protected_mirror = None
        if isinstance(answer_packet, dict):
            protected_mirror = answer_packet.pop("top_review_hypotheses", None)
        # N1: prefer non-lead victims; fall back to lead rows only when no other victims exist.
        target = _largest_non_lead_row_list(result)
        if target is None:
            target = _largest_row_list(result)
        if protected is not None:
            result["review_hypotheses"] = protected
        if isinstance(answer_packet, dict) and protected_mirror is not None:
            answer_packet["top_review_hypotheses"] = protected_mirror
        if target is None:
            break
        label, rows = target
        rows.pop()
        evicted.add(label)
        # Tandem clipping: when a top-level detail list is evicted, pop the matching
        # row from review_leads so the re-mirror at the end of finalize is guaranteed
        # to be shrink-or-equal (never growth after the last cap check).
        _TOPLEVEL_TO_REVIEW_LEADS: dict[str, str] = {
            "changed_symbols": "changed_symbols",
            "direct_callers": "direct_callers",
            "direct_callees": "direct_callees",
            "transitive_callers": "transitive_callers",
        }
        if label in _TOPLEVEL_TO_REVIEW_LEADS:
            rl = result.get("review_leads")
            if isinstance(rl, dict):
                rl_field = _TOPLEVEL_TO_REVIEW_LEADS[label]
                rl_rows = rl.get(rl_field)
                if isinstance(rl_rows, list) and rl_rows and rl_rows is not rows:
                    rl_rows.pop()
                evicted.add(f"review_leads.{rl_field}")
            _sync_review_lead_status_from_packet(result)
        # N1 coherent drop: if we just evicted a review_leads row, sync the mirror
        # immediately so the top_* entry is also removed — never leave mirror showing
        # a lead_id that no longer exists in review_leads.
        elif _is_review_leads_label(label):
            _sync_review_lead_status_from_packet(result)
    return evicted


def _attach_review_hypothesis_status(result: JsonObject, original_hypotheses: list[JsonObject]) -> None:
    """Write review_hypothesis_status in-place on result.

    Emits the status dict so consumers can distinguish none-generated, budget-dropped,
    mirror-only-dropped, and low-coverage paths without inspecting truncated_sections.
    Exception: when even this status object would push the packet over max_chars and no
    other rows are evictable, _finalize_review_hypothesis_budget drops it — the hard cap
    always wins over metadata.

    Fields:
      available_count — hypotheses generated pre-budget
      returned_count — top-level survivors
      answer_packet_returned_count — mirror survivors in review_answer_packet.top_review_hypotheses
      truncated_count — available - returned
      reason — "none_generated" | "budget" | "low_coverage" | null
      available_risk_types — sorted list of risk_type strings from the pre-budget generated set
        (not truncated by budget; bounded at 12 entries; empty when no hypotheses generated)
    """
    _MAX_AVAILABLE_RISK_TYPES = 12
    available = len(original_hypotheses)
    returned = len([h for h in _list_value(result.get("review_hypotheses")) if isinstance(h, dict)])
    answer_packet = result.get("review_answer_packet")
    if isinstance(answer_packet, dict):
        mirror_hyps = answer_packet.get("top_review_hypotheses")
        mirror_count = len([h for h in _list_value(mirror_hyps) if isinstance(h, dict)])
    else:
        mirror_count = 0
    truncated_count = available - returned
    coverage_status = ""
    lead_status = result.get("review_lead_status")
    if isinstance(lead_status, dict):
        coverage_status = lead_status.get("coverage_status") or ""
    if available == 0 and coverage_status == "low_coverage":
        reason: str | None = "low_coverage"
    elif available == 0:
        reason = "none_generated"
    elif truncated_count > 0 or mirror_count < returned:
        reason = "budget"
    else:
        reason = None
    available_risk_types = sorted(
        {str(h.get("risk_type") or "") for h in original_hypotheses if isinstance(h, dict) and h.get("risk_type")}
    )[:_MAX_AVAILABLE_RISK_TYPES]
    result["review_hypothesis_status"] = {
        "available_count": available,
        "returned_count": returned,
        "answer_packet_returned_count": mirror_count,
        "truncated_count": truncated_count,
        "reason": reason,
        "available_risk_types": available_risk_types,
    }


def _collect_surviving_lead_ids(result: JsonObject) -> set[str]:
    """Return the set of lead_ids present in review_leads after all budget eviction."""
    surviving: set[str] = set()
    review_leads = result.get("review_leads")
    if not isinstance(review_leads, dict):
        return surviving
    for rows in review_leads.values():
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict):
                    lead_id = row.get("lead_id")
                    if isinstance(lead_id, str) and lead_id:
                        surviving.add(lead_id)
    return surviving


def _reconcile_hypothesis_lead_ids(result: JsonObject) -> None:
    """Filter supporting_lead_ids in hypotheses to only ids that survived budget eviction.

    hypothesis_id is computed at producer time from the pre-budget lead set and must NOT
    be recomputed here — attribution stability across compaction is the contract. Hypotheses
    whose lead list empties are kept; evidence_refs still guide inspection.
    """
    surviving = _collect_surviving_lead_ids(result)
    for hyp_list_key in ("review_hypotheses",):
        hyp_list = result.get(hyp_list_key)
        if not isinstance(hyp_list, list):
            continue
        for hyp in hyp_list:
            if not isinstance(hyp, dict):
                continue
            lead_ids = hyp.get("supporting_lead_ids")
            if isinstance(lead_ids, list):
                hyp["supporting_lead_ids"] = [lid for lid in lead_ids if lid in surviving]
    answer_packet = result.get("review_answer_packet")
    if isinstance(answer_packet, dict):
        top_hyps = answer_packet.get("top_review_hypotheses")
        if isinstance(top_hyps, list):
            for hyp in top_hyps:
                if not isinstance(hyp, dict):
                    continue
                lead_ids = hyp.get("supporting_lead_ids")
                if isinstance(lead_ids, list):
                    hyp["supporting_lead_ids"] = [lid for lid in lead_ids if lid in surviving]


def _group_changed_symbols_by_cluster(
    review_leads: JsonObject,
) -> dict[str, list[int]]:
    """Group changed_symbol row indices in review_leads by their 'path' field.

    Returns {path: [index, ...]} so callers can reorder rows by cluster.
    """
    clusters: dict[str, list[int]] = {}
    rows = review_leads.get("changed_symbols")
    if not isinstance(rows, list):
        return clusters
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        path = row.get("path") or ""
        if path not in clusters:
            clusters[path] = []
        clusters[path].append(i)
    return clusters


def _cluster_rank_key(
    path: str,
    cluster_lead_ids: list[str],
    hyp_supporting_ids: set[str],
    edge_count: int,
) -> tuple[int, int, str]:
    """Deterministic rank for a changed-file cluster.

    Referenced-by-hypothesis clusters rank first (0), then by descending edge
    count, then by path for tie-breaking. Lower tuple = higher rank = kept first.
    """
    referenced = any(lid in hyp_supporting_ids for lid in cluster_lead_ids)
    return (0 if referenced else 1, -edge_count, path)


def _snapshot_edge_counts(snapshot_review_leads: JsonObject) -> dict[str, int]:
    """Count direct caller/callee edges per path from a pre-budget snapshot.

    Used to keep re-interleave ranking consistent with the repair and P2 passes,
    which also rank from the snapshot. Current compacted rows may have had edge rows
    evicted, so ranking from them can give different orderings across the finalize loop.
    """
    counts: dict[str, int] = {}
    for field in ("direct_callers", "direct_callees"):
        for row in _list_value(snapshot_review_leads.get(field)):
            if isinstance(row, dict):
                p = row.get("path") or ""
                counts[p] = counts.get(p, 0) + 1
    return counts


def _interleave_changed_symbols_by_cluster(
    result: JsonObject,
    *,
    snapshot_edge_counts: dict[str, int] | None = None,
) -> None:
    """Reorder changed-symbol lead rows so rows interleave across clusters.

    Cluster key: the changed-file ``path`` field on each row. One row from each
    cluster is placed before any second row from any cluster, etc. (round-robin by
    rank). Downstream row-limit sampling (``rows[:limit]`` in _compact_review_detail),
    backfill order, and the tail-eviction logic in _evict_review_rows_to_fit then all
    naturally preserve cluster coverage: the head of the list is one anchor per
    cluster in rank order, so limits keep anchors first and tail pops drop the
    lowest-priority extras first, then whole lowest-ranked clusters.

    Reorders both ``review_leads.changed_symbols`` and the top-level
    ``changed_symbols`` detail list (the compaction passes sample the top-level list
    and sync it back into review_leads). Non-dict rows are preserved at the tail.

    Cluster rank: hypothesis-referenced clusters first, then (descending direct-edge
    count, ascending path). Deterministic.

    ``snapshot_edge_counts``: when provided, use these pre-budget edge counts for
    ranking instead of the current (possibly compacted) edge rows in ``result``.
    This keeps re-interleave ranking consistent with the repair/P2 passes that also
    rank from the snapshot.

    Mutates result in place.
    """
    # Collect supporting_lead_ids from current hypotheses
    hyp_supporting: set[str] = set()
    for hyp in _list_value(result.get("review_hypotheses")):
        if isinstance(hyp, dict):
            for lid in _list_value(hyp.get("supporting_lead_ids")):
                if isinstance(lid, str):
                    hyp_supporting.add(lid)

    if snapshot_edge_counts is not None:
        edge_counts = snapshot_edge_counts
    else:
        # Count direct edges per path to rank clusters with no hypothesis reference.
        # Prefer review_leads edge rows; fall back to the top-level detail lists.
        review_leads = result.get("review_leads")
        edge_source: JsonObject = review_leads if isinstance(review_leads, dict) else result
        edge_counts = {}
        for field in ("direct_callers", "direct_callees"):
            rows = edge_source.get(field)
            if not isinstance(rows, list):
                rows = result.get(field) if isinstance(result.get(field), list) else []
            for row in rows:
                if isinstance(row, dict):
                    p = row.get("path") or ""
                    edge_counts[p] = edge_counts.get(p, 0) + 1

    review_leads = result.get("review_leads")
    if isinstance(review_leads, dict) and isinstance(review_leads.get("changed_symbols"), list):
        review_leads["changed_symbols"] = _interleaved_cluster_order(
            review_leads["changed_symbols"], hyp_supporting=hyp_supporting, edge_counts=edge_counts
        )
    if isinstance(result.get("changed_symbols"), list):
        result["changed_symbols"] = _interleaved_cluster_order(
            result["changed_symbols"], hyp_supporting=hyp_supporting, edge_counts=edge_counts
        )


def _interleaved_cluster_order(
    rows: list, *, hyp_supporting: set[str], edge_counts: dict[str, int]
) -> list:
    """Return rows reordered round-robin across path clusters in cluster-rank order.

    Within-cluster order is preserved. Non-dict rows are appended at the tail in
    their original order. Single-cluster lists are returned unchanged.
    """
    if len(rows) <= 1:
        return rows
    cluster_indices: dict[str, list[int]] = {}
    other_indices: list[int] = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            other_indices.append(i)
            continue
        path = row.get("path") or ""
        if path not in cluster_indices:
            cluster_indices[path] = []
        cluster_indices[path].append(i)
    if len(cluster_indices) <= 1:
        return rows

    def _rank(path: str) -> tuple[int, int, str]:
        lead_ids = [rows[i].get("lead_id") or "" for i in cluster_indices[path]]
        return _cluster_rank_key(path, lead_ids, hyp_supporting, edge_counts.get(path, 0))

    ranked_paths = sorted(cluster_indices.keys(), key=_rank)
    interleaved_indices: list[int] = []
    iterators = [iter(cluster_indices[p]) for p in ranked_paths]
    active = list(range(len(ranked_paths)))
    while active:
        next_active = []
        for slot in active:
            idx = next(iterators[slot], None)
            if idx is not None:
                interleaved_indices.append(idx)
                next_active.append(slot)
        active = next_active
    interleaved_indices.extend(other_indices)
    return [rows[i] for i in interleaved_indices]


def _build_truncation_summary(
    result: JsonObject,
    original_review_leads: JsonObject,
    original_hypotheses: list[JsonObject],
) -> JsonObject:
    """Build P2 truncation_summary from pre-budget lead counts vs current packet.

    ``original_review_leads`` must be the pre-budget snapshot captured before any
    compaction pass, so omitted counts reflect everything the reviewer lost — not just
    finalize-stage eviction. ``original_hypotheses`` are the pre-budget hypotheses,
    whose supporting_lead_ids still reference dropped leads (the in-packet copies are
    reconciled to surviving leads only, which would erase risk_family attribution for
    dropped clusters). Returns the summary dict. Bounded: <= 5 cluster rows.
    """
    review_leads = result.get("review_leads")
    if not isinstance(review_leads, dict):
        review_leads = {}

    def _len_field(leads: JsonObject, field: str) -> int:
        v = leads.get(field)
        return len(v) if isinstance(v, list) else 0

    omitted_changed_symbol_count = max(
        0, _len_field(original_review_leads, "changed_symbols") - _len_field(review_leads, "changed_symbols")
    )
    omitted_direct_caller_count = max(
        0, _len_field(original_review_leads, "direct_callers") - _len_field(review_leads, "direct_callers")
    )
    omitted_direct_callee_count = max(
        0, _len_field(original_review_leads, "direct_callees") - _len_field(review_leads, "direct_callees")
    )
    omitted_transitive_caller_count = max(
        0, _len_field(original_review_leads, "transitive_callers") - _len_field(review_leads, "transitive_callers")
    )

    # Find clusters whose changed_symbol rows were entirely omitted
    original_cs = [r for r in _list_value(original_review_leads.get("changed_symbols")) if isinstance(r, dict)]
    retained_cs = [r for r in _list_value(review_leads.get("changed_symbols")) if isinstance(r, dict)]
    retained_lead_ids: set[str] = {r.get("lead_id") or "" for r in retained_cs}

    # Group original rows by cluster (file path)
    original_by_cluster: dict[str, list[dict]] = {}
    for row in original_cs:
        p = row.get("path") or ""
        if p not in original_by_cluster:
            original_by_cluster[p] = []
        original_by_cluster[p].append(row)

    # Collect hypothesis supporting ids to determine risk_family. Use the pre-budget
    # hypotheses: in-packet copies had supporting_lead_ids reconciled to surviving leads,
    # so dropped clusters would never match them.
    hyp_supporting_by_risk: dict[str, set[str]] = {}
    for hyp in original_hypotheses:
        if not isinstance(hyp, dict):
            continue
        rt = str(hyp.get("risk_type") or "")
        for lid in _list_value(hyp.get("supporting_lead_ids")):
            if isinstance(lid, str):
                if rt not in hyp_supporting_by_risk:
                    hyp_supporting_by_risk[rt] = set()
                hyp_supporting_by_risk[rt].add(lid)

    # Edge count per path
    edge_counts: dict[str, int] = {}
    for field in ("direct_callers", "direct_callees"):
        for row in _list_value(original_review_leads.get(field)):
            if isinstance(row, dict):
                p = row.get("path") or ""
                edge_counts[p] = edge_counts.get(p, 0) + 1

    # Clusters dropped entirely
    dropped_clusters: list[tuple[tuple[int, int, str], str, list[dict]]] = []
    for path, orig_rows in original_by_cluster.items():
        if all((r.get("lead_id") or "") not in retained_lead_ids for r in orig_rows):
            orig_ids = {r.get("lead_id") or "" for r in orig_rows}
            hyp_ids = {lid for s in hyp_supporting_by_risk.values() for lid in s}
            rank = _cluster_rank_key(path, list(orig_ids), hyp_ids, edge_counts.get(path, 0))
            dropped_clusters.append((rank, path, orig_rows))

    # Sort by rank (highest-priority dropped clusters first in summary)
    dropped_clusters.sort(key=lambda t: t[0])

    omitted_high_risk_clusters: list[JsonObject] = []
    for _, path, orig_rows in dropped_clusters[:5]:
        orig_ids_list = [r.get("lead_id") or "" for r in orig_rows if r.get("lead_id")]
        orig_ids_set = set(orig_ids_list)
        risk_family = None
        for rt, lids in sorted(hyp_supporting_by_risk.items()):
            if orig_ids_set & lids:
                risk_family = rt
                break
        n_edges = edge_counts.get(path, 0)
        if risk_family:
            reason = f"referenced by hypothesis {risk_family}"
        elif n_edges:
            reason = f"{n_edges} direct edges"
        else:
            reason = "changed file with no retained symbols"
        rep_syms = [
            r.get("qualname") or r.get("name") or ""
            for r in orig_rows[:3]
            if isinstance(r, dict) and (r.get("qualname") or r.get("name"))
        ]
        omitted_high_risk_clusters.append({
            "risk_family": risk_family,
            "changed_files": [path],
            "representative_symbols": rep_syms[:3],
            "representative_lead_ids": orig_ids_list[:3],
            "reason_ranked_high": reason,
        })

    return {
        "omitted_changed_symbol_count": omitted_changed_symbol_count,
        "omitted_direct_caller_count": omitted_direct_caller_count,
        "omitted_direct_callee_count": omitted_direct_callee_count,
        "omitted_transitive_caller_count": omitted_transitive_caller_count,
        "omitted_high_risk_clusters": omitted_high_risk_clusters,
    }


def _repair_cluster_coverage(
    result: JsonObject,
    original_review_leads: JsonObject,
    original_hypotheses: list[JsonObject],
    *,
    max_chars: int,
) -> bool:
    """P1 guarantee: restore >= 1 changed-symbol anchor per changed-file cluster.

    Cluster key: the changed-file ``path`` on review_leads.changed_symbols rows.
    The interleave ordering makes the row-limit and tail-eviction passes cluster-aware,
    but the signal hard cap re-ranks rows by score and can leave one cluster holding
    multiple rows while another holds none. This step enforces the binding rule
    directly against the pre-budget snapshot: while an original cluster has no retained
    anchor, swap in its first original row (compacted) by removing one row from a donor
    cluster — the cluster with the most retained rows, tie-broken toward the
    lowest-ranked cluster. When no cluster holds a 2nd row, a covered cluster that
    ranks BELOW the uncovered one gives up its only anchor instead (whole lowest-ranked
    clusters are dropped, not high-ranked ones). Swaps are approximately size-neutral;
    the caller re-checks the cap afterwards.

    Cluster rank: hypothesis-referenced (pre-budget hypotheses) first, then
    (descending direct-edge count, ascending path). Returns True when rows changed.
    """
    review_leads = result.get("review_leads")
    if not isinstance(review_leads, dict):
        return False
    retained = review_leads.get("changed_symbols")
    if not isinstance(retained, list):
        return False
    original_rows = [
        row for row in _list_value(original_review_leads.get("changed_symbols")) if isinstance(row, dict)
    ]
    if not original_rows:
        return False
    original_by_cluster: dict[str, list[JsonObject]] = {}
    for row in original_rows:
        path = row.get("path") or ""
        if path not in original_by_cluster:
            original_by_cluster[path] = []
        original_by_cluster[path].append(row)
    if len(original_by_cluster) <= 1:
        return False
    hyp_supporting: set[str] = set()
    for hyp in original_hypotheses:
        if isinstance(hyp, dict):
            for lid in _list_value(hyp.get("supporting_lead_ids")):
                if isinstance(lid, str):
                    hyp_supporting.add(lid)
    edge_counts: dict[str, int] = {}
    for field in ("direct_callers", "direct_callees"):
        for row in _list_value(original_review_leads.get(field)):
            if isinstance(row, dict):
                path = row.get("path") or ""
                edge_counts[path] = edge_counts.get(path, 0) + 1

    def _rank(path: str) -> tuple[int, int, str]:
        lead_ids = [row.get("lead_id") or "" for row in original_by_cluster[path]]
        return _cluster_rank_key(path, lead_ids, hyp_supporting, edge_counts.get(path, 0))

    retained_counts: dict[str, int] = {}
    for row in retained:
        if isinstance(row, dict):
            path = row.get("path") or ""
            retained_counts[path] = retained_counts.get(path, 0) + 1
    changed = False
    for path in sorted(original_by_cluster, key=_rank):
        if retained_counts.get(path, 0) > 0:
            continue
        donors = [p for p, count in retained_counts.items() if count >= 2 and p in original_by_cluster]
        donor: str | None = None
        if donors:
            donor = max(donors, key=lambda p: (retained_counts[p], _rank(p)))
        else:
            singles = [p for p, count in retained_counts.items() if count == 1 and p in original_by_cluster]
            if singles:
                worst = max(singles, key=_rank)
                if _rank(worst) > _rank(path):
                    donor = worst
        if donor is None:
            # The best-ranked uncovered cluster cannot be funded; lower-ranked ones
            # cannot either — they stay dropped whole and P2 records them.
            break
        for index in range(len(retained) - 1, -1, -1):
            row = retained[index]
            if isinstance(row, dict) and (row.get("path") or "") == donor:
                del retained[index]
                retained_counts[donor] -= 1
                break
        retained.append(_compact_symbol(original_by_cluster[path][0]))
        retained_counts[path] = retained_counts.get(path, 0) + 1
        changed = True
    if changed:
        # Keep the top-level changed_symbols detail list mirrored with review_leads
        # (the compaction passes keep them in sync; repair must not desync them).
        if isinstance(result.get("changed_symbols"), list):
            result["changed_symbols"] = list(retained)
        # Re-cluster the order so any later tail eviction drops extras before anchors.
        # Use snapshot-derived edge counts (same source repair/P2 already rank from)
        # so ranking is consistent regardless of which edge rows survived compaction.
        _interleave_changed_symbols_by_cluster(result, snapshot_edge_counts=edge_counts)
    return changed


_REVIEW_HYPOTHESIS_HEADROOM_TARGET = 3


def _restore_hypotheses_up_to_target(
    result: JsonObject,
    original_hypotheses: list[JsonObject],
    *,
    max_chars: int,
    target: int,
) -> set[str]:
    """N2: Evict broad-context rows to fund up to ``target`` compacted hypotheses.

    Priority order: hypotheses > leads > coordinates > anchors > inspection areas >
    broad context. This step only evicts from broad application/runtime/framework sections
    (and their answer-packet mirrors) — never from lead/coordinate/anchor sections.
    Returns the set of section labels that had rows evicted.
    """
    if not original_hypotheses:
        return set()
    current_hyps = [h for h in _list_value(result.get("review_hypotheses")) if isinstance(h, dict)]
    if len(current_hyps) >= min(target, len(original_hypotheses)):
        return set()
    evicted: set[str] = set()
    # Try to restore hypotheses one by one (compacted), evicting broad context as needed.
    for hyp in original_hypotheses[len(current_hyps) : target]:
        compact_hyp = _compact_review_hypothesis(hyp)
        hyp_cost = len(canonical_json(compact_hyp))
        # Evict broad-context rows until there is room for this hypothesis.
        broad_evicted = _evict_broad_context_to_fit(
            result, max_chars=max(1, max_chars - hyp_cost - _HARD_CAP_AREA_RESERVE // 4)
        )
        evicted |= broad_evicted
        # Re-check budget after broad eviction.
        if _current_chars(result) + hyp_cost > max_chars:
            break
        # Append the hypothesis to the top-level list.
        hyp_list = result.get("review_hypotheses")
        if not isinstance(hyp_list, list):
            result["review_hypotheses"] = [compact_hyp]
        else:
            hyp_list.append(compact_hyp)
        if _current_chars(result) > max_chars:
            # Didn't fit after all — roll back.
            if isinstance(result.get("review_hypotheses"), list):
                result["review_hypotheses"].pop()
            break
    return evicted


def _finalize_review_hypothesis_budget(
    result: JsonObject,
    original_hypotheses: list[JsonObject],
    *,
    original_review_leads: JsonObject,
    max_chars: int,
) -> None:
    """Sync the answer-packet mirror, attach review_hypothesis_status, and keep under the cap.

    The status metadata is added after the budget passes measured the packet, so its extra
    chars must be paid for by evicting broad rows — never by exceeding the cap and never by
    dropping the protected hypotheses. Mutates ``result`` in place.

    ``original_review_leads`` is the pre-budget review_leads snapshot captured before any
    compaction pass in enforce_review_context_budget. It funds the P1 coverage repair and
    the truthful P2 omitted counts.

    P1: When the packet is still over the cap at entry, reorder changed_symbols so rows
    interleave across clusters (changed file paths); the tail-eviction logic then retains
    >= 1 anchor per cluster before a 2nd anchor from any single cluster. After eviction
    stabilizes, _repair_cluster_coverage enforces the rule against the pre-budget snapshot
    (the signal hard cap re-ranks rows cluster-blind and can violate it).

    N2: Before syncing the mirror, attempt to restore up to min(3, available) compacted
    hypotheses by evicting broad application/runtime/framework context first. This ensures
    Grafana-shaped packets (modest headroom, fat broad sections) return top-3 hypotheses
    instead of just the floor-of-1.

    P2: After all eviction, attach truncation_summary to output_budget when truncated.
    """
    # P1: Interleave changed_symbols by cluster so eviction respects cluster coverage.
    # Gated on budget pressure so packets that already fit keep their producer order.
    # Use snapshot-derived edge counts to keep ranking consistent with repair/P2 passes.
    _snap_edge_counts = _snapshot_edge_counts(original_review_leads)
    if _current_chars(result) > max_chars:
        _interleave_changed_symbols_by_cluster(result, snapshot_edge_counts=_snap_edge_counts)

    # N2: Evict broad context to fund up to _REVIEW_HYPOTHESIS_HEADROOM_TARGET hypotheses.
    broad_evicted = _restore_hypotheses_up_to_target(
        result,
        original_hypotheses,
        max_chars=max_chars,
        target=_REVIEW_HYPOTHESIS_HEADROOM_TARGET,
    )
    if broad_evicted:
        _sync_review_lead_status_from_packet(result)
        budget = result.get("output_budget")
        if isinstance(budget, dict):
            budget["truncated_sections"] = sorted(set(budget.get("truncated_sections") or []) | broad_evicted)
    # Sync mirror within budget: keep as many top-level hypotheses as fit in the mirror,
    # starting from the highest-ranked (first). If none fit, mirror is empty — recorded in status.
    _sync_review_hypothesis_mirror_within_budget(result, max_chars=max_chars)
    _attach_review_hypothesis_status(result, original_hypotheses)
    # The truncated_sections bookkeeping itself costs chars, so iterate until stable.
    for _ in range(5):
        evicted = _evict_review_rows_to_fit(result, max_chars=max_chars)
        if not evicted:
            break
        _sync_review_lead_status_from_packet(result)
        _attach_review_hypothesis_status(result, original_hypotheses)
        budget = result.get("output_budget")
        if isinstance(budget, dict):
            budget["truncated_sections"] = sorted(set(budget.get("truncated_sections") or []) | evicted)
    # P1: Enforce one-anchor-per-cluster against the pre-budget snapshot; the swaps are
    # approximately size-neutral, so re-run the eviction/status cycle if the cap moved.
    if _repair_cluster_coverage(result, original_review_leads, original_hypotheses, max_chars=max_chars):
        _sync_review_lead_status_from_packet(result)
        _attach_review_hypothesis_status(result, original_hypotheses)
        evicted = _evict_review_rows_to_fit(result, max_chars=max_chars)
        if evicted:
            _sync_review_lead_status_from_packet(result)
            _attach_review_hypothesis_status(result, original_hypotheses)
            budget = result.get("output_budget")
            if isinstance(budget, dict):
                budget["truncated_sections"] = sorted(set(budget.get("truncated_sections") or []) | evicted)
    # If nothing was evictable but the status field pushed the packet over the cap, drop it.
    # review_hypothesis_status is optional metadata; the hard-cap guarantee wins.
    if _current_chars(result) > max_chars:
        result.pop("review_hypothesis_status", None)
    # Drop stale lead IDs from hypotheses before sizing the truncation summary (the
    # reconciliation frees chars the summary can use). hypothesis_id is intentionally not
    # recomputed — it was stamped at producer time from the pre-budget set.
    _reconcile_hypothesis_lead_ids(result)
    # P2: Attach truncation_summary when the packet was truncated. The summary replaces
    # bulk, so it may be funded by evicting low-priority rows (leads last); any lead rows
    # it evicts become part of the omitted counts it reports.
    _attach_truncation_summary_within_budget(
        result, original_review_leads, original_hypotheses, max_chars=max_chars
    )
    # The funding eviction above may have dropped further lead rows; reconcile again so
    # hypotheses never cite lead_ids that no longer exist in the packet.
    _reconcile_hypothesis_lead_ids(result)
    # Re-mirror top-level changed_symbols from review_leads.changed_symbols.
    # _repair_cluster_coverage and the gated re-interleave both de-alias the two lists.
    # Tandem clipping in _evict_review_rows_to_fit keeps review_leads.changed_symbols in
    # sync with top-level evictions, so this re-mirror is guaranteed shrink-or-equal BY
    # CONSTRUCTION — never a growth that would breach the cap.  It exists as cheap
    # invariant enforcement (reorder/repair can shift the list objects) and to handle the
    # _repair_cluster_coverage de-alias path.
    _rl = result.get("review_leads")
    if isinstance(_rl, dict) and isinstance(_rl.get("changed_symbols"), list):
        _rl_cs = _rl["changed_symbols"]
        if isinstance(result.get("changed_symbols"), list):
            result["changed_symbols"] = list(_rl_cs)


# Changed-symbol anchor lists are never evicted to fund the truncation summary: the
# P1 coverage rule (one anchor per cluster) outranks P2 summary detail.
_SUMMARY_FUNDING_PROTECTED_SUFFIXES = ("changed_symbols", "top_changed_symbols")


def _largest_summary_funding_victim(node: object) -> tuple[str, list] | None:
    """Largest row list that may fund the truncation summary.

    Same walk as _largest_row_list, but changed-symbol anchor lists are excluded:
    evicting an anchor to fund a summary row that describes its own omission would
    invert the P1 > P2 priority.
    """
    best_size = 0
    best: tuple[str, list] | None = None
    stack: list[tuple[str, object]] = [("", node)]
    while stack:
        label, current = stack.pop()
        if isinstance(current, dict):
            for key, value in current.items():
                if key in _HARD_CAP_PROTECTED_KEYS or (not label and key in _HARD_CAP_PROTECTED_TOPLEVEL):
                    continue
                child_label = f"{label}.{key}" if label else key
                if isinstance(value, list) and value and any(isinstance(x, dict) for x in value):
                    if child_label.split(".")[-1] not in _SUMMARY_FUNDING_PROTECTED_SUFFIXES:
                        size = len(canonical_json(value))
                        if size > best_size:
                            best_size, best = size, (child_label, value)
                stack.append((child_label, value))
        elif isinstance(current, list):
            for index, item in enumerate(current):
                stack.append((f"{label}[{index}]", item))
    return best


def _evict_summary_funding_rows(result: JsonObject, *, max_chars: int) -> set[str]:
    """Evict rows to fund the truncation summary, protecting hypotheses and anchors.

    Mirrors _evict_review_rows_to_fit (hypotheses and their mirror are shielded, lead
    evictions immediately re-sync the answer-packet mirror) but never touches
    changed-symbol anchor lists. Returns the labels of lists that lost rows.
    """
    evicted: set[str] = set()
    guard = 0
    while _current_chars(result) > max_chars and guard < 5_000:
        guard += 1
        protected = result.pop("review_hypotheses", None)
        answer_packet = result.get("review_answer_packet")
        protected_mirror = None
        if isinstance(answer_packet, dict):
            protected_mirror = answer_packet.pop("top_review_hypotheses", None)
        target = _largest_summary_funding_victim(result)
        if protected is not None:
            result["review_hypotheses"] = protected
        if isinstance(answer_packet, dict) and protected_mirror is not None:
            answer_packet["top_review_hypotheses"] = protected_mirror
        if target is None:
            break
        label, rows = target
        rows.pop()
        evicted.add(label)
        if _is_review_leads_label(label):
            _sync_review_lead_status_from_packet(result)
    return evicted


def _compact_omitted_cluster_row(row: JsonObject) -> JsonObject:
    """Degraded omitted-cluster row: one representative instead of three."""
    compact = dict(row)
    for key in ("representative_symbols", "representative_lead_ids"):
        value = compact.get(key)
        if isinstance(value, list):
            compact[key] = value[:1]
    return compact


def _attach_truncation_summary_within_budget(
    result: JsonObject,
    original_review_leads: JsonObject,
    original_hypotheses: list[JsonObject],
    *,
    max_chars: int,
) -> None:
    """Attach output_budget.truncation_summary without ever exceeding the cap.

    Attach counts first; when even the counts-only form does not fit, evict
    low-priority rows to fund it (broad context first, lead rows last — the summary
    replaces bulk per the head-start discipline) and rebuild the summary so its
    counts stay truthful about the funding eviction. Then add omitted-cluster rows
    one at a time while the packet stays under the cap. If nothing is evictable and
    the summary cannot fit, it is dropped — the hard cap wins over metadata.
    Zero-truncation packets get no truncation_summary.
    """
    budget = result.get("output_budget")
    if not isinstance(budget, dict) or not budget.get("truncated"):
        return
    for _ in range(5):
        ts = _build_truncation_summary(result, original_review_leads, original_hypotheses)
        # Gate: broad-bulk-only truncation (no lead rows omitted, no dropped clusters)
        # produces an all-zero summary that adds noise without value — skip it.
        _omit_fields = (
            "omitted_changed_symbol_count",
            "omitted_direct_caller_count",
            "omitted_direct_callee_count",
            "omitted_transitive_caller_count",
        )
        if not any(ts.get(f) for f in _omit_fields) and not ts.get("omitted_high_risk_clusters"):
            return
        cluster_rows = list(ts.get("omitted_high_risk_clusters") or [])
        ts["omitted_high_risk_clusters"] = []
        budget["truncation_summary"] = ts
        if _current_chars(result) > max_chars:
            # Counts-only summary overflows: fund it by evicting rows while the summary
            # is still attached (so eviction targets the packet-with-summary size), then
            # rebuild the summary so the reported counts include the funding eviction.
            evicted = _evict_summary_funding_rows(result, max_chars=max_chars)
            budget.pop("truncation_summary", None)
            if not evicted:
                return
            _sync_review_lead_status_from_packet(result)
            _attach_review_hypothesis_status(result, original_hypotheses)
            budget = result.get("output_budget")
            if not isinstance(budget, dict):
                return
            budget["truncated_sections"] = sorted(set(budget.get("truncated_sections") or []) | evicted)
            continue
        # Counts fit; add cluster rows while the packet stays under the cap, funding
        # each row that overflows by evicting non-anchor rows and degrading the row
        # to a single representative before giving up on it.
        funding_evicted: set[str] = set()
        for row in cluster_rows:
            attached = False
            for candidate in (row, _compact_omitted_cluster_row(row)):
                ts["omitted_high_risk_clusters"].append(candidate)
                if _current_chars(result) <= max_chars:
                    attached = True
                    break
                evicted = _evict_summary_funding_rows(result, max_chars=max_chars)
                funding_evicted |= evicted
                if _current_chars(result) <= max_chars:
                    attached = True
                    break
                ts["omitted_high_risk_clusters"].pop()
            if not attached:
                break
        if funding_evicted:
            _sync_review_lead_status_from_packet(result)
            _attach_review_hypothesis_status(result, original_hypotheses)
            budget = result.get("output_budget")
            if isinstance(budget, dict):
                budget["truncated_sections"] = sorted(
                    set(budget.get("truncated_sections") or []) | funding_evicted
                )
            # The funding eviction may have dropped further lead rows; refresh the
            # summary count fields so they stay truthful (cluster rows stay as fitted).
            refreshed = _build_truncation_summary(result, original_review_leads, original_hypotheses)
            for key in (
                "omitted_changed_symbol_count",
                "omitted_direct_caller_count",
                "omitted_direct_callee_count",
                "omitted_transitive_caller_count",
            ):
                ts[key] = refreshed[key]
        break
    # Safety: never leave the packet over the cap because of the summary itself.
    if isinstance(budget, dict) and _current_chars(result) > max_chars:
        budget.pop("truncation_summary", None)


_REVIEW_HYPOTHESIS_MIRROR_CAP = 3


def _sync_review_hypothesis_mirror_within_budget(result: JsonObject, *, max_chars: int) -> None:
    """Sync the mirror, adding rows only while the packet stays within max_chars.

    Starts with an empty mirror and adds compacted top-level hypotheses one at a time,
    stopping when the next row would exceed the cap. This keeps the mirror in sync with the
    top-level list without growing the packet beyond the budget.
    """
    answer_packet = result.get("review_answer_packet")
    if not isinstance(answer_packet, dict):
        return
    top_hyps = result.get("review_hypotheses")
    if not isinstance(top_hyps, list):
        answer_packet["top_review_hypotheses"] = []
        return
    # Start by clearing any existing mirror rows (they may be stale from a prior pass).
    answer_packet["top_review_hypotheses"] = []
    for hyp in top_hyps[:_REVIEW_HYPOTHESIS_MIRROR_CAP]:
        if not isinstance(hyp, dict):
            continue
        compact_hyp = _compact_review_hypothesis(hyp)
        answer_packet["top_review_hypotheses"].append(compact_hyp)
        if _current_chars(result) > max_chars:
            answer_packet["top_review_hypotheses"].pop()
            break


def _compact_diff_anchor(value: object) -> JsonObject:
    if not isinstance(value, dict):
        return {}
    compact = {
        key: value[key]
        for key in ("repo", "path", "range", "anchor_type", "match_kind", "symbol_count")
        if key in value
    }
    symbols = value.get("symbols")
    if isinstance(symbols, list):
        compact["symbols"] = [_compact_symbol(row) for row in symbols[:COMPACT_RUNTIME_HEADSTART_LIMIT] if isinstance(row, dict)]
    coordinates = value.get("source_coordinates")
    if isinstance(coordinates, list):
        compact["source_coordinates"] = [
            _compact_coordinate(row) for row in coordinates[:COMPACT_RUNTIME_SOURCE_CHECK_LIMIT] if isinstance(row, dict)
        ]
    return compact


def _investigation_brief_anchor_count(value: object) -> int:
    if not isinstance(value, dict):
        return 0
    anchors = value.get("runtime_anchors")
    return len(anchors) if isinstance(anchors, list) else 0


def _compact_partition_counts(value: object) -> JsonObject:
    if not isinstance(value, dict):
        return {}
    return {
        "status": value.get("status"),
        "interpretation": value.get("interpretation"),
        "counts": value.get("counts", {}),
    }


def _minimal_valid_packet(result: JsonObject) -> JsonObject:
    runtime = result.get("runtime_architecture")
    compact_runtime: JsonObject = {}
    if isinstance(runtime, dict):
        answer_packet = runtime.get("answer_packet")
        summary = runtime.get("summary", {})
        anchor_resolution_contract = runtime.get("anchor_resolution_contract")
        # An investigation-brief-only packet had an ambiguous/unresolved anchor, so its
        # runtime maps and deploy_kind_counts are gated out of the answer path. The
        # anchor-resolution caution contract must outlive answer-shaped counts: never
        # reintroduce deploy_kind_counts when the packet is gated, and always carry the
        # omitted-section list and anchor_resolution_contract through this fallback.
        gated = (
            isinstance(summary, dict) and summary.get("answer_packet_mode") == "investigation_brief_only"
        ) or isinstance(anchor_resolution_contract, dict)
        compact_answer = {}
        if isinstance(answer_packet, dict):
            compact_answer = {
                "investigation_brief": _compact_investigation_brief(answer_packet.get("investigation_brief")),
                "missing_fact_families": answer_packet.get("missing_fact_families", []),
                "evidence_contract": answer_packet.get("evidence_contract"),
            }
            if "omitted_answer_sections" in answer_packet:
                compact_answer["omitted_answer_sections"] = answer_packet.get("omitted_answer_sections", [])
            if not gated:
                compact_answer["deploy_kind_counts"] = answer_packet.get("deploy_kind_counts", {})
        compact_runtime = {
            "scope": runtime.get("scope", {}),
            "summary": summary,
            "answer_packet": compact_answer,
            "assembly_contract": runtime.get("assembly_contract"),
        }
        if isinstance(anchor_resolution_contract, dict) and anchor_resolution_contract:
            compact_runtime["anchor_resolution_contract"] = anchor_resolution_contract
    minimal = {
        "tool": result.get("tool"),
        "status": result.get("status"),
        "query": result.get("query"),
        "summary": result.get("summary", {}),
        "snapshot_summary": result.get("snapshot_summary", {}),
        "snapshot_scope": result.get("snapshot_scope", {}),
        "ownership_context": _compact_ownership_context(result.get("ownership_context", {})),
        "related_facts": _compact_related_facts(result.get("related_facts", {})),
        "service_operational_surfaces": _compact_service_operational_surfaces(
            result.get("service_operational_surfaces", {})
        ),
        "anchors": result.get("anchors", {}),
        "proven_facts": _compact_common_fact_index(result.get("proven_facts"), omitted_field="omitted_proven_fact_sources"),
        "candidate_leads": _compact_common_fact_index(
            result.get("candidate_leads"), omitted_field="omitted_candidate_lead_sources"
        ),
        "coverage_gaps": _compact_common_coverage_gaps(result.get("coverage_gaps")),
        "inspection_areas": _compact_common_inspection_areas(result.get("inspection_areas")),
        "answerability": result.get("answerability", {}),
        "coverage_warnings": [],
        "unsupported_scopes": [],
        "next_actions": result.get("next_actions", []),
    }
    if compact_runtime:
        minimal["runtime_architecture"] = compact_runtime
    compact_authz = _compact_authz_surface(result.get("authz_surface", {}))
    if compact_authz:
        minimal["authz_surface"] = compact_authz
    return minimal


def _list_value(value: object) -> list[object]:
    if not isinstance(value, list):
        return []
    return value


def _append_budget_next_action(result: JsonObject, *, max_chars: int) -> None:
    original_actions = result.get("next_actions")
    actions = [str(action).strip() for action in _list_value(result.get("next_actions")) if str(action).strip()]
    actions.append(_TRUNCATED_DETAIL_ACTION)
    result["next_actions"] = _dedupe_strings(actions)
    _refresh_remaining_chars(result, max_chars=max_chars)
    if _current_chars(result) <= max_chars:
        return
    if original_actions is None:
        result.pop("next_actions", None)
    else:
        result["next_actions"] = original_actions
    _refresh_remaining_chars(result, max_chars=max_chars)


def _refresh_remaining_chars(result: JsonObject, *, max_chars: int) -> None:
    output_budget = result.get("output_budget")
    if not isinstance(output_budget, dict):
        return
    for _ in range(3):
        remaining_chars = max(0, max_chars - _current_chars(result))
        if output_budget.get("remaining_chars") == remaining_chars:
            break
        output_budget["remaining_chars"] = remaining_chars


def _compact_common_fact_index(value: object, *, omitted_field: str) -> JsonObject:
    if not isinstance(value, dict):
        return {}
    compact: JsonObject = {}
    status = value.get("status")
    if isinstance(status, str) and status:
        compact["status"] = status
    claim_boundary = value.get("claim_boundary")
    if isinstance(claim_boundary, str) and claim_boundary:
        compact["claim_boundary"] = claim_boundary
    sources = _dedupe_budget_rows([dict(row) for row in _list_value(value.get("sources")) if isinstance(row, dict)])
    if not sources:
        return compact
    if len(sources) <= COMPACT_RUNTIME_HEADSTART_LIMIT:
        compact["sources"] = sources
        return compact
    visible_count = max(0, COMPACT_RUNTIME_HEADSTART_LIMIT - 1)
    compact["sources"] = [
        *sources[:visible_count],
        {
            "field": omitted_field,
            "omitted_source_count": len(sources) - visible_count,
            "reason": "Additional common fact source summaries were omitted by the planning_context budget fallback.",
        },
    ]
    return compact


def _compact_common_coverage_gaps(value: object) -> list[JsonObject]:
    rows = _dedupe_budget_rows([dict(row) for row in _list_value(value) if isinstance(row, dict)])
    if len(rows) <= COMPACT_RUNTIME_HEADSTART_LIMIT:
        return rows
    visible_count = max(0, COMPACT_RUNTIME_HEADSTART_LIMIT - 1)
    omitted_count = len(rows) - visible_count
    return [
        *rows[:visible_count],
        {
            "trigger": "common_coverage_gaps_truncated",
            "detail": {
                "omitted_row_count": omitted_count,
                "reason": "Additional coverage gaps were omitted by the planning_context budget fallback; retry with narrower anchors when relevant.",
            },
        },
    ]


def _compact_common_inspection_areas(value: object) -> list[JsonObject]:
    rows = _dedupe_budget_rows([dict(row) for row in _list_value(value) if isinstance(row, dict)])
    if len(rows) <= COMPACT_RUNTIME_SOURCE_CHECK_LIMIT:
        return [_compact_common_inspection_area(row) for row in rows]
    visible_count = max(0, COMPACT_RUNTIME_SOURCE_CHECK_LIMIT - 1)
    omitted_rows = rows[visible_count:]
    return [
        *(_compact_common_inspection_area(row) for row in rows[:visible_count]),
        _omitted_common_inspection_area(omitted_rows),
    ]


def _compact_common_inspection_area(row: JsonObject) -> JsonObject:
    compact = dict(row)
    refs = _list_value(compact.get("inspection_refs"))
    if refs:
        compact_refs = [
            compact_ref
            for ref in refs
            if isinstance(ref, dict)
            for compact_ref in [_compact_coordinate(ref)]
            if compact_ref
        ]
        compact["inspection_refs"] = compact_refs[:COMPACT_RUNTIME_SOURCE_CHECK_LIMIT]
        omitted_count = len(compact_refs) - len(compact["inspection_refs"])
        if omitted_count > 0:
            compact["inspection_refs_truncated"] = True
            compact["omitted_inspection_ref_count"] = (
                _int_count(compact.get("omitted_inspection_ref_count"), fallback=0) + omitted_count
            )
    search_terms = [term for term in _list_value(compact.get("search_terms")) if isinstance(term, str)]
    if search_terms:
        deduped_terms = _dedupe_strings(search_terms)
        compact["search_terms"] = deduped_terms[:COMPACT_RUNTIME_SOURCE_CHECK_LIMIT]
        omitted_count = len(deduped_terms) - len(compact["search_terms"])
        if omitted_count > 0:
            compact["search_terms_truncated"] = True
            compact["omitted_search_term_count"] = (
                _int_count(compact.get("omitted_search_term_count"), fallback=0) + omitted_count
            )
    return compact


def _omitted_common_inspection_area(rows: list[JsonObject]) -> JsonObject:
    refs: list[JsonObject] = []
    search_terms: list[str] = []
    for row in rows:
        for ref in _list_value(row.get("inspection_refs")):
            if isinstance(ref, dict):
                refs.append(_compact_coordinate(ref))
        for term in _list_value(row.get("search_terms")):
            if isinstance(term, str) and term.strip():
                search_terms.append(term.strip())
        if len(refs) >= COMPACT_RUNTIME_SOURCE_CHECK_LIMIT and len(search_terms) >= COMPACT_RUNTIME_SOURCE_CHECK_LIMIT:
            break
    compact: JsonObject = {
        "area": "omitted_common_inspection_areas",
        "reason": "Additional inspection areas were omitted by the planning_context budget fallback; retry with narrower anchors when relevant.",
        "trigger": "budget_truncated",
        "omitted_row_count": len(rows),
    }
    compact_refs = _dedupe_budget_rows([ref for ref in refs if ref])[:COMPACT_RUNTIME_SOURCE_CHECK_LIMIT]
    if compact_refs:
        compact["inspection_refs"] = compact_refs
    compact_terms = _dedupe_strings(search_terms)[:COMPACT_RUNTIME_SOURCE_CHECK_LIMIT]
    if compact_terms:
        compact["search_terms"] = compact_terms
    return compact


def _answer_list_len(answer_packet: JsonObject, key: str) -> int:
    value = answer_packet.get(key)
    return len(value) if isinstance(value, list) else 0
