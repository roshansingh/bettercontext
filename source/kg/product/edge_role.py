"""Edge role classification and review-value ranking for review_context relation rows.

Roles are derived purely from existing fact families and entity kind lookups in the
KgSnapshot. No keyword vocabularies.

Role derivation per role (in priority order for ranking):

  test_assertion:       other endpoint's path is test-classified via _is_test_file
                        (same predicate used in review_hypotheses.py:520).

  persistence:          other endpoint participates as subject in HANDLES_MODEL,
                        TASK_USES_MODEL, or SERIALIZES_MODEL facts (Python django/celery
                        support_facts; TS has no ORM extractor, so this role is
                        structurally absent for TS repos).

  external_side_effect: other endpoint participates as subject in CALLS_ENDPOINT facts
                        (Python http_client_endpoints.py and TS http_client.py) OR as
                        subject/object in PRODUCES_EVENT or CONSUMES_EVENT facts
                        (message-broker transport, serverless.yaml, terraform).

  intra_repo_consumer:  default for code edges whose other endpoint is not
                        classified by any rule above.

  generic_utility:      other endpoint has entity kind ExternalPackage or
                        ExternalSymbol (see _EXTERNAL_ENTITY_KINDS).

Ranking priority (highest first): test_assertion, persistence, external_side_effect,
intra_repo_consumer, generic_utility. generic_utility is last so budget eviction removes
utility edges before semantic ones.
"""
from __future__ import annotations

from source.kg.core.models import JsonObject

# Test-file classification: mirrors source/kg/product/review_hypotheses.py:520.
# Duplication is intentional; consolidation deferred per BACKLOG pattern.
_TEST_PATH_SEGMENTS: frozenset[str] = frozenset({"test", "tests", "spec", "specs", "__tests__"})


def _is_test_file(path: str) -> bool:
    parts = path.replace("\\", "/").split("/")
    for part in parts[:-1]:
        if part in _TEST_PATH_SEGMENTS:
            return True
    stem = parts[-1]
    base = stem.rsplit(".", 1)[0] if "." in stem else stem
    if base.startswith("test_") or base.endswith("_test") or base.startswith("spec_") or base.endswith("_spec"):
        return True
    return ".test." in stem or ".spec." in stem


# Fact predicates that indicate the endpoint touches persistent storage (django/celery).
# These live in support_facts (produces_support_predicates in Python adapter).
_PERSISTENCE_PREDICATES: frozenset[str] = frozenset(
    {"HANDLES_MODEL", "TASK_USES_MODEL", "SERIALIZES_MODEL"}
)

# Fact predicates that indicate the endpoint makes an external call or transport event.
# CALLS_ENDPOINT: Python http_client_endpoints + TS http_client + shared/endpoints.
# PRODUCES_EVENT / CONSUMES_EVENT: message-broker and serverless config facts.
_EXTERNAL_SIDE_EFFECT_PREDICATES: frozenset[str] = frozenset(
    {"CALLS_ENDPOINT", "PRODUCES_EVENT", "CONSUMES_EVENT"}
)

# Entity kinds that indicate a third-party / out-of-repo dependency.
_EXTERNAL_ENTITY_KINDS: frozenset[str] = frozenset({"ExternalPackage", "ExternalSymbol"})

# Ranking priority: lower number = higher priority (kept first under budget pressure).
_ROLE_PRIORITY: dict[str, int] = {
    "test_assertion": 0,
    "persistence": 1,
    "external_side_effect": 2,
    "intra_repo_consumer": 3,
    "generic_utility": 4,
}


class EdgeRoleIndex:
    """Pre-built index for O(1) edge role lookups per entity_id.

    Built once per review_context call from KgSnapshot.facts + support_facts +
    entities_by_id. Passed to classify_edge_role to avoid re-scanning all facts
    per row.
    """

    def __init__(
        self,
        persistence_subject_ids: frozenset[str],
        external_side_effect_subject_ids: frozenset[str],
        external_entity_ids: frozenset[str],
        entities_by_id: dict[str, JsonObject],
        facts_by_id: dict[str, JsonObject],
    ) -> None:
        self.persistence_subject_ids = persistence_subject_ids
        self.external_side_effect_subject_ids = external_side_effect_subject_ids
        self.external_entity_ids = external_entity_ids
        self.entities_by_id = entities_by_id
        self.facts_by_id = facts_by_id


