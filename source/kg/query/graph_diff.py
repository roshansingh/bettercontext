from __future__ import annotations

from dataclasses import dataclass, field

from source.kg.core.models import JsonObject, canonical_json
from source.kg.query.snapshot import KgSnapshot

# ---------------------------------------------------------------------------
# Test-file classification
# Minimal shared predicate mirroring source/kg/product/review_hypotheses.py
# _is_test_file. Duplication noted; consolidation is a future TODO (BACKLOG).
# ---------------------------------------------------------------------------
_TEST_PATH_SEGMENTS: frozenset[str] = frozenset({"test", "tests", "spec", "specs", "__tests__"})


def _is_test_file(path: str) -> bool:
    """Return True if *path* looks like a test/spec file."""
    parts = path.replace("\\", "/").split("/")
    for part in parts[:-1]:
        if part in _TEST_PATH_SEGMENTS:
            return True
    stem = parts[-1]
    base = stem.rsplit(".", 1)[0] if "." in stem else stem
    if base.startswith("test_") or base.endswith("_test") or base.startswith("spec_") or base.endswith("_spec"):
        return True
    if ".test." in stem or ".spec." in stem:
        return True
    return False


def _entity_coordinates(entity: JsonObject, snap: KgSnapshot) -> JsonObject:
    """Return best-available source coordinates for *entity* as a flat dict.

    Priority:
      1. entity properties (path/line/end_line) — always present for extractor-built symbols.
      2. first evidence row's bytes_ref for that entity.

    Returns {} when no coordinates are available (unit-test synthetic entities
    built without properties or evidence).
    """
    properties = entity.get("properties") or {}
    if properties.get("path"):
        coords: JsonObject = {"path": properties["path"]}
        if properties.get("line") is not None:
            coords["line"] = properties["line"]
        if properties.get("end_line") is not None:
            coords["end_line"] = properties["end_line"]
        return coords
    # Fallback: first evidence bytes_ref
    for ev in snap.evidence_by_target.get(entity.get("entity_id", ""), []):
        br = ev.get("bytes_ref") or {}
        if br.get("path"):
            coords = {"path": br["path"]}
            if br.get("line_start") is not None:
                coords["line"] = br["line_start"]
            if br.get("line_end") is not None:
                coords["end_line"] = br["line_end"]
            return coords
    return {}


# Natural fact key: (predicate, subject_id, object_id, canonical qualifier)
# fact_id is stable (models.py:140 — hash of predicate + subject_id + object_id + qualifier),
# but we key by natural tuple so the contract is explicit and immune to any future ID-scheme change.
def _fact_natural_key(fact: JsonObject) -> tuple[str, str, str, str]:
    qualifier = fact.get("qualifier") or {}
    canonical_q = canonical_json(qualifier)
    return (
        str(fact.get("predicate", "")),
        str(fact.get("subject_id", "")),
        str(fact.get("object_id", "")),
        canonical_q,
    )


@dataclass(frozen=True)
class GraphDelta:
    """Structural diff between two KG snapshots.

    added_entities / removed_entities: dict[kind, list[entity_record]], sorted by URN.
    added_facts / removed_facts: list[fact_record], sorted by (predicate, subject_id, object_id).
    uninstrumented_scopes: deduplicated, sorted list of scope_ref dicts from coverage rows
        whose state == 'uninstrumented' in either base or head snapshot.

    Attribute-visibility note: this diff operates at identity level.  Attribute-only
    changes — body edits without qualname change, line-number shifts, property drift — are
    INVISIBLE as entity changes; body edits surface only indirectly through added/removed
    fact rows (e.g. CALLS edges added or dropped as a result).  Callers should not interpret
    an empty entity delta as "nothing changed" without also checking the fact delta.

    Uninstrumented-scope note: an empty entity/fact delta combined with non-empty
    uninstrumented_scopes must be read as "no change detected WITHIN INSTRUMENTED SCOPE" —
    changes inside uninstrumented scopes are not visible to this diff.
    """

    added_entities: dict[str, list[JsonObject]] = field(default_factory=dict)
    removed_entities: dict[str, list[JsonObject]] = field(default_factory=dict)
    added_facts: list[JsonObject] = field(default_factory=list)
    removed_facts: list[JsonObject] = field(default_factory=list)
    uninstrumented_scopes: list[JsonObject] = field(default_factory=list)

    def summary(self) -> JsonObject:
        """Return counts of structural changes and uninstrumented scopes.

        When added_entities, removed_entities, added_facts, and removed_facts are all
        zero but uninstrumented_scopes is non-empty, read the result as "no change
        detected WITHIN INSTRUMENTED SCOPE" — the delta is silent about uninstrumented
        languages, paths, or repos.
        """
        return {
            "added_entities": sum(len(v) for v in self.added_entities.values()),
            "removed_entities": sum(len(v) for v in self.removed_entities.values()),
            "added_facts": len(self.added_facts),
            "removed_facts": len(self.removed_facts),
            "uninstrumented_scopes": self.uninstrumented_scopes,
        }


