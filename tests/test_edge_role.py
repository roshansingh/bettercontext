"""Tests for edge_role classification and review-value ranking (Phase B3).

All role classification is via EdgeRoleIndex lookups (fact families + entity kinds).
Real-pipeline coverage: build_kg extracts ExternalPackage entities + CALLS facts pointing
to them, verifying the generic_utility role end-to-end through the live extractor.

Tests:
1. Real KG (build_kg): CALLS to ExternalPackage → generic_utility role correct.
2. Synthetic index: test_assertion role from test-file entity path.
3. Synthetic index: persistence role from HANDLES_MODEL/TASK_USES_MODEL support_fact subject.
4. Synthetic index: external_side_effect role from CALLS_ENDPOINT subject.
5. Synthetic index: intra_repo_consumer default when no matching index entry.
6. Negative: no persistence facts → no persistence role fabricated.
7. rank_by_review_value: generic_utility ranked last.
8. Budget eviction: after rank + compaction (limit=2), utility evicted, consumer survives.
9. annotate_edge_roles: in-place mutation, correct return.
10. edge_role preserved through _compact_relation_rows.

Role derivation fact families (cited from extractor adapters):
  generic_utility  — entity kind ExternalPackage (python/ast_extractor.py:816,
                     typescript/compiler_api_extractor.py:236, dotnet/csharp_extractor.py:122)
  persistence      — support_fact predicates HANDLES_MODEL, TASK_USES_MODEL, SERIALIZES_MODEL
                     (django_framework.py:169,194,216; extractor_adapter.py:44-49)
  external_side_effect — fact predicates CALLS_ENDPOINT (http_client_endpoints.py:551,
                     typescript/http_client.py:68), PRODUCES_EVENT/CONSUMES_EVENT
                     (message_events.py:23; serverless_yaml.py:90)
  test_assertion   — entity path classified by _is_test_file (review_hypotheses.py:520;
                     edge_role.py mirrors the same predicate)
  intra_repo_consumer — default (no matching index entry)
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from source.kg.build.pipeline import build_kg
from source.kg.product.edge_role import (
    EdgeRoleIndex,
    annotate_edge_roles,
    build_edge_role_index,
    classify_edge_role,
    rank_by_review_value,
)
from source.kg.product.output_budget import _compact_relation_rows
from source.kg.query.snapshot import KgSnapshot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_index(
    *,
    persistence_ids: frozenset[str] = frozenset(),
    side_effect_ids: frozenset[str] = frozenset(),
    external_entity_ids: frozenset[str] = frozenset(),
    entities_by_id: dict | None = None,
    facts_by_id: dict | None = None,
) -> EdgeRoleIndex:
    return EdgeRoleIndex(
        persistence_subject_ids=persistence_ids,
        external_side_effect_subject_ids=side_effect_ids,
        external_entity_ids=external_entity_ids,
        entities_by_id=entities_by_id or {},
        facts_by_id=facts_by_id or {},
    )


def _make_entity(entity_id: str, kind: str, path: str = "") -> dict:
    return {
        "entity_id": entity_id,
        "kind": kind,
        "identity": {},
        "properties": {"path": path},
    }


def _make_fact(fact_id: str, predicate: str, subject_id: str, object_id: str) -> dict:
    return {
        "fact_id": fact_id,
        "predicate": predicate,
        "subject_id": subject_id,
        "object_id": object_id,
    }


def _make_row(fact_id: str, subject: str = "a", object_: str = "b") -> dict:
    return {"fact_id": fact_id, "predicate": "CALLS", "subject": subject, "object": object_}


# ---------------------------------------------------------------------------
# Real-pipeline: ExternalPackage via build_kg
# ---------------------------------------------------------------------------

_PY_MAIN = """\
import requests

def process(data):
    resp = requests.get("http://example.com")
    return resp
