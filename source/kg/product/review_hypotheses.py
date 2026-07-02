from __future__ import annotations

from source.kg.core.models import JsonObject
from source.kg.product.review_attribution import review_stable_id

_TEST_PATH_SEGMENTS = frozenset({"test", "tests", "spec", "specs", "__tests__"})
_CONFIG_EXTENSIONS = frozenset({".json", ".yaml", ".yml", ".toml", ".ini", ".env"})
_CONFIG_BASENAMES = frozenset({"Dockerfile"})
_STYLESHEET_EXTENSIONS = frozenset({".css", ".scss", ".sass", ".less"})
_FRONTEND_COMPONENT_EXTENSIONS = frozenset({".tsx", ".jsx"})
_FRONTEND_HOOK_EXTENSIONS = frozenset({".ts", ".tsx", ".js", ".jsx"})
_CONFIDENCE_RANK = {"strong": 2, "medium": 1, "weak": 0}
_FRAMEWORK_IMPACT_KEYS = ("changed_models", "model_fields", "model_relations", "serializers", "views", "tasks")
_RUNTIME_SURFACE_KEYS = ("endpoints", "endpoint_consumers", "event_channels", "deploy_mappings")


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
    hypotheses.sort(
        key=lambda row: (
            -len(row.get("supporting_lead_ids") or []),
            -len(row.get("evidence_refs") or []),
            -_CONFIDENCE_RANK.get(str(row.get("confidence")), 0),
            str(row.get("risk_type") or ""),
        )
    )
    return hypotheses[:5]


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
) -> JsonObject:
    row: JsonObject = {
        "risk_type": risk_type,
        "confidence": confidence,
        "why": why,
        "evidence_refs": evidence_refs,
        "source_checks": source_checks,
        "supporting_lead_ids": supporting_lead_ids,
    }
    row["hypothesis_id"] = review_stable_id("hypothesis", row, fallback_kind=risk_type)
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
            for key in ("repo", "path", "line_start", "line_end", "qualified_name", "qualname"):
                val = row.get(key)
                if val is not None:
                    ref[key] = val
            caller = row.get("caller_symbol") or row.get("subject")
            if isinstance(caller, dict):
                for key in ("qualified_name", "qualname", "repo", "path"):
                    val = caller.get(key)
                    if val is not None:
                        ref[f"caller_{key}"] = val
            callee = row.get("callee_symbol") or row.get("object")
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
    return base.startswith("test_") or base.endswith("_test") or base.startswith("spec_") or base.endswith("_spec")


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
    changed_names = {_short_name(s) for s in changed_symbols if _short_name(s)}
    if not _edges_touch_names(direct_callers, changed_names) and not _edges_touch_names(direct_callees, changed_names):
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
    lead_ids = _lead_ids_for_fields(review_leads, ("changed_symbols", "direct_callers", "direct_callees"))
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
    lead_ids = _lead_ids_for_fields(review_leads, ("changed_symbols", "direct_callers", "direct_callees"))
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
    changed_names = {_short_name(s) for s in changed_symbols if _short_name(s)}
    if not _edges_touch_names(direct_callers, changed_names) and not _edges_touch_names(direct_callees, changed_names):
        return None
    test_files = [f for f in changed_files if _is_test_file(f)]
    evidence_refs: list[JsonObject] = [{"path": f} for f in test_files[:5]]
    source_checks = [
        "Verify updated tests exercise behavior (interaction and outcome), not just presence or text.",
        "Check whether test assertions reflect new invariants or lock in a regression.",
    ]
    lead_ids = _lead_ids_for_fields(review_leads, ("changed_symbols",))
    return _make_hypothesis(
        risk_type="test_locks_in_regression",
        confidence="weak",
        why="Updated test files and changed code symbols share call edges; updated tests may assert the new (possibly broken) behavior.",
        evidence_refs=evidence_refs,
        source_checks=source_checks,
        supporting_lead_ids=lead_ids[:10],
    )