def diff_snapshots(base: KgSnapshot, head: KgSnapshot) -> GraphDelta:
    """Set-diff base vs head by natural identity.

    Entity identity key: URN (commit-independent; models.py:37-64).
    Fact identity key: (predicate, subject_id, object_id, canonical_qualifier).

    Evidence rows and timestamps are excluded — they differ across builds by construction.

    Attribute-visibility: identity-level diff only.  Attribute-only changes (body edits
    without qualname change, line-number shifts, property drift) are invisible as entity
    changes; body edits surface only indirectly via added/removed fact rows (e.g. CALLS).

    Uninstrumented scopes: coverage rows with state == 'uninstrumented' from base and head
    are unioned, deduplicated by scope_ref, and surfaced in GraphDelta.uninstrumented_scopes.
    An empty entity/fact delta combined with non-empty uninstrumented_scopes means "no
    change detected WITHIN INSTRUMENTED SCOPE" — not "no change."

    Tenant handling: raises ValueError if base and head carry different tenant_ids.
    When both manifests omit tenant_id (None == None), diff proceeds without error.
    """
    base_tenant = base.manifest.get("tenant_id")
    head_tenant = head.manifest.get("tenant_id")
    if base_tenant != head_tenant:
        raise ValueError(
            f"diff_snapshots: tenant mismatch — base has tenant_id={base_tenant!r}, "
            f"head has tenant_id={head_tenant!r}. Both snapshots must belong to the same tenant."
        )

    # --- entities ---
    base_by_urn: dict[str, JsonObject] = {e["urn"]: e for e in base.entities}
    head_by_urn: dict[str, JsonObject] = {e["urn"]: e for e in head.entities}

    base_urns = set(base_by_urn)
    head_urns = set(head_by_urn)

    added_urns = sorted(head_urns - base_urns)
    removed_urns = sorted(base_urns - head_urns)

    added_entities: dict[str, list[JsonObject]] = {}
    for urn in added_urns:
        record = head_by_urn[urn]
        kind = str(record.get("kind", ""))
        added_entities.setdefault(kind, []).append(record)

    removed_entities: dict[str, list[JsonObject]] = {}
    for urn in removed_urns:
        record = base_by_urn[urn]
        kind = str(record.get("kind", ""))
        removed_entities.setdefault(kind, []).append(record)

    # --- facts ---
    base_facts_by_key: dict[tuple[str, str, str, str], JsonObject] = {
        _fact_natural_key(f): f for f in base.facts
    }
    head_facts_by_key: dict[tuple[str, str, str, str], JsonObject] = {
        _fact_natural_key(f): f for f in head.facts
    }

    base_keys = set(base_facts_by_key)
    head_keys = set(head_facts_by_key)

    added_facts = sorted(
        (head_facts_by_key[k] for k in head_keys - base_keys),
        key=lambda f: (str(f.get("predicate", "")), str(f.get("subject_id", "")), str(f.get("object_id", ""))),
    )
    removed_facts = sorted(
        (base_facts_by_key[k] for k in base_keys - head_keys),
        key=lambda f: (str(f.get("predicate", "")), str(f.get("subject_id", "")), str(f.get("object_id", ""))),
    )

    # --- uninstrumented scopes ---
    # Union coverage rows with state == 'uninstrumented' from both snapshots.
    # Deduplicate by canonical_json(scope_ref) so identical scopes from base and head
    # are not double-counted.  Sort for determinism.
    seen_scope_keys: set[str] = set()
    uninstrumented_scopes: list[JsonObject] = []
    for row in list(base.coverage) + list(head.coverage):
        if row.get("state") != "uninstrumented":
            continue
        scope_ref = row.get("scope_ref") or {}
        key = canonical_json(scope_ref)
        if key in seen_scope_keys:
            continue
        seen_scope_keys.add(key)
        uninstrumented_scopes.append(scope_ref)
    uninstrumented_scopes.sort(key=canonical_json)

    return GraphDelta(
        added_entities=added_entities,
        removed_entities=removed_entities,
        added_facts=added_facts,
        removed_facts=removed_facts,
        uninstrumented_scopes=uninstrumented_scopes,
    )


