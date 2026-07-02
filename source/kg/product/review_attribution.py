from __future__ import annotations

import hashlib

from source.kg.core.models import JsonObject, canonical_json


LEAD_LIST_KINDS = {
    "changed_symbols": "changed_symbol",
    "direct_callers": "direct_caller",
    "direct_callees": "direct_callee",
    "transitive_callers": "transitive_caller",
    "source_coordinates": "source_coordinate",
}

# Mapping from lead list field name to its singular _count key in available/returned dicts.
LEAD_COUNT_KEYS = {
    "changed_symbols": "changed_symbol_count",
    "direct_callers": "direct_caller_count",
    "direct_callees": "direct_callee_count",
    "transitive_callers": "transitive_caller_count",
    "source_coordinates": "source_coordinate_count",
}


def hypothesis_stable_id(
    risk_type: str,
    supporting_lead_ids: list[str],
    evidence_refs: list[JsonObject],
) -> str:
    """Build an evidence-specific hypothesis_id from risk_type, lead IDs, and evidence coords.

    Hashes risk_type + sorted supporting_lead_ids + sorted (repo,path,line_start,line_end)
    tuples from evidence_refs so every distinct evidence set gets a distinct ID, while the
    same inputs always produce the same ID regardless of call order.
    """
    coord_tuples = sorted(
        (
            ref.get("repo") or "",
            ref.get("path") or "",
            ref.get("line_start"),
            ref.get("line_end"),
        )
        for ref in evidence_refs
        if isinstance(ref, dict)
    )
    payload: JsonObject = {
        "risk_type": risk_type,
        "supporting_lead_ids": sorted(supporting_lead_ids),
        "evidence_coords": [list(t) for t in coord_tuples],
    }
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()[:16]
    return f"hypothesis:{risk_type}:{digest}"


def review_stable_id(prefix: str, row: JsonObject, *, fallback_kind: str) -> str:
    existing = row.get(f"{prefix}_id")
    if isinstance(existing, str) and existing.strip():
        return existing.strip()
    for key in ("fact_id", "symbol_id", "entity_id"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return f"{prefix}:{fallback_kind}:{value.strip()}"
    payload = {
        key: row.get(key)
        for key in (
            "lead_kind",
            "risk_type",
            "predicate",
            "subject",
            "object",
            "repo",
            "path",
            "line_start",
            "line_end",
            "qualified_name",
            "qualname",
            "name",
        )
        if row.get(key) not in (None, "", [], {})
    }
    if not payload:
        stable_projection = {
            key: row.get(key)
            for key in ("lead_kind", "risk_type", "predicate", "subject", "object", "repo", "path")
            if row.get(key) not in (None, "", [], {})
        }
        payload = {"fallback_kind": fallback_kind, "projection": stable_projection}
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}:{fallback_kind}:{digest}"


def add_review_lead_ids(leads: JsonObject) -> JsonObject:
    """Stamp each row in a review_leads dict with a stable lead_id and lead_kind."""
    result: JsonObject = {}
    for field, value in leads.items():
        kind = LEAD_LIST_KINDS.get(field)
        if kind is None or not isinstance(value, list):
            result[field] = value
            continue
        stamped = []
        for row in value:
            if not isinstance(row, dict):
                stamped.append(row)
                continue
            row_copy = dict(row)
            row_copy["lead_kind"] = kind
            row_copy["lead_id"] = review_stable_id("lead", row_copy, fallback_kind=kind)
            stamped.append(row_copy)
        result[field] = stamped
    return result


def review_lead_counts(leads: JsonObject) -> JsonObject:
    """Count rows actually present in a review_leads dict (returned counts)."""
    counts: JsonObject = {}
    for field in LEAD_LIST_KINDS:
        value = leads.get(field)
        counts[LEAD_COUNT_KEYS[field]] = len(value) if isinstance(value, list) else 0
    return counts


def review_available_counts(
    *,
    changed_symbols: list[JsonObject],
    direct_callers: list[JsonObject],
    direct_callees: list[JsonObject],
    transitive_callers: list[JsonObject],
    source_coordinates: list[JsonObject],
) -> JsonObject:
    """Capture unbounded available counts before any section limit is applied."""
    return {
        "changed_symbol_count": len(changed_symbols),
        "direct_caller_count": len(direct_callers),
        "direct_callee_count": len(direct_callees),
        "transitive_caller_count": len(transitive_callers),
        "source_coordinate_count": len(source_coordinates),
    }
