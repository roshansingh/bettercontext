from __future__ import annotations

from source.kg.core.models import JsonObject
from source.kg.product.review_attribution import hypothesis_label, hypothesis_stable_id


def _normalize_path(path: str) -> str:
    """Normalize path for comparison: backslash to forward slash, strip literal "./" prefixes."""
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


_TEST_PATH_SEGMENTS = frozenset({"test", "tests", "spec", "specs", "__tests__"})
_CONFIG_EXTENSIONS = frozenset({".json", ".yaml", ".yml", ".toml", ".ini", ".env"})
_CONFIG_BASENAMES = frozenset({"Dockerfile"})
_STYLESHEET_EXTENSIONS = frozenset({".css", ".scss", ".sass", ".less"})
_FRONTEND_COMPONENT_EXTENSIONS = frozenset({".tsx", ".jsx"})
_FRONTEND_HOOK_EXTENSIONS = frozenset({".ts", ".tsx", ".js", ".jsx"})
_CONFIDENCE_RANK = {"strong": 2, "medium": 1, "weak": 0}
_FRAMEWORK_IMPACT_KEYS = ("changed_models", "model_fields", "model_relations", "serializers", "views", "tasks")
_RUNTIME_SURFACE_KEYS = ("endpoints", "endpoint_consumers", "event_channels", "deploy_mappings")
_ASYNC_LIFECYCLE_FAMILIES: frozenset[str] = frozenset({"async_callback_in_iteration", "unawaited_async_call"})
_CALL_RESULT_IDENTITY_FAMILY: frozenset[str] = frozenset({"call_result_identity_comparison"})
_DESTRUCTIVE_CALL_SUFFIXES: frozenset[str] = frozenset({".deletemany", ".delete", ".deleteone", ".destroy"})
_DESTRUCTIVE_CALL_BARE: frozenset[str] = frozenset({"delete", "deletemany", "deleteone", "destroy"})

# O2: Specific-class families. Post-1e74bf2 probe evidence (Grafana 106778, limit=25) showed
# specific families generating on real repos but losing visible slots to generics because
# generic families (direct_call_contract_drift) accumulate more supporting leads by construction.
# Stable partition: specific-class first, generic-class second, within each class the existing
# 4-part comparator order is preserved. Mirror slot selection takes the head of this order,
# so hook_gate_render_mismatch outranks direct_call_contract_drift in top_review_hypotheses.
_SPECIFIC_CLASS_FAMILIES: frozenset[str] = frozenset(
    {
        "async_side_effect_lifecycle_drift",
        "call_result_identity_comparison_semantics",
        "component_list_render_identity_drift",
        "destructive_mutation_test_gap",
        "hook_gate_render_mismatch",
        "swallowed_exception_state_drift",
        "test_locks_in_regression",
        "low_coverage_stylesheet_gap",
    }
)

# B2: Per-family specificity class. "high" = code_risk_signal-driven or A2 contract-diff;
# "medium" = convention-triggered specific families; "low" = generic families.
# test_locks_in_regression is "low" by default: it lacks a named runtime invariant in its
# current form and must not outrank high-specificity families in top_review_hypotheses.
# It becomes "medium" only if signal/delta-backed evidence attaches (not yet wired).
_FAMILY_SPECIFICITY: dict[str, str] = {
    # High: driven by code_risk_signal evidence or A2 contract-diff
    "async_side_effect_lifecycle_drift": "high",
    "swallowed_exception_state_drift": "high",
    "call_result_identity_comparison_semantics": "high",
    "destructive_mutation_test_gap": "high",
    # Medium: convention-triggered specific families
    "component_list_render_identity_drift": "medium",
    "hook_gate_render_mismatch": "medium",
    "low_coverage_stylesheet_gap": "medium",
    # Low: test_locks lacks a named runtime invariant; treated low until signal-backed
    "test_locks_in_regression": "low",
    # Low: generic families
    "direct_call_contract_drift": "low",
    "framework_contract_drift": "low",
    "runtime_endpoint_or_event_contract_drift": "low",
    "application_surface_contract_drift": "low",
    "test_or_config_masks_runtime_change": "low",
}


_SPEC_RANK: dict[str, int] = {"high": 0, "medium": 1, "low": 2}


def _sort_and_cap_hypotheses(hypotheses: list[JsonObject]) -> list[JsonObject]:
    """Sort by signal strength, partition into high/medium/low specificity tiers, cap at 5.

    Partition is applied to the FULL sorted list so a high-specificity row ranked 6th by
    lead-count is not discarded before the tier ordering runs. Within each tier the 4-part
    comparator order (lead count, evidence count, confidence, risk_type) is preserved.
    """
    hypotheses = list(hypotheses)
    hypotheses.sort(
        key=lambda row: (
            -len(row.get("supporting_lead_ids") or []),
            -len(row.get("evidence_refs") or []),
            -_CONFIDENCE_RANK.get(str(row.get("confidence")), 0),
            str(row.get("risk_type") or ""),
        )
    )
    highs = [h for h in hypotheses if _SPEC_RANK.get(str(h.get("specificity") or "low"), 2) == 0]
    mediums = [h for h in hypotheses if _SPEC_RANK.get(str(h.get("specificity") or "low"), 2) == 1]
    lows = [h for h in hypotheses if _SPEC_RANK.get(str(h.get("specificity") or "low"), 2) == 2]
    return (highs + mediums + lows)[:5]


# ---------------------------------------------------------------------------
# Structural noise downranking (pre-cap reorder within peer groups)
# ---------------------------------------------------------------------------
#
# The splice layer inserts diff-derived rows at the front partitioned strictly by
# derivation trust tier (deterministic_static, then inferred_llm, then generic rows
# with no derivation stamp). That tier boundary is intentional and must not be
# crossed by scoring — a low-value deterministic row still outranks a high-value
# inferred one. The scorer therefore reorders only WITHIN each derivation peer group,
# stably (score desc, prior order as tiebreak), so on a large PR a builtin/test-only/
# module-root call row cannot win a scarce slot ahead of a concrete-failure-mode row.
#
# All signals are structural (KG entity kind / path segments / claim shape) — no
# name lists, no keywords. Constants are additive; the base score is 0.

# Call-target entity kinds that make a removed/moved-call row low-value:
#   external symbol (e.g. a language builtin) or an external package.
_LOW_VALUE_CALL_TARGET_KINDS: frozenset[str] = frozenset({"ExternalSymbol", "ExternalPackage"})
# Entity kinds that represent a bare package/module root rather than a real symbol.
_MODULE_ROOT_TARGET_KINDS: frozenset[str] = frozenset({"CodeModule", "ExternalPackage"})
# Risk types whose rows describe a removed or moved CALLS edge (target-bearing).
_CALL_MOVE_RISK_TYPES: frozenset[str] = frozenset(
    {"guard_call_removed_drift", "responsibility_moved_drift"}
)

_PENALTY_EXTERNAL_CALL_TARGET = -3.0
_PENALTY_BOTH_PATHS_TEST = -2.0
_PENALTY_MODULE_ROOT_TARGET = -2.0
_BOOST_CONCRETE_FAILURE_MODE = 3.0
_BOOST_CAUSE_IN_CHANGED_PROD_FILE = 2.0


def _path_of(ref: JsonObject | None) -> str:
    if not isinstance(ref, dict):
        return ""
    return _normalize_path(str(ref.get("path") or ""))