# ---------------------------------------------------------------------------
# Review-relevant delta queries
# ---------------------------------------------------------------------------

def removed_symbols_with_surviving_referrers(
    delta: GraphDelta,
    base: KgSnapshot,
    head: KgSnapshot,
) -> list[JsonObject]:
    """Symbols absent in head that still have live referrers in head.

    A *referrer* is the subject of a CALLS or IMPORTS fact in *base* whose
    object is one of the removed symbols. "Surviving" means the referrer
    entity (by URN) also exists in *head*.

    Output rows (sorted by removed symbol URN, then referrer URN):
      removed_symbol: {urn, entity_id, kind, qualname, repo, module, coordinates}
      surviving_referrers: list of {urn, entity_id, kind, qualname, repo, module, coordinates}

    Coordinates come from entity properties (path/line) first, then evidence
    bytes_ref. Unit-test synthetic entities built without properties return
    coordinates: {} — the caller should treat absence as "not available" rather
    than as missing evidence.
    """
    # Build index of removed entity_ids (base-side).
    removed_by_id: dict[str, JsonObject] = {}
    for entities in delta.removed_entities.values():
        for e in entities:
            removed_by_id[e["entity_id"]] = e

    if not removed_by_id:
        return []

    # URNs present in head — used to check referrer survival.
    head_urns: set[str] = {e["urn"] for e in head.entities}

    # For each base CALLS/IMPORTS fact pointing at a removed entity,
    # check whether the subject (referrer) still exists in head.
    referrers_by_removed: dict[str, set[str]] = {}  # removed entity_id → set of surviving referrer entity_ids
    for fact in base.facts:
        if fact.get("predicate") not in {"CALLS", "IMPORTS"}:
            continue
        obj_id = fact["object_id"]
        if obj_id not in removed_by_id:
            continue
        referrer = base.entities_by_id.get(fact["subject_id"])
        if referrer is None:
            continue
        # Referrer survives if its URN is present in head.
        if referrer["urn"] not in head_urns:
            continue
        referrers_by_removed.setdefault(obj_id, set()).add(referrer["entity_id"])

    if not referrers_by_removed:
        return []

    def _symbol_identity(entity: JsonObject, snap: KgSnapshot) -> JsonObject:
        identity = entity.get("identity") or {}
        return {
            "urn": entity["urn"],
            "entity_id": entity["entity_id"],
            "kind": entity["kind"],
            "qualname": identity.get("qualname"),
            "repo": identity.get("repo"),
            "module": identity.get("module"),
            "coordinates": _entity_coordinates(entity, snap),
        }

    rows: list[JsonObject] = []
    for removed_id in sorted(referrers_by_removed):
        removed_entity = removed_by_id[removed_id]
        surviving_referrer_ids = sorted(referrers_by_removed[removed_id])
        referrers = [
            _symbol_identity(base.entities_by_id[rid], base)
            for rid in surviving_referrer_ids
            if rid in base.entities_by_id
        ]
        rows.append({
            "removed_symbol": _symbol_identity(removed_entity, base),
            "surviving_referrers": referrers,
        })

    rows.sort(key=lambda r: r["removed_symbol"]["urn"])
    return rows


