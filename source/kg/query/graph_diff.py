from __future__ import annotations

from dataclasses import dataclass, field

from source.kg.core.models import JsonObject
from source.kg.query.snapshot import KgSnapshot


# Natural fact key: (predicate, subject_id, object_id, canonical qualifier)
# fact_id is stable (models.py:140 — hash of predicate + subject_id + object_id + qualifier),
# but we key by natural tuple so the contract is explicit and immune to any future ID-scheme change.
def _fact_natural_key(fact: JsonObject) -> tuple[str, str, str, str]:
    import json

    qualifier = fact.get("qualifier") or {}
    canonical_q = json.dumps(qualifier, sort_keys=True, separators=(",", ":"))
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
    """

    added_entities: dict[str, list[JsonObject]] = field(default_factory=dict)
    removed_entities: dict[str, list[JsonObject]] = field(default_factory=dict)
    added_facts: list[JsonObject] = field(default_factory=list)
    removed_facts: list[JsonObject] = field(default_factory=list)

    def summary(self) -> JsonObject:
        return {
            "added_entities": sum(len(v) for v in self.added_entities.values()),
            "removed_entities": sum(len(v) for v in self.removed_entities.values()),
            "added_facts": len(self.added_facts),
            "removed_facts": len(self.removed_facts),
        }


def diff_snapshots(base: KgSnapshot, head: KgSnapshot) -> GraphDelta:
    """Set-diff base vs head by natural identity.

    Entity identity key: URN (commit-independent; models.py:37-64).
    Fact identity key: (predicate, subject_id, object_id, canonical_qualifier).

    Evidence rows and timestamps are excluded — they differ across builds by construction.

    Raises ValueError if base and head carry different tenant_ids.
    """
    base_tenant = base.manifest.get("tenant_id")
    head_tenant = head.manifest.get("tenant_id")
    if base_tenant and head_tenant and base_tenant != head_tenant:
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

    return GraphDelta(
        added_entities=added_entities,
        removed_entities=removed_entities,
        added_facts=added_facts,
        removed_facts=removed_facts,
    )