def _structural_noise_score(
    row: JsonObject,
    changed_file_set: frozenset[str],
    changed_qualnames: frozenset[str],
) -> float:
    """Deterministic structural score for one hypothesis row (higher = keep).

    Structural signals only — entity kind, path segments, claim shape. Base 0.

    Penalize:
      - a removed/moved-call row whose call target resolves to a language builtin or
        external-package entity, with no changed-symbol overlap (target/subject qualname
        not among the PR's changed symbols) — a call into third-party/builtin code that
        the PR did not itself touch.
      - a row whose cause AND consequence are both test-classified paths.
      - a moved-call row whose target is a bare module/package root entity.
    Boost:
      - a row carrying a concrete failure mode in its claim structure
        (an ``unimplemented_members`` list — e.g. abstract-contract rows).
      - a row whose cause path is a production (non-test) file present in the PR's
        changed files.
    """
    score = 0.0
    risk_type = str(row.get("risk_type") or "")
    cause_path = _path_of(row.get("cause"))
    consequence_path = _path_of(row.get("consequence"))
    target_kind = str(row.get("target_entity_kind") or "")

    # Penalty 1: call row targeting a builtin/external entity with no changed-symbol overlap.
    if risk_type in _CALL_MOVE_RISK_TYPES and target_kind in _LOW_VALUE_CALL_TARGET_KINDS:
        target_urn = str(row.get("target_urn") or "")
        overlaps = bool(target_urn) and any(q and q in target_urn for q in changed_qualnames)
        if not overlaps:
            score += _PENALTY_EXTERNAL_CALL_TARGET

    # Penalty 2: both cause and consequence are test paths.
    if cause_path and consequence_path and _is_test_file(cause_path) and _is_test_file(consequence_path):
        score += _PENALTY_BOTH_PATHS_TEST

    # Penalty 3: moved-call row whose target is a bare module/package root.
    if risk_type in _CALL_MOVE_RISK_TYPES and target_kind in _MODULE_ROOT_TARGET_KINDS:
        score += _PENALTY_MODULE_ROOT_TARGET

    # Boost 1: concrete failure mode carried in the claim structure.
    if row.get("unimplemented_members"):
        score += _BOOST_CONCRETE_FAILURE_MODE

    # Boost 2: cause path is a production file that the PR changed.
    if cause_path and not _is_test_file(cause_path) and cause_path in changed_file_set:
        score += _BOOST_CAUSE_IN_CHANGED_PROD_FILE

    return score


def apply_structural_noise_downranking(
    hypotheses: list[JsonObject],
    changed_files: list[str],
    changed_symbols: list[JsonObject] | None = None,
) -> list[JsonObject]:
    """Stable-reorder rows by structural score WITHIN each (derivation, risk_type) peer group.

    Peer group is (derivation trust tier, risk_type family). The scorer reorders only
    among rows of the SAME tier AND SAME family, writing them back into the exact
    positions that family's rows already occupied. This preserves two existing
    invariants the downstream cap depends on:
      - the derivation trust-tier partition (deterministic_static, then inferred_llm,
        then generic) — a row never crosses a tier boundary; and
      - the family interleaving the splice round-robin built — so which diff-derived
        families sit in the pre-cap prefix is unchanged, and the cap's per-family
        survival guarantee (_cap_review_hypotheses_reserving_diff_families) still holds.
    Within a family, a noisy row (builtin/external or module-root call target, test-only
    cause+consequence) sinks below a higher-signal peer of the same family; a boosted
    row (concrete failure mode, changed-prod-file cause) rises. Ties keep prior order.
    Does not cap or drop rows.
    """
    changed_file_set = frozenset(
        _normalize_path(str(p)) for p in changed_files if isinstance(p, str) and p
    )
    changed_qualnames = frozenset(
        str(s.get("qualname") or s.get("qualified_name") or "")
        for s in (changed_symbols or [])
        if isinstance(s, dict) and (s.get("qualname") or s.get("qualified_name"))
    )

    # Group original list indices by (derivation, risk_type). Rows are reordered only
    # among their group's own positions, so the sequence of group-slots is preserved.
    groups: dict[tuple[str, str], list[int]] = {}
    for idx, row in enumerate(hypotheses):
        if not isinstance(row, dict):
            continue
        key = (str(row.get("derivation") or ""), str(row.get("risk_type") or ""))
        groups.setdefault(key, []).append(idx)

    reordered = list(hypotheses)
    for positions in groups.values():
        if len(positions) < 2:
            continue
        ranked = sorted(
            positions,
            key=lambda i: (
                -_structural_noise_score(hypotheses[i], changed_file_set, changed_qualnames),
                i,  # stable tiebreak: prior order
            ),
        )
        for slot, src in zip(positions, ranked):
            reordered[slot] = hypotheses[src]
    return reordered


def review_hypotheses_for_context(
    *,
    changed_files: list[str],
    changed_symbols: list[JsonObject],
    direct_callers: list[JsonObject],
    direct_callees: list[JsonObject],
    transitive_callers: list[JsonObject],
    framework_impact: JsonObject,
    application_impact: JsonObject,
    runtime_surfaces: dict[str, list[JsonObject]],
    review_leads: JsonObject,
    review_lead_status: JsonObject,
    risk_signals: list[JsonObject] | None = None,
) -> list[JsonObject]:
    low_coverage = review_lead_status.get("coverage_status") == "low_coverage"
    if low_coverage:
        h = _low_coverage_stylesheet_gap(
            changed_files=changed_files,
            low_coverage=True,
            review_leads=review_leads,
        )
        return [h] if h else []
    has_surface_signal = bool(
        _has_framework_signal(framework_impact)
        or _has_runtime_signal(runtime_surfaces)
        or _has_application_signal(application_impact)
    )
    hypotheses: list[JsonObject] = []
    h = _direct_call_contract_drift(
        changed_symbols=changed_symbols,
        direct_callers=direct_callers,
        direct_callees=direct_callees,
        review_leads=review_leads,
        has_surface_signal=has_surface_signal,
    )
    if h:
        hypotheses.append(h)
    h = _framework_contract_drift(framework_impact=framework_impact, review_leads=review_leads)
    if h:
        hypotheses.append(h)
    h = _runtime_endpoint_or_event_contract_drift(runtime_surfaces=runtime_surfaces, review_leads=review_leads)
    if h:
        hypotheses.append(h)
    h = _application_surface_contract_drift(application_impact=application_impact, review_leads=review_leads)
    if h:
        hypotheses.append(h)
    h = _test_or_config_masks_runtime_change(
        changed_files=changed_files,
        changed_symbols=changed_symbols,
        direct_callers=direct_callers,
        direct_callees=direct_callees,
        review_leads=review_leads,
    )
    if h:
        hypotheses.append(h)
    h = _component_list_render_identity_drift(
        changed_symbols=changed_symbols,
        direct_callers=direct_callers,
        direct_callees=direct_callees,
        review_leads=review_leads,
    )
    if h:
        hypotheses.append(h)
    h = _hook_gate_render_mismatch(
        changed_symbols=changed_symbols,
        direct_callers=direct_callers,
        direct_callees=direct_callees,
        review_leads=review_leads,
    )
    if h:
        hypotheses.append(h)
    h = _test_locks_in_regression(
        changed_files=changed_files,
        changed_symbols=changed_symbols,
        direct_callers=direct_callers,
        direct_callees=direct_callees,
        review_leads=review_leads,
    )
    if h:
        hypotheses.append(h)
    effective_risk_signals = risk_signals or []
    h = _async_side_effect_lifecycle_drift(
        changed_symbols=changed_symbols,
        changed_files=changed_files,
        direct_callers=direct_callers,
        direct_callees=direct_callees,
        risk_signals=effective_risk_signals,
        review_leads=review_leads,
    )
    if h:
        hypotheses.append(h)
    h = _swallowed_exception_state_drift(
        changed_symbols=changed_symbols,
        changed_files=changed_files,
        direct_callers=direct_callers,
        direct_callees=direct_callees,
        risk_signals=effective_risk_signals,
        review_leads=review_leads,
    )
    if h:
        hypotheses.append(h)
    h = _call_result_identity_comparison_semantics(
        changed_symbols=changed_symbols,
        changed_files=changed_files,
        risk_signals=effective_risk_signals,
        review_leads=review_leads,
        direct_callers=direct_callers,
        direct_callees=direct_callees,
    )
    if h:
        hypotheses.append(h)
    h = _destructive_mutation_test_gap(
        changed_symbols=changed_symbols,
        changed_files=changed_files,
        direct_callees=direct_callees,
        review_leads=review_leads,
    )
    if h:
        hypotheses.append(h)
    return _sort_and_cap_hypotheses(hypotheses)


def _has_framework_signal(framework_impact: JsonObject) -> bool:
    return any(framework_impact.get(key) for key in _FRAMEWORK_IMPACT_KEYS)


def _has_runtime_signal(runtime_surfaces: dict[str, list[JsonObject]]) -> bool:
    return any(runtime_surfaces.get(key) for key in _RUNTIME_SURFACE_KEYS)


def _has_application_signal(application_impact: JsonObject) -> bool:
    same_repo_surfaces = application_impact.get("same_repo_surfaces")
    runtime_facts = application_impact.get("runtime_facts")
    return bool(
        (isinstance(same_repo_surfaces, dict) and any(same_repo_surfaces.values()))
        or (isinstance(runtime_facts, list) and runtime_facts)
    )


