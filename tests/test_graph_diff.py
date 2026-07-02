from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from source.kg.build.pipeline import build_kg
from source.kg.core.models import Coverage, Entity, Evidence, Fact
from source.kg.core.store import JsonlKgStore
from source.kg.query.snapshot import KgSnapshot
from source.kg.query.graph_diff import GraphDelta, diff_snapshots


TENANT = "default"


def _make_snapshot(tmpdir: Path, name: str, entities: list[Entity], facts: list[Fact]) -> KgSnapshot:
    root = tmpdir / name
    evidence: list[Evidence] = []
    coverage: list[Coverage] = []
    JsonlKgStore(root).write(
        entities=entities,
        facts=facts,
        evidence=evidence,
        coverage=coverage,
        manifest={"version": 1, "tenant_id": TENANT},
    )
    return KgSnapshot(root)


def _module(repo: str, module: str) -> Entity:
    return Entity("CodeModule", {"tenant_id": TENANT, "repo": repo, "module": module})


def _symbol(repo: str, module: str, qualname: str, kind: str = "function") -> Entity:
    return Entity("CodeSymbol", {"tenant_id": TENANT, "repo": repo, "module": module, "qualname": qualname, "symbol_kind": kind})


def _calls_fact(caller: Entity, callee: Entity) -> Fact:
    return Fact("CALLS", caller.entity_id, callee.entity_id)