def build_edge_role_index(kg) -> EdgeRoleIndex:
    """Build EdgeRoleIndex from a KgSnapshot.

    Scans kg.facts and kg.support_facts once; O(F) where F = total facts.
    The kg argument is typed as Any to avoid a circular import with
    source.kg.query.snapshot (which already imports from this package).
    """
    persistence_subject_ids: set[str] = set()
    external_side_effect_subject_ids: set[str] = set()

    all_facts = list(kg.facts) + list(kg.support_facts)
    for fact in all_facts:
        predicate = fact.get("predicate", "")
        subject_id = str(fact.get("subject_id", ""))
        if predicate in _PERSISTENCE_PREDICATES and subject_id:
            persistence_subject_ids.add(subject_id)
        if predicate in _EXTERNAL_SIDE_EFFECT_PREDICATES and subject_id:
            external_side_effect_subject_ids.add(subject_id)
        # PRODUCES_EVENT / CONSUMES_EVENT can reference channels as objects too;
        # the subject (the service/symbol emitting/consuming) is the semantic actor.

    external_entity_ids: set[str] = set()
    for entity in kg.entities:
        if entity.get("kind") in _EXTERNAL_ENTITY_KINDS:
            external_entity_ids.add(str(entity.get("entity_id", "")))

    facts_by_id: dict[str, JsonObject] = {
        str(f.get("fact_id", "")): f for f in all_facts if f.get("fact_id")
    }

    return EdgeRoleIndex(
        persistence_subject_ids=frozenset(persistence_subject_ids),
        external_side_effect_subject_ids=frozenset(external_side_effect_subject_ids),
        external_entity_ids=frozenset(external_entity_ids),
        entities_by_id=kg.entities_by_id,
        facts_by_id=facts_by_id,
    )


def classify_edge_role(row: JsonObject, index: EdgeRoleIndex, *, caller_perspective: bool) -> str:
    """Classify a relation row as one of the five edge roles.

    caller_perspective=True → row is a direct_caller row (subject called the changed
    symbol); the "other" endpoint is the subject.
    caller_perspective=False → row is a direct_callee/transitive row (changed symbol calls
    the object); the "other" endpoint is the object.

    Derivation is purely structural (facts + entity kinds, no keyword matching).
    """
    fact_id = str(row.get("fact_id", ""))
    fact = index.facts_by_id.get(fact_id)

    # Determine entity_id of the "other" endpoint.
    other_entity_id: str | None = None
    if fact is not None:
        if caller_perspective:
            other_entity_id = str(fact.get("subject_id", "")) or None
        else:
            other_entity_id = str(fact.get("object_id", "")) or None

    # --- test_assertion ---
    # Check path of the other endpoint's entity.
    if other_entity_id:
        entity = index.entities_by_id.get(other_entity_id)
        if entity is not None:
            path = entity.get("properties", {}).get("path", "")
            if path and _is_test_file(path):
                return "test_assertion"

    # Fall back to path in the row itself (callee_symbol / caller_symbol).
    _row_path = _row_endpoint_path(row, caller_perspective=caller_perspective)
    if _row_path and _is_test_file(_row_path):
        return "test_assertion"

    # --- generic_utility (ExternalPackage/ExternalSymbol) ---
    if other_entity_id and other_entity_id in index.external_entity_ids:
        return "generic_utility"

    # --- persistence ---
    if other_entity_id and other_entity_id in index.persistence_subject_ids:
        return "persistence"

    # --- external_side_effect ---
    if other_entity_id and other_entity_id in index.external_side_effect_subject_ids:
        return "external_side_effect"

    return "intra_repo_consumer"


def _row_endpoint_path(row: JsonObject, *, caller_perspective: bool) -> str:
    """Extract the path from the row's 'other' endpoint dict when available."""
    key = "caller_symbol" if caller_perspective else "callee_symbol"
    endpoint = row.get(key)
    if isinstance(endpoint, dict):
        return str(endpoint.get("path", ""))
    # Some rows use subject/object as dicts.
    side = "subject" if caller_perspective else "object"
    endpoint = row.get(side)
    if isinstance(endpoint, dict):
        return str(endpoint.get("path", ""))
    return ""


def annotate_edge_roles(
    rows: list[JsonObject],
    index: EdgeRoleIndex,
    *,
    caller_perspective: bool,
) -> list[JsonObject]:
    """Attach edge_role to each row (in-place mutation; also returns the list)."""
    for row in rows:
        row["edge_role"] = classify_edge_role(row, index, caller_perspective=caller_perspective)
    return rows


def rank_by_review_value(rows: list[JsonObject]) -> list[JsonObject]:
    """Sort rows by review-value priority, generic_utility last.

    Rows without an edge_role are treated as intra_repo_consumer (priority 3).
    Stable sort: ties preserve original order.
    """
    return sorted(
        rows,
        key=lambda r: _ROLE_PRIORITY.get(r.get("edge_role", "intra_repo_consumer"), 3),
    )