def _make_hypothesis(
    risk_type: str,
    confidence: str,
    why: str,
    evidence_refs: list[JsonObject],
    source_checks: list[str],
    supporting_lead_ids: list[str],
    concrete_invariant: str | None = None,
    postable_claim: str | None = None,
    cause: JsonObject | None = None,
    consequence: JsonObject | None = None,
    negative_checks: list[str] | None = None,
) -> JsonObject:
    row: JsonObject = {
        "risk_type": risk_type,
        "confidence": confidence,
        "why": why,
        "evidence_refs": evidence_refs,
        "source_checks": source_checks,
        "supporting_lead_ids": supporting_lead_ids,
        "specificity": _FAMILY_SPECIFICITY.get(risk_type, "low"),
    }
    if concrete_invariant is not None:
        row["concrete_invariant"] = concrete_invariant
    if postable_claim is not None:
        row["postable_claim"] = postable_claim
    if cause is not None:
        row["cause"] = cause
    if consequence is not None:
        row["consequence"] = consequence
    if negative_checks is not None:
        row["negative_checks"] = negative_checks
    source_spans = _source_spans_from_evidence_refs(evidence_refs)
    if source_spans:
        row["source_spans"] = source_spans
    hypothesis_id = hypothesis_stable_id(risk_type, supporting_lead_ids, evidence_refs)
    row["hypothesis_id"] = hypothesis_id
    row["label"] = hypothesis_label(risk_type, hypothesis_id)
    return row


_SOURCE_SPAN_KEYS = ("repo", "path", "line_start", "line_end", "qualified_name", "qualname")
_SOURCE_SPAN_LIMIT = 4


def _source_spans_from_evidence_refs(evidence_refs: list[JsonObject]) -> list[JsonObject]:
    """Project pure coordinate spans from a hypothesis's evidence_refs.

    Keeps only path-bearing refs, reduced to coordinate keys, deduped in order,
    bounded at _SOURCE_SPAN_LIMIT. Never fabricates coordinates: refs without a
    path produce no span.
    """
    spans: list[JsonObject] = []
    seen: set[tuple] = set()
    for ref in evidence_refs:
        if not isinstance(ref, dict) or not ref.get("path"):
            continue
        span = {key: ref[key] for key in _SOURCE_SPAN_KEYS if ref.get(key) is not None}
        dedupe_key = tuple(sorted(span.items()))
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        spans.append(span)
        if len(spans) >= _SOURCE_SPAN_LIMIT:
            break
    return spans


def _lead_ids_for_fields(review_leads: JsonObject, fields: tuple[str, ...]) -> list[str]:
    ids: list[str] = []
    for field in fields:
        for row in review_leads.get(field) or []:
            if isinstance(row, dict):
                lead_id = row.get("lead_id")
                if isinstance(lead_id, str) and lead_id:
                    ids.append(lead_id)
    return ids


def _evidence_refs_from_leads(review_leads: JsonObject, fields: tuple[str, ...]) -> list[JsonObject]:
    refs: list[JsonObject] = []
    for field in fields:
        for row in review_leads.get(field) or []:
            if not isinstance(row, dict):
                continue
            ref: JsonObject = {}
            lead_id = row.get("lead_id")
            if isinstance(lead_id, str) and lead_id:
                ref["lead_id"] = lead_id
            # Flat coordinate fields present on symbol rows.
            for key in ("repo", "path", "line_start", "line_end", "qualified_name", "qualname"):
                val = row.get(key)
                if val is not None:
                    ref[key] = val
            # Relation-row fields: predicate/subject/object are always present on _fact_result rows.
            for key in ("predicate", "subject", "object"):
                val = row.get(key)
                if isinstance(val, str) and val:
                    ref[key] = val
            # When flat path/line_start are absent (relation rows), pull from first evidence bytes_ref.
            if "path" not in ref or "line_start" not in ref:
                evidence_list = row.get("evidence")
                if isinstance(evidence_list, list):
                    for ev in evidence_list:
                        if not isinstance(ev, dict):
                            continue
                        bytes_ref = ev.get("bytes_ref")
                        if not isinstance(bytes_ref, dict):
                            continue
                        for coord_key in ("repo", "path", "line_start", "line_end"):
                            if coord_key not in ref:
                                val = bytes_ref.get(coord_key)
                                if val is not None:
                                    ref[coord_key] = val
                        if "path" in ref:
                            break
            # Fallback: call_site carries qualifier-level source coordinates.
            if "path" not in ref and "line_start" not in ref:
                call_site = row.get("call_site")
                if isinstance(call_site, dict):
                    for coord_key in ("repo", "path", "line_start", "line_end"):
                        if coord_key not in ref:
                            val = call_site.get(coord_key)
                            if val is not None:
                                ref[coord_key] = val
            caller = row.get("caller_symbol") or (row.get("subject") if isinstance(row.get("subject"), dict) else None)
            if isinstance(caller, dict):
                for key in ("qualified_name", "qualname", "repo", "path"):
                    val = caller.get(key)
                    if val is not None:
                        ref[f"caller_{key}"] = val
            callee = row.get("callee_symbol") or (row.get("object") if isinstance(row.get("object"), dict) else None)
            if isinstance(callee, dict):
                for key in ("qualified_name", "qualname", "repo", "path"):
                    val = callee.get(key)
                    if val is not None:
                        ref[f"callee_{key}"] = val
            if ref:
                refs.append(ref)
    return refs[:5]


def _direct_call_contract_drift(
    *,
    changed_symbols: list[JsonObject],
    direct_callers: list[JsonObject],
    direct_callees: list[JsonObject],
    review_leads: JsonObject,
    has_surface_signal: bool,
) -> JsonObject | None:
    if not changed_symbols:
        return None
    if not direct_callers and not direct_callees:
        return None
    lead_fields = ("direct_callers", "direct_callees", "transitive_callers")
    lead_ids = _lead_ids_for_fields(review_leads, lead_fields)
    if not lead_ids:
        return None
    confidence = "strong" if ((direct_callers or direct_callees) and has_surface_signal) else "medium"
    evidence_refs = _evidence_refs_from_leads(review_leads, ("direct_callers", "direct_callees"))
    # Build postable_claim from first caller/callee lead if available
    postable_claim: str | None = None
    first_ref = evidence_refs[0] if evidence_refs else None
    if first_ref:
        caller_q = first_ref.get("caller_qualname") or first_ref.get("caller_qualified_name")
        callee_q = first_ref.get("callee_qualname") or first_ref.get("callee_qualified_name")
        subj = first_ref.get("subject")
        obj_ = first_ref.get("object")
        if caller_q and callee_q:
            postable_claim = f"{caller_q} calls {callee_q}; verify the call contract holds after this change."
        elif subj and obj_:
            postable_claim = f"{subj} calls {obj_}; verify the call contract holds after this change."
    source_checks = [
        "Verify callers still satisfy the changed symbol's pre/post-conditions.",
        "Check direct callees for interface drift introduced by this change.",
    ]
    negative_checks = [
        "If callers pass the same arguments and the changed symbol's return type and side effects are unchanged, contract drift is unlikely.",
    ]
    return _make_hypothesis(
        risk_type="direct_call_contract_drift",
        confidence=confidence,
        why="Changed symbols have direct callers or callees whose call contracts can drift with this change.",
        evidence_refs=evidence_refs,
        source_checks=source_checks,
        supporting_lead_ids=lead_ids[:10],
        postable_claim=postable_claim,
        negative_checks=negative_checks,
    )


def _framework_contract_drift(
    *,
    framework_impact: JsonObject,
    review_leads: JsonObject,
) -> JsonObject | None:
    if not _has_framework_signal(framework_impact):
        return None
    evidence_refs: list[JsonObject] = []
    for key in _FRAMEWORK_IMPACT_KEYS:
        rows = framework_impact.get(key)
        if isinstance(rows, list):
            for row in rows[:3]:
                if isinstance(row, dict):
                    ref: JsonObject = {"framework_key": key}
                    for fk in ("repo", "path", "qualname", "qualified_name", "name"):
                        val = row.get(fk)
                        if val is not None:
                            ref[fk] = val
                    evidence_refs.append(ref)
    if not evidence_refs:
        return None
    source_checks = [
        "Inspect framework-generated schema migrations and serializer output for drift.",
        "Verify view/task handlers still align with updated model contracts.",
    ]
    negative_checks = [
        "If the changed code only adds new fields without altering existing fields or serializer output, framework contract drift is unlikely.",
    ]
    lead_ids = _lead_ids_for_fields(review_leads, ("changed_symbols",))
    return _make_hypothesis(
        risk_type="framework_contract_drift",
        confidence="medium" if lead_ids else "weak",
        why="Framework-declared models, serializers, views, or tasks are affected by the changed code.",
        evidence_refs=evidence_refs[:5],
        source_checks=source_checks,
        supporting_lead_ids=lead_ids[:10],
        negative_checks=negative_checks,
    )