"""

_SETUP_CFG = """\
[metadata]
name = testpkg
version = 0.1
"""


class TestGenericUtilityRealPipeline(unittest.TestCase):
    """build_kg extracts CALLS→ExternalPackage; classify_edge_role returns generic_utility."""

    @classmethod
    def setUpClass(cls):
        cls._tmproot = tempfile.mkdtemp()
        root = Path(cls._tmproot)
        pkg = root / "pkg"
        pkg.mkdir()
        (pkg / "setup.cfg").write_text(_SETUP_CFG, encoding="utf-8")
        (pkg / "mypkg").mkdir()
        (pkg / "mypkg/__init__.py").write_text("", encoding="utf-8")
        (pkg / "mypkg/main.py").write_text(_PY_MAIN, encoding="utf-8")
        out = root / "kg"
        build_kg(pkg, out)
        cls.kg = KgSnapshot(out)
        cls.index = build_edge_role_index(cls.kg)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._tmproot, ignore_errors=True)

    def test_external_package_entities_extracted(self):
        external = [e for e in self.kg.entities if e.get("kind") == "ExternalPackage"]
        self.assertGreater(len(external), 0, "build_kg produced no ExternalPackage entities")

    def test_calls_to_external_package_extracted(self):
        # CALLS: CodeSymbol → ExternalPackage (e.g. process → requests)
        external_ids = {str(e["entity_id"]) for e in self.kg.entities if e.get("kind") == "ExternalPackage"}
        calls_to_external = [
            f for f in self.kg.facts
            if f.get("predicate") == "CALLS" and str(f.get("object_id", "")) in external_ids
        ]
        self.assertGreater(len(calls_to_external), 0, "No CALLS→ExternalPackage facts extracted")

    def test_generic_utility_role_for_external_callee(self):
        external_ids = {str(e["entity_id"]) for e in self.kg.entities if e.get("kind") == "ExternalPackage"}
        calls_to_external = [
            f for f in self.kg.facts
            if f.get("predicate") == "CALLS" and str(f.get("object_id", "")) in external_ids
        ]
        self.assertTrue(calls_to_external)
        fact = calls_to_external[0]
        row = _make_row(fact["fact_id"])
        role = classify_edge_role(row, self.index, caller_perspective=False)
        self.assertEqual(
            role, "generic_utility",
            f"Expected generic_utility for ExternalPackage callee, got {role!r}"
        )

    def test_build_edge_role_index_populates_external_ids(self):
        self.assertGreater(len(self.index.external_entity_ids), 0)

    def test_inversion_external_entity_not_in_persistence_ids(self):
        # An ExternalPackage should NOT appear as persistence subject (no overlap).
        external_ids = self.index.external_entity_ids
        overlap = external_ids & self.index.persistence_subject_ids
        self.assertEqual(len(overlap), 0, f"ExternalPackage IDs in persistence index: {overlap}")


# ---------------------------------------------------------------------------
# Synthetic index: test_assertion from entity path
# ---------------------------------------------------------------------------

class TestTestAssertionRole(unittest.TestCase):
    """Entity path is test-classified → test_assertion role."""

    def _index_with_test_entity(self, entity_id: str, path: str) -> EdgeRoleIndex:
        entity = _make_entity(entity_id, "CodeSymbol", path)
        fact = _make_fact("f1", "CALLS", entity_id, "other")
        return _make_index(
            entities_by_id={entity_id: entity},
            facts_by_id={"f1": fact},
        )

    def test_test_prefix_path_gives_test_assertion_as_caller(self):
        index = self._index_with_test_entity("e1", "tests/test_service.py")
        row = _make_row("f1")
        role = classify_edge_role(row, index, caller_perspective=True)
        self.assertEqual(role, "test_assertion")

    def test_test_dir_segment_gives_test_assertion_as_callee(self):
        # Callee is in a test file → test_assertion
        entity = _make_entity("callee1", "CodeSymbol", "src/__tests__/serviceTest.ts")
        fact = _make_fact("f2", "CALLS", "caller1", "callee1")
        index = _make_index(
            entities_by_id={"callee1": entity},
            facts_by_id={"f2": fact},
        )
        row = _make_row("f2")
        role = classify_edge_role(row, index, caller_perspective=False)
        self.assertEqual(role, "test_assertion")

    def test_spec_suffix_gives_test_assertion(self):
        index = self._index_with_test_entity("e3", "src/service.spec.ts")
        # test_assertion only triggered as caller_perspective=True if the subject entity is test
        fact = _make_fact("f3", "CALLS", "e3", "other")
        index2 = _make_index(
            entities_by_id={"e3": _make_entity("e3", "CodeSymbol", "src/service.spec.ts")},
            facts_by_id={"f3": fact},
        )
        row = _make_row("f3")
        role = classify_edge_role(row, index2, caller_perspective=True)
        self.assertEqual(role, "test_assertion")

    def test_non_test_path_does_not_give_test_assertion(self):
        entity = _make_entity("e4", "CodeSymbol", "src/service.py")
        fact = _make_fact("f4", "CALLS", "e4", "other")
        index = _make_index(
            entities_by_id={"e4": entity},
            facts_by_id={"f4": fact},
        )
        row = _make_row("f4")
        role = classify_edge_role(row, index, caller_perspective=True)
        self.assertNotEqual(role, "test_assertion")


# ---------------------------------------------------------------------------
# Synthetic index: persistence role from support_fact subjects
# ---------------------------------------------------------------------------

class TestPersistenceRole(unittest.TestCase):
    """Entity participates as HANDLES_MODEL/TASK_USES_MODEL/SERIALIZES_MODEL subject → persistence."""

    def _index_with_persistence(self, entity_id: str) -> EdgeRoleIndex:
        entity = _make_entity(entity_id, "CodeSymbol", "myapp/views.py")
        fact = _make_fact("f1", "CALLS", "caller", entity_id)
        return _make_index(
            persistence_ids=frozenset({entity_id}),
            entities_by_id={entity_id: entity},
            facts_by_id={"f1": fact},
        )

    def test_persistence_role_for_handles_model_subject(self):
        index = self._index_with_persistence("view1")
        row = _make_row("f1")
        role = classify_edge_role(row, index, caller_perspective=False)
        self.assertEqual(role, "persistence")

    def test_persistence_role_for_caller_perspective(self):
        entity = _make_entity("task1", "CodeSymbol", "myapp/tasks.py")
        fact = _make_fact("f2", "CALLS", "task1", "other")
        index = _make_index(
            persistence_ids=frozenset({"task1"}),
            entities_by_id={"task1": entity},
            facts_by_id={"f2": fact},
        )
        row = _make_row("f2")
        role = classify_edge_role(row, index, caller_perspective=True)
        self.assertEqual(role, "persistence")

    def test_no_persistence_role_when_no_django_facts(self):
        # Negative: empty persistence_ids → no persistence role fabricated
        index = _make_index(
            entities_by_id={"e1": _make_entity("e1", "CodeSymbol", "myapp/views.py")},
            facts_by_id={"f1": _make_fact("f1", "CALLS", "caller", "e1")},
        )
        row = _make_row("f1")
        role = classify_edge_role(row, index, caller_perspective=False)
        self.assertNotEqual(role, "persistence", "Fabricated persistence role from empty index")
        self.assertEqual(role, "intra_repo_consumer")

    def test_persistence_not_fabricated_for_external_entity(self):
        # An ExternalPackage entity is not in persistence_ids → generic_utility, not persistence
        index = _make_index(
            external_entity_ids=frozenset({"ext1"}),
            entities_by_id={"ext1": _make_entity("ext1", "ExternalPackage")},
            facts_by_id={"f1": _make_fact("f1", "CALLS", "caller", "ext1")},
        )
        row = _make_row("f1")
        role = classify_edge_role(row, index, caller_perspective=False)
        # generic_utility wins (priority check) — persistence not in index for ext1
        self.assertEqual(role, "generic_utility")
        self.assertNotEqual(role, "persistence")


# ---------------------------------------------------------------------------
# Synthetic index: external_side_effect from CALLS_ENDPOINT / event facts
# ---------------------------------------------------------------------------

class TestExternalSideEffectRole(unittest.TestCase):
    """Entity subject participates in CALLS_ENDPOINT/PRODUCES_EVENT/CONSUMES_EVENT → external_side_effect."""

    def test_calls_endpoint_subject_gives_external_side_effect(self):
        entity = _make_entity("svc1", "CodeSymbol", "services/client.py")
        fact = _make_fact("f1", "CALLS", "caller", "svc1")
        index = _make_index(
            side_effect_ids=frozenset({"svc1"}),
            entities_by_id={"svc1": entity},
            facts_by_id={"f1": fact},
        )
        row = _make_row("f1")
        role = classify_edge_role(row, index, caller_perspective=False)
        self.assertEqual(role, "external_side_effect")

    def test_no_side_effect_role_when_not_in_index(self):
        entity = _make_entity("svc2", "CodeSymbol", "services/internal.py")
        fact = _make_fact("f2", "CALLS", "caller", "svc2")
        index = _make_index(
            entities_by_id={"svc2": entity},
            facts_by_id={"f2": fact},
        )
        row = _make_row("f2")
        role = classify_edge_role(row, index, caller_perspective=False)
        self.assertNotEqual(role, "external_side_effect")
        self.assertEqual(role, "intra_repo_consumer")


# ---------------------------------------------------------------------------
# Intra-repo consumer default
# ---------------------------------------------------------------------------

class TestIntraRepoConsumerDefault(unittest.TestCase):
    """Default role when no index entry matches → intra_repo_consumer."""

    def test_default_role_for_no_fact_id(self):
        index = _make_index()
        row = {"predicate": "CALLS", "subject": "a", "object": "b"}  # no fact_id
        role = classify_edge_role(row, index, caller_perspective=False)
        self.assertEqual(role, "intra_repo_consumer")

    def test_default_role_for_unknown_fact_id(self):
        index = _make_index()
        row = _make_row("unknown-fact-id")
        role = classify_edge_role(row, index, caller_perspective=False)
        self.assertEqual(role, "intra_repo_consumer")


# ---------------------------------------------------------------------------
# Ranking and budget eviction
# ---------------------------------------------------------------------------

class TestRankByReviewValue(unittest.TestCase):
    """rank_by_review_value orders rows: test_assertion first, generic_utility last."""

    def _row(self, role: str, lead_id: str) -> dict:
        return {"lead_id": lead_id, "edge_role": role, "subject": "a", "object": "b", "predicate": "CALLS"}

    def test_generic_utility_ranked_last(self):
        rows = [
            self._row("generic_utility", "u1"),
            self._row("intra_repo_consumer", "c1"),
            self._row("test_assertion", "t1"),
        ]
        ranked = rank_by_review_value(rows)
        self.assertEqual(ranked[0]["edge_role"], "test_assertion")
        self.assertEqual(ranked[-1]["edge_role"], "generic_utility")

    def test_persistence_ranked_before_intra(self):
        rows = [self._row("intra_repo_consumer", "c1"), self._row("persistence", "p1")]
        ranked = rank_by_review_value(rows)
        self.assertEqual(ranked[0]["edge_role"], "persistence")

    def test_external_side_effect_ranked_before_intra(self):
        rows = [self._row("intra_repo_consumer", "c1"), self._row("external_side_effect", "s1")]
        ranked = rank_by_review_value(rows)
        self.assertEqual(ranked[0]["edge_role"], "external_side_effect")

    def test_utility_evicted_first_under_compaction(self):
        """After rank + compact(limit=2): generic_utility evicted, consumers survive.

        Inversion evidence: if ranking were absent, generic_utility would appear first
        (as produced by the KG) and survive compaction while consumers get evicted.
        """
        rows = [
            self._row("generic_utility", "u1"),
            self._row("intra_repo_consumer", "c1"),
            self._row("intra_repo_consumer", "c2"),
        ]
        ranked = rank_by_review_value(rows)
        # Compaction takes first `limit` rows of ranked list
        kept = _compact_relation_rows(ranked, limit=2)
        kept_roles = [r.get("edge_role") for r in kept]
        self.assertNotIn("generic_utility", kept_roles, "generic_utility survived compaction")
        self.assertEqual(kept_roles.count("intra_repo_consumer"), 2)

    def test_stable_sort_within_same_role(self):
        rows = [
            self._row("intra_repo_consumer", "c1"),
            self._row("generic_utility", "u1"),
            self._row("intra_repo_consumer", "c2"),
        ]
        ranked = rank_by_review_value(rows)
        intra_ids = [r["lead_id"] for r in ranked if r["edge_role"] == "intra_repo_consumer"]
        self.assertEqual(intra_ids, ["c1", "c2"])

    def test_no_edge_role_treated_as_intra_repo_consumer(self):
        rows = [
            {"lead_id": "u1", "edge_role": "generic_utility", "subject": "a", "object": "b"},
            {"lead_id": "c1", "subject": "a", "object": "b"},  # no edge_role
        ]
        ranked = rank_by_review_value(rows)
        # row without edge_role (treated as intra_repo_consumer priority 3) before generic (4)
        self.assertEqual(ranked[0]["lead_id"], "c1")
        self.assertEqual(ranked[1]["lead_id"], "u1")


# ---------------------------------------------------------------------------
# annotate_edge_roles
# ---------------------------------------------------------------------------

class TestAnnotateEdgeRoles(unittest.TestCase):
    def test_annotates_rows_in_place_and_returns_list(self):
        index = _make_index()
        rows = [
            {"fact_id": "f1", "subject": "a", "object": "b", "predicate": "CALLS"},
            {"fact_id": "f2", "subject": "c", "object": "d", "predicate": "CALLS"},
        ]
        result = annotate_edge_roles(rows, index, caller_perspective=False)
        self.assertIs(result, rows)
        for row in rows:
            self.assertIn("edge_role", row)
            self.assertEqual(row["edge_role"], "intra_repo_consumer")

    def test_caller_perspective_uses_subject_entity(self):
        entity = _make_entity("s1", "CodeSymbol", "tests/test_foo.py")
        fact = _make_fact("f1", "CALLS", "s1", "other")
        index = _make_index(entities_by_id={"s1": entity}, facts_by_id={"f1": fact})
        rows = [{"fact_id": "f1", "subject": "s1", "object": "other", "predicate": "CALLS"}]
        annotate_edge_roles(rows, index, caller_perspective=True)
        self.assertEqual(rows[0]["edge_role"], "test_assertion")


# ---------------------------------------------------------------------------
# edge_role preserved through _compact_relation_rows
# ---------------------------------------------------------------------------

class TestEdgeRolePreservedThroughCompaction(unittest.TestCase):
    def test_edge_role_survives_compaction(self):
        rows = [
            {
                "lead_id": "l1",
                "lead_kind": "direct_callee",
                "predicate": "CALLS",
                "edge_role": "test_assertion",
                "subject": "mod.caller",
                "object": "mod.callee",
            }
        ]
        compacted = _compact_relation_rows(rows, limit=10)
        self.assertEqual(len(compacted), 1)
        self.assertEqual(compacted[0].get("edge_role"), "test_assertion")

    def test_absent_edge_role_not_emitted_as_none(self):
        rows = [{"lead_id": "l1", "predicate": "CALLS", "subject": "a", "object": "b"}]
        compacted = _compact_relation_rows(rows, limit=10)
        self.assertEqual(len(compacted), 1)
        # edge_role=None is filtered by `if v is not None`
        self.assertNotIn("edge_role", compacted[0])

    def test_all_roles_survive_compaction(self):
        roles = ["test_assertion", "persistence", "external_side_effect", "intra_repo_consumer", "generic_utility"]
        rows = [
            {"lead_id": f"l{i}", "predicate": "CALLS", "edge_role": role, "subject": "a", "object": "b"}
            for i, role in enumerate(roles)
        ]
        compacted = _compact_relation_rows(rows, limit=10)
        compacted_roles = [r.get("edge_role") for r in compacted]
        self.assertEqual(compacted_roles, roles)