class TestSelfDiffIsEmpty(unittest.TestCase):
    def test_self_diff_returns_empty_delta(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            mod = _module("svc", "svc.core")
            fn_a = _symbol("svc", "svc.core", "alpha")
            fn_b = _symbol("svc", "svc.core", "beta")
            call = _calls_fact(fn_a, fn_b)

            snap = _make_snapshot(root, "snap", [mod, fn_a, fn_b], [call])
            delta = diff_snapshots(snap, snap)

        self.assertEqual(delta.added_entities, {})
        self.assertEqual(delta.removed_entities, {})
        self.assertEqual(delta.added_facts, [])
        self.assertEqual(delta.removed_facts, [])
        counts = delta.summary()
        self.assertEqual(counts["added_entities"], 0)
        self.assertEqual(counts["removed_entities"], 0)
        self.assertEqual(counts["added_facts"], 0)
        self.assertEqual(counts["removed_facts"], 0)


class TestBasicDiff(unittest.TestCase):
    """
    Base snapshot: mod, fn_alpha, fn_beta, CALLS(alpha->beta)
    Head snapshot: mod, fn_alpha, fn_gamma (beta removed, gamma added), CALLS(alpha->gamma)
    Expected:
      added_entities: {CodeSymbol: [fn_gamma]}
      removed_entities: {CodeSymbol: [fn_beta]}
      added_facts: [CALLS(alpha->gamma)]
      removed_facts: [CALLS(alpha->beta)]
    """

    def _build_pair(self, tmpdir: Path):
        mod = _module("svc", "svc.core")
        fn_alpha = _symbol("svc", "svc.core", "alpha")
        fn_beta = _symbol("svc", "svc.core", "beta")
        fn_gamma = _symbol("svc", "svc.core", "gamma")
        call_ab = _calls_fact(fn_alpha, fn_beta)
        call_ag = _calls_fact(fn_alpha, fn_gamma)

        base = _make_snapshot(tmpdir, "base", [mod, fn_alpha, fn_beta], [call_ab])
        head = _make_snapshot(tmpdir, "head", [mod, fn_alpha, fn_gamma], [call_ag])
        return base, head, fn_beta, fn_gamma, call_ab, call_ag

    def test_added_and_removed_entities(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            base, head, fn_beta, fn_gamma, call_ab, call_ag = self._build_pair(root)
            delta = diff_snapshots(base, head)

        added_urns = {e["urn"] for e in delta.added_entities.get("CodeSymbol", [])}
        removed_urns = {e["urn"] for e in delta.removed_entities.get("CodeSymbol", [])}
        self.assertIn(fn_gamma.urn, added_urns)
        self.assertIn(fn_beta.urn, removed_urns)
        self.assertNotIn(fn_beta.urn, added_urns)
        self.assertNotIn(fn_gamma.urn, removed_urns)

    def test_added_and_removed_facts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            base, head, fn_beta, fn_gamma, call_ab, call_ag = self._build_pair(root)
            delta = diff_snapshots(base, head)

        added_keys = {(f["predicate"], f["subject_id"], f["object_id"]) for f in delta.added_facts}
        removed_keys = {(f["predicate"], f["subject_id"], f["object_id"]) for f in delta.removed_facts}
        self.assertIn(("CALLS", call_ag.subject_id, call_ag.object_id), added_keys)
        self.assertIn(("CALLS", call_ab.subject_id, call_ab.object_id), removed_keys)
        self.assertNotIn(("CALLS", call_ab.subject_id, call_ab.object_id), added_keys)
        self.assertNotIn(("CALLS", call_ag.subject_id, call_ag.object_id), removed_keys)

    def test_unchanged_entities_not_in_delta(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            base, head, fn_beta, fn_gamma, call_ab, call_ag = self._build_pair(root)
            mod = _module("svc", "svc.core")
            fn_alpha = _symbol("svc", "svc.core", "alpha")
            delta = diff_snapshots(base, head)

        all_added_urns = {e["urn"] for es in delta.added_entities.values() for e in es}
        all_removed_urns = {e["urn"] for es in delta.removed_entities.values() for e in es}
        self.assertNotIn(mod.urn, all_added_urns)
        self.assertNotIn(mod.urn, all_removed_urns)
        self.assertNotIn(fn_alpha.urn, all_added_urns)
        self.assertNotIn(fn_alpha.urn, all_removed_urns)

    def test_summary_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            base, head, fn_beta, fn_gamma, call_ab, call_ag = self._build_pair(root)
            delta = diff_snapshots(base, head)

        counts = delta.summary()
        self.assertEqual(counts["added_entities"], 1)
        self.assertEqual(counts["removed_entities"], 1)
        self.assertEqual(counts["added_facts"], 1)
        self.assertEqual(counts["removed_facts"], 1)


class TestDeterminism(unittest.TestCase):
    def test_repeated_diff_is_identical(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            mod = _module("svc", "svc.util")
            fn_a = _symbol("svc", "svc.util", "foo")
            fn_b = _symbol("svc", "svc.util", "bar")
            fn_c = _symbol("svc", "svc.util", "baz")
            base = _make_snapshot(root, "base", [mod, fn_a, fn_b], [_calls_fact(fn_a, fn_b)])
            head = _make_snapshot(root, "head", [mod, fn_a, fn_c], [_calls_fact(fn_a, fn_c)])

            delta1 = diff_snapshots(base, head)
            delta2 = diff_snapshots(base, head)

        self.assertEqual(
            [e["urn"] for es in delta1.added_entities.values() for e in es],
            [e["urn"] for es in delta2.added_entities.values() for e in es],
        )
        self.assertEqual(
            [e["urn"] for es in delta1.removed_entities.values() for e in es],
            [e["urn"] for es in delta2.removed_entities.values() for e in es],
        )
        self.assertEqual(
            [(f["predicate"], f["subject_id"], f["object_id"]) for f in delta1.added_facts],
            [(f["predicate"], f["subject_id"], f["object_id"]) for f in delta2.added_facts],
        )
        self.assertEqual(
            [(f["predicate"], f["subject_id"], f["object_id"]) for f in delta1.removed_facts],
            [(f["predicate"], f["subject_id"], f["object_id"]) for f in delta2.removed_facts],
        )


class TestTenantMismatchRaises(unittest.TestCase):
    def test_different_tenants_raise_value_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            mod = _module("svc", "svc.core")
            fn = _symbol("svc", "svc.core", "foo")

            base_root = root / "base"
            JsonlKgStore(base_root).write(
                entities=[mod, fn],
                facts=[],
                evidence=[],
                coverage=[],
                manifest={"version": 1, "tenant_id": "tenant-a"},
            )
            head_root = root / "head"
            JsonlKgStore(head_root).write(
                entities=[mod, fn],
                facts=[],
                evidence=[],
                coverage=[],
                manifest={"version": 1, "tenant_id": "tenant-b"},
            )
            base_snap = KgSnapshot(base_root)
            head_snap = KgSnapshot(head_root)

            with self.assertRaises(ValueError) as ctx:
                diff_snapshots(base_snap, head_snap)

        self.assertIn("tenant", str(ctx.exception).lower())


class TestBuildKgRoundTrip(unittest.TestCase):
    """Build two real snapshots via build_kg and verify diff sees the structural change."""

    def test_add_and_remove_function_shows_in_delta(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            # v1: two functions, one call
            svc = root / "svc"
            svc.mkdir()
            (svc / "__init__.py").write_text("", encoding="utf-8")
            (svc / "core.py").write_text(
                "def alpha():\n    beta()\n\ndef beta():\n    pass\n",
                encoding="utf-8",
            )

            out_base = root / "kg_base"
            build_kg(svc, out_base, tenant_id=TENANT)

            # v2: beta removed, gamma added, call changes to alpha->gamma
            (svc / "core.py").write_text(
                "def alpha():\n    gamma()\n\ndef gamma():\n    pass\n",
                encoding="utf-8",
            )
            out_head = root / "kg_head"
            build_kg(svc, out_head, tenant_id=TENANT)

            base_snap = KgSnapshot(out_base)
            head_snap = KgSnapshot(out_head)
            delta = diff_snapshots(base_snap, head_snap)

        added_qualnames = {
            e["identity"]["qualname"]
            for es in delta.added_entities.values()
            for e in es
        }
        removed_qualnames = {
            e["identity"]["qualname"]
            for es in delta.removed_entities.values()
            for e in es
        }
        self.assertIn("gamma", added_qualnames)
        self.assertIn("beta", removed_qualnames)


if __name__ == "__main__":
    unittest.main()