def _runtime_endpoint_or_event_contract_drift(
    *,
    runtime_surfaces: dict[str, list[JsonObject]],
    review_leads: JsonObject,
) -> JsonObject | None:
    if not _has_runtime_signal(runtime_surfaces):
        return None
    evidence_refs: list[JsonObject] = []
    for key in _RUNTIME_SURFACE_KEYS:
        rows = runtime_surfaces.get(key) or []
        for row in rows[:2]:
            if isinstance(row, dict):
                ref: JsonObject = {"surface_kind": key}
                for fk in ("repo", "path", "method", "endpoint_path", "channel_address", "name"):
                    val = row.get(fk)
                    if val is not None:
                        ref[fk] = val
                evidence_refs.append(ref)
    if not evidence_refs:
        return None
    source_checks = [
        "Verify endpoint request/response contracts match caller expectations.",
        "Check event channel producers and consumers for schema compatibility.",
    ]
    negative_checks = [
        "If the changed code path is not reachable from any exposed endpoint or event channel, contract drift to other services is unlikely.",
    ]
    lead_ids = _lead_ids_for_fields(review_leads, ("changed_symbols",))
    return _make_hypothesis(
        risk_type="runtime_endpoint_or_event_contract_drift",
        confidence="medium" if lead_ids else "weak",
        why="The changed repo exposes runtime endpoints, event channels, or deploy mappings that can carry the change to other services.",
        evidence_refs=evidence_refs[:5],
        source_checks=source_checks,
        supporting_lead_ids=lead_ids[:10],
        negative_checks=negative_checks,
    )


def _application_surface_contract_drift(
    *,
    application_impact: JsonObject,
    review_leads: JsonObject,
) -> JsonObject | None:
    if not _has_application_signal(application_impact):
        return None
    same_repo_surfaces = application_impact.get("same_repo_surfaces")
    runtime_facts = application_impact.get("runtime_facts")
    evidence_refs: list[JsonObject] = []
    if isinstance(same_repo_surfaces, dict):
        for surface_key, rows in same_repo_surfaces.items():
            if isinstance(rows, list):
                for row in rows[:2]:
                    if isinstance(row, dict):
                        ref: JsonObject = {"surface_kind": surface_key}
                        for fk in ("repo", "path", "qualname", "qualified_name"):
                            val = row.get(fk)
                            if val is not None:
                                ref[fk] = val
                        evidence_refs.append(ref)
    if isinstance(runtime_facts, list):
        for row in runtime_facts[:2]:
            if isinstance(row, dict):
                ref = {"surface_kind": "runtime_fact"}
                for fk in ("repo", "path", "predicate"):
                    val = row.get(fk)
                    if val is not None:
                        ref[fk] = val
                evidence_refs.append(ref)
    if not evidence_refs:
        return None
    source_checks = [
        "Inspect same-repo application surfaces (APIs, models, workers) for exposure drift.",
        "Check runtime facts for contract assumptions that changed symbols may violate.",
    ]
    negative_checks = [
        "If the changed symbols are internal utilities with no direct exposure to application surfaces or runtime facts, drift is unlikely.",
    ]
    lead_ids = _lead_ids_for_fields(review_leads, ("changed_symbols",))
    return _make_hypothesis(
        risk_type="application_surface_contract_drift",
        confidence="medium" if lead_ids else "weak",
        why="Same-repo application surfaces or runtime facts overlap the changed code and can shift application-level contracts.",
        evidence_refs=evidence_refs[:5],
        source_checks=source_checks,
        supporting_lead_ids=lead_ids[:10],
        negative_checks=negative_checks,
    )


def _is_test_file(path: str) -> bool:
    parts = path.replace("\\", "/").split("/")
    for part in parts[:-1]:
        if part in _TEST_PATH_SEGMENTS:
            return True
    stem = parts[-1]
    base = stem.rsplit(".", 1)[0] if "." in stem else stem
    if base.startswith("test_") or base.endswith("_test") or base.startswith("spec_") or base.endswith("_spec"):
        return True
    # Co-located test file patterns: src/foo.test.ts or src/Bar.spec.tsx
    if ".test." in stem or ".spec." in stem:
        return True
    return False


def _is_config_file(path: str) -> bool:
    parts = path.replace("\\", "/").split("/")
    filename = parts[-1]
    if filename in _CONFIG_BASENAMES:
        return True
    dot_pos = filename.rfind(".")
    ext = filename[dot_pos:] if dot_pos >= 0 else ""
    return ext in _CONFIG_EXTENSIONS


def _is_stylesheet_file(path: str) -> bool:
    dot_pos = path.rfind(".")
    ext = path[dot_pos:] if dot_pos >= 0 else ""
    return ext in _STYLESHEET_EXTENSIONS


def _test_or_config_masks_runtime_change(
    *,
    changed_files: list[str],
    changed_symbols: list[JsonObject],
    direct_callers: list[JsonObject],
    direct_callees: list[JsonObject],
    review_leads: JsonObject,
) -> JsonObject | None:
    has_test_or_config = any(_is_test_file(f) or _is_config_file(f) for f in changed_files)
    if not has_test_or_config:
        return None
    has_code_leads = bool(changed_symbols or direct_callers or direct_callees)
    if not has_code_leads:
        return None
    tc_files = [f for f in changed_files if _is_test_file(f) or _is_config_file(f)]
    evidence_refs: list[JsonObject] = [{"path": f} for f in tc_files[:5]]
    source_checks = [
        "Verify test or config changes do not mask a runtime behavioral change.",
        "Check whether changed config values alter runtime contracts.",
    ]
    negative_checks = [
        "If the test or config changes are purely additive (new tests, new config keys) with no modification of existing behavior, masking is unlikely.",
    ]
    lead_ids = _lead_ids_for_fields(review_leads, ("changed_symbols",))
    return _make_hypothesis(
        risk_type="test_or_config_masks_runtime_change",
        confidence="weak",
        why="Test or config files changed alongside code leads; a runtime behavioral change may be masked by test or config edits.",
        evidence_refs=evidence_refs,
        source_checks=source_checks,
        supporting_lead_ids=lead_ids[:10],
        negative_checks=negative_checks,
    )


def _low_coverage_stylesheet_gap(
    *,
    changed_files: list[str],
    low_coverage: bool,
    review_leads: JsonObject,
) -> JsonObject | None:
    if not low_coverage:
        return None
    stylesheet_files = [f for f in changed_files if _is_stylesheet_file(f)]
    if not stylesheet_files:
        return None
    evidence_refs: list[JsonObject] = [{"path": f} for f in stylesheet_files[:5]]
    source_checks = [
        "Inspect stylesheet changes manually; KG has limited coverage of CSS/styling contracts.",
    ]
    negative_checks = [
        "If the stylesheet changes are scoped to a single isolated component with no shared class names or variables, broader impact is unlikely.",
    ]
    lead_ids = _lead_ids_for_fields(review_leads, ("changed_symbols", "source_coordinates"))
    return _make_hypothesis(
        risk_type="low_coverage_stylesheet_gap",
        confidence="weak",
        why="Stylesheet files changed but the KG has low coverage of styling contracts, so impact must be inspected manually.",
        evidence_refs=evidence_refs,
        source_checks=source_checks,
        supporting_lead_ids=lead_ids[:10],
        negative_checks=negative_checks,
    )


def _path_extension(path: str) -> str:
    dot_pos = path.rfind(".")
    return path[dot_pos:] if dot_pos >= 0 else ""


def _short_name(sym: JsonObject) -> str:
    """Return the simple (last-segment) name from a symbol row.

    Production rows from _symbol_result carry 'qualname' (short), 'display_name'
    and 'qualified_name' (module-qualified).  Unit-test synthetic rows may use 'name'.
    """
    qualname = sym.get("qualname")
    if qualname:
        return str(qualname).rsplit(".", 1)[-1]
    for key in ("display_name", "qualified_name"):
        val = sym.get(key)
        if val:
            return str(val).rsplit(".", 1)[-1]
    return str(sym.get("name") or "")