def call_edge_delta_for_paths(
    delta: GraphDelta,
    base: KgSnapshot,
    head: KgSnapshot,
    paths: list[str],
) -> list[JsonObject]:
    """Added/removed CALLS facts whose subject or object entity's path matches
    one of *paths*.

    Path matching rule: the entity's properties["path"] must equal one of the
    given paths after normalizing both sides — backslashes become "/", whitespace
    is stripped, and any leading "./" prefixes are removed as literal prefixes
    ("../" is NOT stripped; "./x" and "x" match, "../x" and "x" do not).
    Entities without a path property are excluded.

    Output rows (sorted by (change_kind, subject_id, object_id)):
      change_kind: "added" | "removed"
      predicate: always "CALLS"
      subject_id / object_id: entity IDs from the respective snapshot
      subject_path / object_path: path from entity properties (None when absent)
    """
    if not paths:
        return []

    def _norm(p: str) -> str:
        p = p.replace("\\", "/").strip()
        while p.startswith("./"):
            p = p[2:]
        return p

    normalized_paths: set[str] = {_norm(p) for p in paths}

    def _entity_path(entity_id: str, snap: KgSnapshot) -> str | None:
        entity = snap.entities_by_id.get(entity_id)
        if entity is None:
            return None
        return entity.get("properties", {}).get("path") or None

    def _path_matches(path: str | None) -> bool:
        if path is None:
            return False
        return _norm(path) in normalized_paths

    rows: list[JsonObject] = []

    for fact in delta.added_facts:
        if fact.get("predicate") != "CALLS":
            continue
        subj_path = _entity_path(fact["subject_id"], head)
        obj_path = _entity_path(fact["object_id"], head)
        if _path_matches(subj_path) or _path_matches(obj_path):
            rows.append({
                "change_kind": "added",
                "predicate": fact["predicate"],
                "subject_id": fact["subject_id"],
                "object_id": fact["object_id"],
                "subject_path": subj_path,
                "object_path": obj_path,
            })

    for fact in delta.removed_facts:
        if fact.get("predicate") != "CALLS":
            continue
        subj_path = _entity_path(fact["subject_id"], base)
        obj_path = _entity_path(fact["object_id"], base)
        if _path_matches(subj_path) or _path_matches(obj_path):
            rows.append({
                "change_kind": "removed",
                "predicate": fact["predicate"],
                "subject_id": fact["subject_id"],
                "object_id": fact["object_id"],
                "subject_path": subj_path,
                "object_path": obj_path,
            })

    rows.sort(key=lambda r: (r["change_kind"], str(r.get("subject_id", "")), str(r.get("object_id", ""))))
    return rows


def removed_test_references(
    delta: GraphDelta,
    base: KgSnapshot,
    head: KgSnapshot,
) -> list[JsonObject]:
    """Removed facts whose SUBJECT entity's path is test-classified and whose
    OBJECT entity still exists in head (by URN).

    "Test-classified" means the subject entity's properties["path"] passes
    ``_is_test_file``. Entities without a path property are excluded.

    Output rows (sorted by (subject_path, predicate, object URN)):
      predicate: the removed fact's predicate
      subject: {entity_id, urn, kind, path, qualname, repo, module}
      object: {entity_id, urn, kind, qualname, repo, module}  (head-side entity looked up by URN)
    """
    head_by_urn: dict[str, JsonObject] = {e["urn"]: e for e in head.entities}

    rows: list[JsonObject] = []
    for fact in delta.removed_facts:
        subject = base.entities_by_id.get(fact["subject_id"])
        if subject is None:
            continue
        subj_path = (subject.get("properties") or {}).get("path") or ""
        if not subj_path or not _is_test_file(subj_path):
            continue
        # Object must survive in head (by URN).
        obj_base = base.entities_by_id.get(fact["object_id"])
        if obj_base is None:
            continue
        obj_head = head_by_urn.get(obj_base["urn"])
        if obj_head is None:
            continue
        subj_identity = subject.get("identity") or {}
        obj_identity = obj_base.get("identity") or {}
        rows.append({
            "predicate": fact["predicate"],
            "subject": {
                "entity_id": subject["entity_id"],
                "urn": subject["urn"],
                "kind": subject["kind"],
                "path": subj_path,
                "qualname": subj_identity.get("qualname"),
                "repo": subj_identity.get("repo"),
                "module": subj_identity.get("module"),
            },
            "object": {
                "entity_id": obj_base["entity_id"],
                "urn": obj_base["urn"],
                "kind": obj_base["kind"],
                "qualname": obj_identity.get("qualname"),
                "repo": obj_identity.get("repo"),
                "module": obj_identity.get("module"),
            },
        })

    rows.sort(key=lambda r: (
        str(r["subject"].get("path") or ""),
        str(r.get("predicate") or ""),
        str(r["object"].get("urn") or ""),
    ))
    return rows
