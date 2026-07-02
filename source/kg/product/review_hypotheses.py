from __future__ import annotations

from source.kg.core.models import JsonObject
from source.kg.product.review_attribution import hypothesis_stable_id


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

# O2: Specific-class families. Post-1e74bf2 probe evidence (Grafana 106778, limit=25) showed
# specific families generating on real repos but losing visible slots to generics because
# generic families (direct_call_contract_drift) accumulate more supporting leads by construction.
# Stable partition: specific-class first, generic-class second, within each class the existing
# 4-part comparator order is preserved. Mirror slot selection takes the head of this order,
# so hook_gate_render_mismatch outranks direct_call_contract_drift in top_review_hypotheses.
_SPECIFIC_CLASS_FAMILIES: frozenset[str] = frozenset(
    {
        "async_side_effect_lifecycle_drift",
        "component_list_render_identity_drift",
        "hook_gate_render_mismatch",
        "swallowed_exception_state_drift",
        "test_locks_in_regression",
        "low_coverage_stylesheet_gap",
    }
)


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
        direct_callers=direct_callers,
        direct_callees=direct_callees,
        risk_signals=effective_risk_signals,
        review_leads=review_leads,
    )
    if h:
        hypotheses.append(h)
    h = _swallowed_exception_state_drift(
        changed_symbols=changed_symbols,
        direct_callers=direct_callers,
        direct_callees=direct_callees,
        risk_signals=effective_risk_signals,
        review_leads=review_leads,
    )
    if h:
        hypotheses.append(h)
    hypotheses.sort(
        key=lambda row: (
            -len(row.get("supporting_lead_ids") or []),
            -len(row.get("evidence_refs") or []),
            -_CONFIDENCE_RANK.get(str(row.get("confidence")), 0),
            str(row.get("risk_type") or ""),
        )
    )
    cap = 5
    selected = list(hypotheses[:cap])
    # O2: Stable partition — specific-class families before generic-class. The 4-part
    # comparator order is preserved within each class. Mirror slot selection takes the
    # head of this order, so specific families appear in top_review_hypotheses before generics.
    specifics = [h for h in selected if str(h.get("risk_type") or "") in _SPECIFIC_CLASS_FAMILIES]
    generics = [h for h in selected if str(h.get("risk_type") or "") not in _SPECIFIC_CLASS_FAMILIES]
    return specifics + generics


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
) -> JsonObject:
    row: JsonObject = {
        "risk_type": risk_type,
        "confidence": confidence,
        "why": why,
        "evidence_refs": evidence_refs,
        "source_checks": source_checks,
        "supporting_lead_ids": supporting_lead_ids,
    }
    if concrete_invariant is not None:
        row["concrete_invariant"] = concrete_invariant
    row["hypothesis_id"] = hypothesis_stable_id(risk_type, supporting_lead_ids, evidence_refs)
    return row


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
    source_checks = [
        "Verify callers still satisfy the changed symbol's pre/post-conditions.",
        "Check direct callees for interface drift introduced by this change.",
    ]
    return _make_hypothesis(
        risk_type="direct_call_contract_drift",
        confidence=confidence,
        why="Changed symbols have direct callers or callees whose call contracts can drift with this change.",
        evidence_refs=evidence_refs,
        source_checks=source_checks,
        supporting_lead_ids=lead_ids[:10],
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
    lead_ids = _lead_ids_for_fields(review_leads, ("changed_symbols",))
    return _make_hypothesis(
        risk_type="framework_contract_drift",
        confidence="medium" if lead_ids else "weak",
        why="Framework-declared models, serializers, views, or tasks are affected by the changed code.",
        evidence_refs=evidence_refs[:5],
        source_checks=source_checks,
        supporting_lead_ids=lead_ids[:10],
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
    lead_ids = _lead_ids_for_fields(review_leads, ("changed_symbols",))
    return _make_hypothesis(
        risk_type="runtime_endpoint_or_event_contract_drift",
        confidence="medium" if lead_ids else "weak",
        why="The changed repo exposes runtime endpoints, event channels, or deploy mappings that can carry the change to other services.",
        evidence_refs=evidence_refs[:5],
        source_checks=source_checks,
        supporting_lead_ids=lead_ids[:10],
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
    lead_ids = _lead_ids_for_fields(review_leads, ("changed_symbols",))
    return _make_hypothesis(
        risk_type="application_surface_contract_drift",
        confidence="medium" if lead_ids else "weak",
        why="Same-repo application surfaces or runtime facts overlap the changed code and can shift application-level contracts.",
        evidence_refs=evidence_refs[:5],
        source_checks=source_checks,
        supporting_lead_ids=lead_ids[:10],
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
    lead_ids = _lead_ids_for_fields(review_leads, ("changed_symbols",))
    return _make_hypothesis(
        risk_type="test_or_config_masks_runtime_change",
        confidence="weak",
        why="Test or config files changed alongside code leads; a runtime behavioral change may be masked by test or config edits.",
        evidence_refs=evidence_refs,
        source_checks=source_checks,
        supporting_lead_ids=lead_ids[:10],
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
    lead_ids = _lead_ids_for_fields(review_leads, ("changed_symbols", "source_coordinates"))
    return _make_hypothesis(
        risk_type="low_coverage_stylesheet_gap",
        confidence="weak",
        why="Stylesheet files changed but the KG has low coverage of styling contracts, so impact must be inspected manually.",
        evidence_refs=evidence_refs,
        source_checks=source_checks,
        supporting_lead_ids=lead_ids[:10],
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
    return _make_hypothesis(
        risk_type="component_list_render_identity_drift",
        confidence=confidence,
        why="Changed component symbols have call edges; list/child rendering identity (keys, memoization, branch parity) may have drifted.",
        evidence_refs=evidence_refs,
        source_checks=source_checks,
        supporting_lead_ids=lead_ids[:10],
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
    return _make_hypothesis(
        risk_type="hook_gate_render_mismatch",
        confidence=confidence,
        why="A changed hook may have a shifted gate or permission contract; components consuming it may diverge in rendered output.",
        evidence_refs=evidence_refs,
        source_checks=source_checks,
        supporting_lead_ids=lead_ids[:10],
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
    )


def _changed_symbol_entity_ids(changed_symbols: list[JsonObject]) -> set[str]:
    """Return the set of entity_ids for changed-symbol rows.

    Symbol rows from _symbol_result carry 'entity_id' directly.  Synthetic
    test rows may omit it; those rows simply contribute nothing to the set.
    """
    ids: set[str] = set()
    for sym in changed_symbols:
        eid = sym.get("entity_id")
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
    changed_symbols: list[JsonObject],
    review_leads: JsonObject,
) -> list[str]:
    """Return lead_ids from changed_symbols whose path/entity matches a signal subject."""
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
        eid = row.get("entity_id")
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
    direct_callers: list[JsonObject],
    direct_callees: list[JsonObject],
    risk_signals: list[JsonObject],
    review_leads: JsonObject,
) -> JsonObject | None:
    """Trigger: >=1 code_risk_signal with async-lifecycle family on changed symbols (by entity_id or path)."""
    changed_entity_ids = _changed_symbol_entity_ids(changed_symbols)
    changed_paths = _changed_symbol_paths(changed_symbols)
    matching = [
        sig for sig in risk_signals
        if sig.get("qualifier", {}).get("risk_family") in _ASYNC_LIFECYCLE_FAMILIES
        and _signal_matches_changed_context(sig, changed_entity_ids, changed_paths)
    ]
    if not matching:
        return None
    evidence_refs = _evidence_refs_from_risk_signals(matching)
    lead_ids = _lead_ids_for_signal_subjects(
        matching, changed_symbols, review_leads
    )
    has_direct_edge = bool(direct_callers or direct_callees)
    confidence = "medium" if (lead_ids and has_direct_edge) else "weak"
    source_checks = [
        "Trace each flagged call site; verify the async call is awaited or its result is reconciled with any paired persistent-state change.",
        "Check whether the mutation path that contains the async call has a compensating rollback or retry if the async work fails.",
    ]
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
    )


def _swallowed_exception_state_drift(
    *,
    changed_symbols: list[JsonObject],
    direct_callers: list[JsonObject],
    direct_callees: list[JsonObject],
    risk_signals: list[JsonObject],
    review_leads: JsonObject,
) -> JsonObject | None:
    """Trigger: >=1 code_risk_signal with swallowed_exception family on changed symbols (by entity_id or path)."""
    changed_entity_ids = _changed_symbol_entity_ids(changed_symbols)
    changed_paths = _changed_symbol_paths(changed_symbols)
    matching = [
        sig for sig in risk_signals
        if sig.get("qualifier", {}).get("risk_family") == "swallowed_exception"
        and _signal_matches_changed_context(sig, changed_entity_ids, changed_paths)
    ]
    if not matching:
        return None
    evidence_refs = _evidence_refs_from_risk_signals(matching)
    lead_ids = _lead_ids_for_signal_subjects(
        matching, changed_symbols, review_leads
    )
    has_direct_edge = bool(direct_callers or direct_callees)
    confidence = "medium" if (lead_ids and has_direct_edge) else "weak"
    source_checks = [
        "Trace each flagged broad-exception handler; verify it cannot mask a state transition the caller depends on.",
        "Check whether the handler's silent outcome (pass/continue/constant return) is documented as intentional in the changed code.",
    ]
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
    )