def _is_component_symbol(sym: JsonObject) -> bool:
    name = _short_name(sym)
    path = sym.get("path") or ""
    return bool(name) and name[0].isupper() and _path_extension(path) in _FRONTEND_COMPONENT_EXTENSIONS


def _is_hook_symbol(sym: JsonObject) -> bool:
    name = _short_name(sym)
    path = sym.get("path") or ""
    return (
        len(name) > 3
        and name.startswith("use")
        and name[3].isupper()
        and _path_extension(path) in _FRONTEND_HOOK_EXTENSIONS
    )


def _is_component_name(name: str) -> bool:
    return bool(name) and name[0].isupper()


def _is_hook_name(name: str) -> bool:
    return len(name) > 3 and name.startswith("use") and name[3].isupper()


def _edges_touch_names(edges: list[JsonObject], names: set[str]) -> bool:
    """Return True if any edge subject/object matches a name in *names*.

    Matches bare names (unit-test rows) and qualified names like "module.fn"
    (production _fact_result rows) where the last segment equals a name in *names*.
    """
    for edge in edges:
        for field in ("subject", "object"):
            val = str(edge.get(field) or "")
            if not val:
                continue
            if val in names:
                return True
            seg = val.rsplit(".", 1)[-1]
            if seg in names:
                return True
    return False


def _component_list_render_identity_drift(
    *,
    changed_symbols: list[JsonObject],
    direct_callers: list[JsonObject],
    direct_callees: list[JsonObject],
    review_leads: JsonObject,
) -> JsonObject | None:
    component_syms = [s for s in changed_symbols if _is_component_symbol(s)]
    if not component_syms:
        return None
    # Require >=1 edge touching a COMPONENT symbol specifically, not any changed symbol.
    component_names: set[str] = set()
    for s in component_syms:
        short = _short_name(s)
        if short:
            component_names.add(short)
        for key in ("qualname", "qualified_name", "display_name"):
            val = s.get(key)
            if val:
                component_names.add(str(val))
    if not _edges_touch_names(direct_callers, component_names) and not _edges_touch_names(direct_callees, component_names):
        return None
    evidence_refs: list[JsonObject] = []
    for sym in component_syms[:5]:
        ref: JsonObject = {}
        for key in ("path", "qualname", "name", "kind"):
            val = sym.get(key)
            if val is not None:
                ref[key] = val
        if ref:
            evidence_refs.append(ref)
    # supporting_lead_ids: component symbol rows + edge rows touching a component name.
    lead_ids: list[str] = []
    for row in review_leads.get("changed_symbols") or []:
        if not isinstance(row, dict):
            continue
        lid = row.get("lead_id")
        if not isinstance(lid, str) or not lid:
            continue
        # match by path to component symbol paths
        row_path = row.get("path") or ""
        if any(row_path == (s.get("path") or "") for s in component_syms):
            lead_ids.append(lid)
    for field in ("direct_callers", "direct_callees"):
        for row in review_leads.get(field) or []:
            if not isinstance(row, dict):
                continue
            lid = row.get("lead_id")
            if not isinstance(lid, str) or not lid:
                continue
            if _edges_touch_names([row], component_names):
                lead_ids.append(lid)
    confidence = "medium" if lead_ids else "weak"
    source_checks = [
        "Inspect changed component render paths for list keys and branch parity.",
        "Check computed values against rendered output to detect identity drift.",
    ]
    negative_checks = [
        "If the component renders a static list with stable keys and no memoization dependency changed, identity drift is unlikely.",
    ]
    # Build postable_claim from first component symbol
    postable_claim: str | None = None
    if component_syms:
        first_comp = _short_name(component_syms[0])
        if first_comp:
            postable_claim = (
                f"{first_comp} has changed; verify list keys and child rendering identity are stable."
            )
    return _make_hypothesis(
        risk_type="component_list_render_identity_drift",
        confidence=confidence,
        why="Changed component symbols have call edges; list/child rendering identity (keys, memoization, branch parity) may have drifted.",
        evidence_refs=evidence_refs,
        source_checks=source_checks,
        supporting_lead_ids=lead_ids[:10],
        negative_checks=negative_checks,
        postable_claim=postable_claim,
    )


def _hook_gate_render_mismatch(
    *,
    changed_symbols: list[JsonObject],
    direct_callers: list[JsonObject],
    direct_callees: list[JsonObject],
    review_leads: JsonObject,
) -> JsonObject | None:
    hook_syms = [s for s in changed_symbols if _is_hook_symbol(s)]
    if not hook_syms:
        return None
    hook_names = {_short_name(s) for s in hook_syms if _short_name(s)}
    has_consumer_edge = False
    for edge in direct_callers:
        subj = str(edge.get("subject") or "")
        obj_ = str(edge.get("object") or "")
        subj_seg = subj.rsplit(".", 1)[-1]
        obj_seg = obj_.rsplit(".", 1)[-1]
        if (obj_ in hook_names or obj_seg in hook_names) and (
            _is_component_name(subj_seg) or _is_hook_name(subj_seg)
        ):
            has_consumer_edge = True
            break
        if (subj in hook_names or subj_seg in hook_names) and (
            _is_component_name(obj_seg) or _is_hook_name(obj_seg)
        ):
            has_consumer_edge = True
            break
    if not has_consumer_edge:
        for edge in direct_callees:
            subj = str(edge.get("subject") or "")
            obj_ = str(edge.get("object") or "")
            subj_seg = subj.rsplit(".", 1)[-1]
            obj_seg = obj_.rsplit(".", 1)[-1]
            if (subj in hook_names or subj_seg in hook_names) and (
                _is_component_name(obj_seg) or _is_hook_name(obj_seg)
            ):
                has_consumer_edge = True
                break
            if (obj_ in hook_names or obj_seg in hook_names) and (
                _is_component_name(subj_seg) or _is_hook_name(subj_seg)
            ):
                has_consumer_edge = True
                break
    evidence_refs: list[JsonObject] = []
    for sym in hook_syms[:5]:
        ref: JsonObject = {}
        for key in ("path", "qualname", "name", "kind"):
            val = sym.get(key)
            if val is not None:
                ref[key] = val
        if ref:
            evidence_refs.append(ref)
    # supporting_lead_ids: hook symbol rows + edge rows touching a hook name with component/hook endpoint.
    lead_ids = []
    for row in review_leads.get("changed_symbols") or []:
        if not isinstance(row, dict):
            continue
        lid = row.get("lead_id")
        if not isinstance(lid, str) or not lid:
            continue
        row_path = row.get("path") or ""
        if any(row_path == (s.get("path") or "") for s in hook_syms):
            lead_ids.append(lid)
    for field in ("direct_callers", "direct_callees"):
        for row in review_leads.get(field) or []:
            if not isinstance(row, dict):
                continue
            lid = row.get("lead_id")
            if not isinstance(lid, str) or not lid:
                continue
            subj = str(row.get("subject") or "")
            obj_ = str(row.get("object") or "")
            subj_seg = subj.rsplit(".", 1)[-1]
            obj_seg = obj_.rsplit(".", 1)[-1]
            # Mirror the consumer-edge detector: only include edges where one end
            # is a hook name and the other end is a component or hook.
            is_consumer_edge = (
                (
                    (obj_ in hook_names or obj_seg in hook_names)
                    and (_is_component_name(subj_seg) or _is_hook_name(subj_seg))
                )
                or (
                    (subj in hook_names or subj_seg in hook_names)
                    and (_is_component_name(obj_seg) or _is_hook_name(obj_seg))
                )
            )
            if is_consumer_edge:
                lead_ids.append(lid)
    confidence = "medium" if has_consumer_edge else "weak"
    source_checks = [
        "Compare hook return contract against each consuming call site's usage and render gate.",
        "Verify components consuming this hook handle all return states, including loading and error.",
    ]
    negative_checks = [
        "If the hook's return shape is unchanged and only its internal implementation changed, render gate mismatch is unlikely.",
    ]
    # postable_claim from first hook symbol
    postable_claim: str | None = None
    if hook_syms:
        first_hook = _short_name(hook_syms[0])
        if first_hook:
            postable_claim = (
                f"{first_hook} return contract may have shifted; components consuming it may render incorrectly."
            )
    return _make_hypothesis(
        risk_type="hook_gate_render_mismatch",
        confidence=confidence,
        why="A changed hook may have a shifted gate or permission contract; components consuming it may diverge in rendered output.",
        evidence_refs=evidence_refs,
        source_checks=source_checks,
        supporting_lead_ids=lead_ids[:10],
        negative_checks=negative_checks,
        postable_claim=postable_claim,
    )


