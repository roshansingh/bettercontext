from __future__ import annotations

from source.kg.core.models import JsonObject
from source.kg.product.review_attribution import review_stable_id

_TEST_PATH_SEGMENTS = frozenset({"test", "tests", "spec", "specs", "__tests__"})
_CONFIG_EXTENSIONS = frozenset({".json", ".yaml", ".yml", ".toml", ".ini", ".env"})
_CONFIG_BASENAMES = frozenset({"Dockerfile"})
_STYLESHEET_EXTENSIONS = frozenset({".css", ".scss", ".sass", ".less"})
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
    h = _low_coverage_stylesheet_gap(
        changed_files=changed_files,
        low_coverage=low_coverage,
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
    return hypotheses


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
        confidence="medium",
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
        confidence="medium",
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
        confidence="medium",
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