def _test_locks_in_regression(
    *,
    changed_files: list[str],
    changed_symbols: list[JsonObject],
    direct_callers: list[JsonObject],
    direct_callees: list[JsonObject],
    review_leads: JsonObject,
) -> JsonObject | None:
    has_test_file = any(_is_test_file(f) for f in changed_files)
    if not has_test_file:
        return None
    non_test_syms = [s for s in changed_symbols if not _is_test_file(s.get("path") or "")]
    if not non_test_syms:
        return None
    # Require >=1 edge touching a changed NON-TEST code symbol, not any changed symbol.
    non_test_names: set[str] = set()
    for s in non_test_syms:
        short = _short_name(s)
        if short:
            non_test_names.add(short)
        for key in ("qualname", "qualified_name", "display_name"):
            val = s.get(key)
            if val:
                non_test_names.add(str(val))
    if not _edges_touch_names(direct_callers, non_test_names) and not _edges_touch_names(direct_callees, non_test_names):
        return None
    test_files = [f for f in changed_files if _is_test_file(f)]
    evidence_refs: list[JsonObject] = [{"path": f} for f in test_files[:5]]
    source_checks = [
        "Verify updated tests exercise behavior (interaction and outcome), not just presence or text.",
        "Check whether test assertions reflect new invariants or lock in a regression.",
    ]
    negative_checks = [
        "If test assertions verify the intended new behavior (not the old broken behavior) and cover both success and failure paths, locking in a regression is unlikely.",
    ]
    # supporting_lead_ids: non-test symbol rows + edge rows touching a non-test symbol name.
    lead_ids = []
    for row in review_leads.get("changed_symbols") or []:
        if not isinstance(row, dict):
            continue
        lid = row.get("lead_id")
        if not isinstance(lid, str) or not lid:
            continue
        row_path = row.get("path") or ""
        if any(row_path == (s.get("path") or "") for s in non_test_syms):
            lead_ids.append(lid)
    for field in ("direct_callers", "direct_callees"):
        for row in review_leads.get(field) or []:
            if not isinstance(row, dict):
                continue
            lid = row.get("lead_id")
            if not isinstance(lid, str) or not lid:
                continue
            if _edges_touch_names([row], non_test_names):
                lead_ids.append(lid)
    return _make_hypothesis(
        risk_type="test_locks_in_regression",
        confidence="weak",
        why="Updated test files and changed code symbols share call edges; updated tests may assert the new (possibly broken) behavior.",
        evidence_refs=evidence_refs,
        source_checks=source_checks,
        supporting_lead_ids=lead_ids[:10],
        negative_checks=negative_checks,
    )


def _changed_symbol_entity_ids(changed_symbols: list[JsonObject]) -> set[str]:
    """Return the set of entity ids for changed-symbol rows.

    _symbol_result rows carry 'symbol_id' (= entity["entity_id"]).  Synthetic
    test rows may omit it; those rows simply contribute nothing to the set.
    """
    ids: set[str] = set()
    for sym in changed_symbols:
        eid = sym.get("symbol_id")
        if isinstance(eid, str) and eid:
            ids.add(eid)
    return ids


def _changed_symbol_paths(changed_symbols: list[JsonObject]) -> set[str]:
    paths: set[str] = set()
    for sym in changed_symbols:
        p = sym.get("path")
        if isinstance(p, str) and p:
            paths.add(_normalize_path(p))
    return paths


def _changed_context_paths(changed_symbols: list[JsonObject], changed_files: list[str]) -> set[str]:
    """Changed-symbol paths, falling back to changed files when no symbols resolved.

    Signals reaching the families are already repo- and range-scoped by
    _review_context_risk_signals, so the file fallback cannot widen scope; it
    only lets in-range signals fire when symbol anchoring came up empty
    (supporting_lead_ids stays empty there, so confidence stays weak).
    """
    paths = _changed_symbol_paths(changed_symbols)
    if paths:
        return paths
    return {_normalize_path(f) for f in changed_files if isinstance(f, str) and f}


def _signal_matches_changed_context(
    signal: JsonObject,
    changed_entity_ids: set[str],
    changed_paths: set[str],
) -> bool:
    """Return True if this risk_signal is attached to a changed symbol or file.

    Matching logic (in priority order):
      1. signal.subject_id in changed_entity_ids
      2. evidence bytes_ref.path in changed_paths
    """
    subject_id = signal.get("subject_id")
    if isinstance(subject_id, str) and subject_id in changed_entity_ids:
        return True
    # Fall back to evidence path matching.
    for ev in signal.get("_evidence", []):
        if not isinstance(ev, dict):
            continue
        br = ev.get("bytes_ref")
        if isinstance(br, dict):
            p = br.get("path")
            if isinstance(p, str) and _normalize_path(p) in changed_paths:
                return True
    return False


def _evidence_refs_from_risk_signals(signals: list[JsonObject]) -> list[JsonObject]:
    """Build evidence_refs from risk signal evidence bytes_refs."""
    refs: list[JsonObject] = []
    for sig in signals:
        for ev in sig.get("_evidence", []):
            if not isinstance(ev, dict):
                continue
            br = ev.get("bytes_ref")
            if not isinstance(br, dict):
                continue
            ref: JsonObject = {}
            for key in ("repo", "path", "line_start", "line_end"):
                val = br.get(key)
                if val is not None:
                    ref[key] = val
            q = sig.get("qualifier")
            if isinstance(q, dict):
                callee = q.get("callee")
                if callee:
                    ref["callee"] = callee
                qualname = q.get("qualname")
                if qualname:
                    ref["qualname"] = qualname
            if ref:
                refs.append(ref)
    return refs[:5]


def _lead_ids_for_signal_subjects(
    signals: list[JsonObject],
    review_leads: JsonObject,
) -> list[str]:
    """Return lead_ids from review_leads["changed_symbols"] whose entity/path matches a signal subject."""
    sig_entity_ids: set[str] = set()
    sig_paths: set[str] = set()
    for sig in signals:
        eid = sig.get("subject_id")
        if isinstance(eid, str) and eid:
            sig_entity_ids.add(eid)
        for ev in sig.get("_evidence", []):
            if not isinstance(ev, dict):
                continue
            br = ev.get("bytes_ref")
            if isinstance(br, dict):
                p = br.get("path")
                if isinstance(p, str) and p:
                    sig_paths.add(_normalize_path(p))

    lead_ids: list[str] = []
    for row in review_leads.get("changed_symbols") or []:
        if not isinstance(row, dict):
            continue
        lid = row.get("lead_id")
        if not isinstance(lid, str) or not lid:
            continue
        # Stamped review_leads rows carry symbol_id (= entity["entity_id"] from _symbol_result).
        eid = row.get("symbol_id")
        if isinstance(eid, str) and eid in sig_entity_ids:
            lead_ids.append(lid)
            continue
        p = row.get("path")
        if isinstance(p, str) and p:
            if _normalize_path(p) in sig_paths:
                lead_ids.append(lid)
    return lead_ids


def _why_from_signals(signals: list[JsonObject], family: str) -> str:
    """Build a why string from actual signal qualifier data (no fabrication)."""
    callees: list[str] = []
    qualnames: list[str] = []
    for sig in signals[:3]:
        q = sig.get("qualifier")
        if not isinstance(q, dict):
            continue
        c = q.get("callee")
        if isinstance(c, str) and c and c not in callees:
            callees.append(c)
        qn = q.get("qualname")
        if isinstance(qn, str) and qn and qn not in qualnames:
            qualnames.append(qn)
    count = len(signals)
    count_str = f"{count} signal{'s' if count != 1 else ''}"
    if family == "async_lifecycle":
        if callees:
            callee_str = ", ".join(callees[:2])
            return (
                f"{count_str} ({callee_str}) in changed symbols have async side-effect calls "
                f"whose completion or persistence may not be coupled to the changed mutation path."
            )
        return (
            f"{count_str} in changed symbols have async side-effect calls "
            f"whose completion or persistence may not be coupled to the changed mutation path."
        )
    # swallowed_exception
    if qualnames:
        qualname_str = ", ".join(qualnames[:2])
        return (
            f"{count_str} in changed symbols ({qualname_str}) swallow broad exceptions; "
            f"errors converted to silent state must be intentional and not mask a caller-visible state transition."
        )
    return (
        f"{count_str} in changed symbols swallow broad exceptions; "
        f"errors converted to silent state must be intentional and not mask a caller-visible state transition."
    )


def _async_side_effect_lifecycle_drift(
    *,
    changed_symbols: list[JsonObject],
    changed_files: list[str],
    direct_callers: list[JsonObject],
    direct_callees: list[JsonObject],
    risk_signals: list[JsonObject],
    review_leads: JsonObject,
) -> JsonObject | None:
    """Trigger: >=1 code_risk_signal with async-lifecycle family on changed symbols/files (entity_id or path)."""
    changed_entity_ids = _changed_symbol_entity_ids(changed_symbols)
    changed_paths = _changed_context_paths(changed_symbols, changed_files)
    matching = [
        sig for sig in risk_signals
        if sig.get("qualifier", {}).get("risk_family") in _ASYNC_LIFECYCLE_FAMILIES
        and _signal_matches_changed_context(sig, changed_entity_ids, changed_paths)
    ]
    if not matching:
        return None
    evidence_refs = _evidence_refs_from_risk_signals(matching)
    lead_ids = _lead_ids_for_signal_subjects(matching, review_leads)
    has_direct_edge = bool(direct_callers or direct_callees)
    confidence = "medium" if (lead_ids and has_direct_edge) else "weak"
    source_checks = [
        "Trace each flagged call site; verify the async call is awaited or its result is reconciled with any paired persistent-state change.",
        "Check whether the mutation path that contains the async call has a compensating rollback or retry if the async work fails.",
    ]
    negative_checks = [
        "If the async call result is intentionally fire-and-forget and the mutation path documents this, the hypothesis does not apply.",
    ]
    # Build postable_claim from first signal's qualifier (no fabrication — null when data absent)
    postable_claim: str | None = None
    cause: JsonObject | None = None
    consequence: JsonObject | None = None
    first_sig = matching[0] if matching else None
    if first_sig:
        q = first_sig.get("qualifier") or {}
        callee = q.get("callee")
        qualname = q.get("qualname")
        if callee and qualname:
            postable_claim = (
                f"{qualname} contains an async call to {callee} whose completion may not be "
                "coupled to the changed mutation path."
            )
        elif qualname:
            postable_claim = (
                f"{qualname} contains an async side-effect call whose completion may not be "
                "coupled to the changed mutation path."
            )
        evs = first_sig.get("_evidence") or []
        if evs and isinstance(evs[0], dict):
            br = evs[0].get("bytes_ref")
            if isinstance(br, dict):
                cause = {k: br[k] for k in ("repo", "path", "line_start", "line_end") if k in br}
    if evidence_refs:
        first_ref = evidence_refs[0]
        consequence = {k: first_ref[k] for k in ("repo", "path", "line_start", "line_end", "qualname", "callee") if k in first_ref} or None
    return _make_hypothesis(
        risk_type="async_side_effect_lifecycle_drift",
        confidence=confidence,
        why=_why_from_signals(matching, "async_lifecycle"),
        evidence_refs=evidence_refs,
        source_checks=source_checks,
        supporting_lead_ids=lead_ids[:10],
        concrete_invariant=(
            "Side-effect calls and their completion or persistence must stay coupled: "
            "an async call in a mutation path must be awaited, returned, or reconciled "
            "before the mutation is considered complete."
        ),
        postable_claim=postable_claim,
        cause=cause or None,
        consequence=consequence or None,
        negative_checks=negative_checks,
    )


def _swallowed_exception_state_drift(
    *,
    changed_symbols: list[JsonObject],
    changed_files: list[str],
    direct_callers: list[JsonObject],
    direct_callees: list[JsonObject],
    risk_signals: list[JsonObject],
    review_leads: JsonObject,
) -> JsonObject | None:
    """Trigger: >=1 code_risk_signal with swallowed_exception family on changed symbols/files (entity_id or path)."""
    changed_entity_ids = _changed_symbol_entity_ids(changed_symbols)
    changed_paths = _changed_context_paths(changed_symbols, changed_files)
    matching = [
        sig for sig in risk_signals
        if sig.get("qualifier", {}).get("risk_family") == "swallowed_exception"
        and _signal_matches_changed_context(sig, changed_entity_ids, changed_paths)
    ]
    if not matching:
        return None
    evidence_refs = _evidence_refs_from_risk_signals(matching)
    lead_ids = _lead_ids_for_signal_subjects(matching, review_leads)
    has_direct_edge = bool(direct_callers or direct_callees)
    confidence = "medium" if (lead_ids and has_direct_edge) else "weak"
    source_checks = [
        "Trace each flagged broad-exception handler; verify it cannot mask a state transition the caller depends on.",
        "Check whether the handler's silent outcome (pass/continue/constant return) is documented as intentional in the changed code.",
    ]
    negative_checks = [
        "If the exception handler is intentional and documented (e.g. optional cleanup, non-fatal fallback), the hypothesis does not apply.",
    ]
    postable_claim: str | None = None
    cause: JsonObject | None = None
    consequence: JsonObject | None = None
    first_sig = matching[0] if matching else None
    if first_sig:
        q = first_sig.get("qualifier") or {}
        qualname = q.get("qualname")
        if qualname:
            postable_claim = (
                f"{qualname} swallows a broad exception; the silent outcome may mask a state transition callers depend on."
            )
        evs = first_sig.get("_evidence") or []
        if evs and isinstance(evs[0], dict):
            br = evs[0].get("bytes_ref")
            if isinstance(br, dict):
                cause = {k: br[k] for k in ("repo", "path", "line_start", "line_end") if k in br}
    if evidence_refs:
        first_ref = evidence_refs[0]
        consequence = {k: first_ref[k] for k in ("repo", "path", "line_start", "line_end", "qualname") if k in first_ref} or None
    return _make_hypothesis(
        risk_type="swallowed_exception_state_drift",
        confidence=confidence,
        why=_why_from_signals(matching, "swallowed_exception"),
        evidence_refs=evidence_refs,
        source_checks=source_checks,
        supporting_lead_ids=lead_ids[:10],
        concrete_invariant=(
            "Errors converted to silent state must be intentional: "
            "a broad exception handler that discards an error must not mask a state transition "
            "the caller observes or relies on."
        ),
        postable_claim=postable_claim,
        cause=cause or None,
        consequence=consequence or None,
        negative_checks=negative_checks,
    )


def _call_result_identity_comparison_semantics(
    *,
    changed_symbols: list[JsonObject],
    changed_files: list[str],
    risk_signals: list[JsonObject],
    review_leads: JsonObject,
    direct_callers: list[JsonObject],
    direct_callees: list[JsonObject],
) -> JsonObject | None:
    """Trigger: >=1 code_risk_signal with call_result_identity_comparison family on changed symbols/files."""
    changed_entity_ids = _changed_symbol_entity_ids(changed_symbols)
    changed_paths = _changed_context_paths(changed_symbols, changed_files)
    matching = [
        sig for sig in risk_signals
        if sig.get("qualifier", {}).get("risk_family") in _CALL_RESULT_IDENTITY_FAMILY
        and _signal_matches_changed_context(sig, changed_entity_ids, changed_paths)
    ]
    if not matching:
        return None
    evidence_refs = _evidence_refs_from_risk_signals(matching)
    for ref, sig in zip(evidence_refs, matching):
        q = sig.get("qualifier")
        if isinstance(q, dict):
            for key in ("callee_left", "callee_right"):
                val = q.get(key)
                if val:
                    ref[key] = val
    lead_ids = _lead_ids_for_signal_subjects(matching, review_leads)
    count = len(matching)
    count_str = f"{count} signal{'s' if count != 1 else ''}"
    callees_left: list[str] = []
    callees_right: list[str] = []
    for sig in matching[:3]:
        q = sig.get("qualifier")
        if isinstance(q, dict):
            cl = q.get("callee_left")
            if isinstance(cl, str) and cl and cl not in callees_left:
                callees_left.append(cl)
            cr = q.get("callee_right")
            if isinstance(cr, str) and cr and cr not in callees_right:
                callees_right.append(cr)
    if callees_left or callees_right:
        sides = " vs ".join(filter(None, [", ".join(callees_left[:2]), ", ".join(callees_right[:2])]))
        why = (
            f"{count_str} ({sides}) in changed symbols compare call-expression results with "
            f"=== or !==; object-returning calls compare by reference, not value."
        )
    else:
        why = (
            f"{count_str} in changed symbols compare call-expression results with "
            f"=== or !==; object-returning calls compare by reference, not value."
        )
    source_checks = [
        "Verify === or !== comparisons between call expressions use .isSame()/.equals()/.isEqual() when value equality is intended.",
        "Check whether both call sides return objects or class instances that compare by reference rather than by value.",
    ]
    negative_checks = [
        "If both sides are known to return primitives (string, number, boolean), reference equality is correct and the hypothesis does not apply.",
    ]
    concrete_invariant = "Object-returning call expressions compared with === or !== test reference identity, not value equality; use the type's equality method instead."
    confidence = "medium" if (lead_ids and (direct_callers or direct_callees)) else "weak"
    # postable_claim from first signal qualifier (no fabrication)
    postable_claim: str | None = None
    cause: JsonObject | None = None
    consequence: JsonObject | None = None
    first_sig = matching[0] if matching else None
    if first_sig:
        q = first_sig.get("qualifier") or {}
        cl = q.get("callee_left")
        cr = q.get("callee_right")
        qualname = q.get("qualname")
        if cl and cr:
            postable_claim = (
                f"{qualname or 'Changed symbol'} compares {cl} === {cr}; "
                "if either returns an object, this is reference equality, not value equality."
            )
        elif qualname:
            postable_claim = (
                f"{qualname} uses === or !== on call-expression results; object-returning calls compare by reference."
            )
        evs = first_sig.get("_evidence") or []
        if evs and isinstance(evs[0], dict):
            br = evs[0].get("bytes_ref")
            if isinstance(br, dict):
                cause = {k: br[k] for k in ("repo", "path", "line_start", "line_end") if k in br}
    if evidence_refs:
        first_ref = evidence_refs[0]
        consequence = {k: first_ref[k] for k in ("repo", "path", "line_start", "line_end", "qualname", "callee") if k in first_ref} or None
    return _make_hypothesis(
        risk_type="call_result_identity_comparison_semantics",
        confidence=confidence,
        why=why,
        evidence_refs=evidence_refs,
        source_checks=source_checks,
        supporting_lead_ids=lead_ids[:10],
        concrete_invariant=concrete_invariant,
        postable_claim=postable_claim,
        cause=cause or None,
        consequence=consequence or None,
        negative_checks=negative_checks,
    )


def _destructive_mutation_test_gap(
    *,
    changed_symbols: list[JsonObject],
    changed_files: list[str],
    direct_callees: list[JsonObject],
    review_leads: JsonObject,
) -> JsonObject | None:
    """Trigger: changed symbol calls a destructive operation with no test file in the changeset."""
    if any(_is_test_file(f) for f in changed_files):
        return None
    changed_names: set[str] = set()
    for sym in changed_symbols:
        short = _short_name(sym)
        if short:
            changed_names.add(short)
        for key in ("qualname", "qualified_name", "display_name"):
            val = sym.get(key)
            if val:
                changed_names.add(str(val))
    matching_callees: list[JsonObject] = []
    for row in direct_callees:
        qualifier = row.get("qualifier") or {}
        call_str = qualifier.get("call") or ""
        call_lower = call_str.lower()
        is_destructive = (
            any(call_lower.endswith(sfx) for sfx in _DESTRUCTIVE_CALL_SUFFIXES)
            or (call_lower.rsplit(".", 1)[-1] if "." in call_lower else call_lower) in _DESTRUCTIVE_CALL_BARE
        )
        if not is_destructive:
            continue
        subj = str(row.get("subject") or "")
        subj_seg = subj.rsplit(".", 1)[-1]
        if subj in changed_names or subj_seg in changed_names:
            matching_callees.append(row)
    if not matching_callees:
        return None
    evidence_refs: list[JsonObject] = []
    for row in matching_callees[:5]:
        ref: JsonObject = {}
        qualifier = row.get("qualifier") or {}
        call_str = qualifier.get("call")
        if call_str:
            ref["call"] = call_str
        subj = row.get("subject")
        if subj:
            ref["qualname"] = str(subj)
        for ev in (row.get("evidence") or []):
            if not isinstance(ev, dict):
                continue
            br = ev.get("bytes_ref")
            if isinstance(br, dict):
                for coord_key in ("repo", "path", "line_start", "line_end"):
                    val = br.get(coord_key)
                    if val is not None:
                        ref[coord_key] = val
                if "path" in ref:
                    break
        if ref:
            evidence_refs.append(ref)
    # Lead matching joins the callee-row subject (the calling symbol) to a
    # changed-symbol lead by full qualified name, or by short name anchored to
    # the call-site path.  A bare short-name match is too loose: a subject
    # segment like "handler" would match every changed symbol named "handler"
    # across unrelated files and inflate supporting_lead_ids.
    lead_ids: list[str] = []
    callee_full_names: set[str] = set()
    callee_name_path_keys: set[tuple[str, str]] = set()
    for row in matching_callees:
        subj = str(row.get("subject") or "")
        if not subj:
            continue
        callee_full_names.add(subj)
        subj_seg = subj.rsplit(".", 1)[-1]
        for ev in row.get("evidence") or []:
            if not isinstance(ev, dict):
                continue
            br = ev.get("bytes_ref")
            if isinstance(br, dict):
                p = br.get("path")
                if isinstance(p, str) and p:
                    callee_name_path_keys.add((subj_seg, _normalize_path(p)))
    for row in review_leads.get("changed_symbols") or []:
        if not isinstance(row, dict):
            continue
        lid = row.get("lead_id")
        if not isinstance(lid, str) or not lid:
            continue
        row_qualified = str(row.get("qualified_name") or "")
        row_qualname = str(row.get("qualname") or "")
        row_short = row_qualname.rsplit(".", 1)[-1] if row_qualname else ""
        row_path = _normalize_path(str(row.get("path") or ""))
        if row_qualified and row_qualified in callee_full_names:
            lead_ids.append(lid)
            continue
        if row_qualname and row_qualname in callee_full_names:
            lead_ids.append(lid)
            continue
        if row_short and row_path and (row_short, row_path) in callee_name_path_keys:
            lead_ids.append(lid)
    why = "Changed symbol(s) perform destructive persistence operations (delete/destroy) with no test file in the changeset; ownership guard and delete path are untested."
    source_checks = [
        "Verify the authorization/ownership guard is tested for the failure path (access denied or not found).",
        "Verify the delete/destroy operation is exercised by at least one test covering both success and no-match cases.",
    ]
    negative_checks = [
        "If a test file already exists elsewhere in the repo that covers the delete/destroy path and authorization guard, this hypothesis does not apply.",
    ]
    concrete_invariant = "A destructive persistence operation must have test coverage for both the happy path and the ownership-guard failure path before shipping."
    confidence = "medium" if lead_ids else "weak"
    # postable_claim from first matching callee (no fabrication)
    postable_claim: str | None = None
    cause: JsonObject | None = None
    consequence: JsonObject | None = None
    if matching_callees:
        first_row = matching_callees[0]
        qualifier = first_row.get("qualifier") or {}
        call_str = qualifier.get("call")
        subj = str(first_row.get("subject") or "")
        if call_str and subj:
            postable_claim = (
                f"{subj} calls {call_str} with no test coverage for the ownership-guard failure path."
            )
        elif call_str:
            postable_claim = (
                f"A changed symbol calls {call_str} with no test coverage for the ownership-guard failure path."
            )
        for ev in (first_row.get("evidence") or []):
            if isinstance(ev, dict):
                br = ev.get("bytes_ref")
                if isinstance(br, dict):
                    cause = {k: br[k] for k in ("repo", "path", "line_start", "line_end") if k in br}
                    if cause:
                        break
    if evidence_refs:
        first_ref = evidence_refs[0]
        consequence = {k: first_ref[k] for k in ("repo", "path", "line_start", "line_end", "qualname", "call") if k in first_ref} or None
    return _make_hypothesis(
        risk_type="destructive_mutation_test_gap",
        confidence=confidence,
        why=why,
        evidence_refs=evidence_refs,
        source_checks=source_checks,
        supporting_lead_ids=lead_ids[:10],
        concrete_invariant=concrete_invariant,
        postable_claim=postable_claim,
        cause=cause or None,
        consequence=consequence or None,
        negative_checks=negative_checks,
    )
