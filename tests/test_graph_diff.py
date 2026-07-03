from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from source.kg.build.pipeline import build_kg
from source.kg.core.models import Coverage, Entity, Evidence, Fact
from source.kg.core.store import JsonlKgStore
from source.kg.query.snapshot import KgSnapshot
from source.kg.query.graph_diff import (
    GraphDelta,
    diff_snapshots,
    removed_symbols_with_surviving_referrers,
    call_edge_delta_for_paths,
    removed_test_references,
)


TENANT = "default"


def _make_snapshot(
    tmpdir: Path,
    name: str,
    entities: list[Entity],
    facts: list[Fact],
    evidence: list[Evidence] | None = None,
    coverage: list[Coverage] | None = None,
) -> KgSnapshot:
    root = tmpdir / name
    JsonlKgStore(root).write(
        entities=entities,
        facts=facts,
        evidence=evidence or [],
        coverage=coverage or [],
        manifest={"version": 1, "tenant_id": TENANT},
    )
    return KgSnapshot(root)


def _uninstrumented_coverage(repo: str, language: str, path_prefix: str = ".") -> Coverage:
    return Coverage(
        tenant_id=TENANT,
        predicate="LANGUAGE_SUPPORT",
        scope_ref={"language": language, "path_prefix": path_prefix, "repo": repo},
        state="uninstrumented",
        source_system="repo_discovery",
    )


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
        self.assertEqual(delta.uninstrumented_scopes, [])
        counts = delta.summary()
        self.assertEqual(counts["added_entities"], 0)
        self.assertEqual(counts["removed_entities"], 0)
        self.assertEqual(counts["added_facts"], 0)
        self.assertEqual(counts["removed_facts"], 0)
        self.assertEqual(counts["uninstrumented_scopes"], [])


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

    def test_one_missing_tenant_raises_value_error(self) -> None:
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
                manifest={"version": 1},
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

        added_fact_keys = {(f["predicate"], f["subject_id"], f["object_id"]) for f in delta.added_facts}
        removed_fact_keys = {(f["predicate"], f["subject_id"], f["object_id"]) for f in delta.removed_facts}

        # alpha is UNCHANGED (same URN in base and head), so look it up from the base snapshot.
        # beta exists only in base; gamma exists only in head.
        # Use next() without a default so a missing entity crashes the test loudly.
        alpha_id = next(
            e["entity_id"] for e in base_snap.entities
            if e.get("identity", {}).get("qualname") == "alpha"
        )
        beta_id = next(
            e["entity_id"] for e in base_snap.entities
            if e.get("identity", {}).get("qualname") == "beta"
        )
        gamma_id = next(
            e["entity_id"] for e in head_snap.entities
            if e.get("identity", {}).get("qualname") == "gamma"
        )

        # The real Python extractor emits CALLS at CodeSymbol grain (function-level subject).
        self.assertIn(("CALLS", alpha_id, beta_id), removed_fact_keys)
        self.assertIn(("CALLS", alpha_id, gamma_id), added_fact_keys)


def _symbol_with_path(repo: str, module: str, qualname: str, path: str, kind: str = "function") -> Entity:
    return Entity(
        "CodeSymbol",
        {"tenant_id": TENANT, "repo": repo, "module": module, "qualname": qualname, "symbol_kind": kind},
        {"path": path, "line": 1},
    )


class TestRemovedSymbolsWithSurvivingReferrers(unittest.TestCase):
    """
    Positive: fn_beta removed; fn_alpha (referrer via CALLS) survives → one row.
    Negative: both beta AND alpha removed → no row.
    Negative: beta removed but no referrers at all → no row.
    """

    def _build_base(self, tmpdir: Path, alpha: Entity, beta: Entity, call: Fact) -> KgSnapshot:
        mod = _module("svc", "svc.core")
        return _make_snapshot(tmpdir, "base", [mod, alpha, beta], [call])

    def _build_head(self, tmpdir: Path, alpha: Entity) -> KgSnapshot:
        mod = _module("svc", "svc.core")
        return _make_snapshot(tmpdir, "head", [mod, alpha], [])

    def test_positive_surviving_referrer_produces_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fn_alpha = _symbol_with_path("svc", "svc.core", "alpha", "svc/core.py")
            fn_beta = _symbol_with_path("svc", "svc.core", "beta", "svc/core.py")
            call = _calls_fact(fn_alpha, fn_beta)

            base = self._build_base(root, fn_alpha, fn_beta, call)
            head = self._build_head(root, fn_alpha)
            delta = diff_snapshots(base, head)
            rows = removed_symbols_with_surviving_referrers(delta, base, head)

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["removed_symbol"]["urn"], fn_beta.urn)
        self.assertEqual(len(row["surviving_referrers"]), 1)
        self.assertEqual(row["surviving_referrers"][0]["urn"], fn_alpha.urn)
        # Coordinates sourced from entity properties (not evidence).
        self.assertEqual(row["removed_symbol"]["coordinates"].get("path"), "svc/core.py")
        self.assertEqual(row["surviving_referrers"][0]["coordinates"].get("path"), "svc/core.py")

    def test_negative_referrer_also_removed_produces_no_row(self) -> None:
        """Both beta and alpha removed — no surviving referrers."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fn_alpha = _symbol_with_path("svc", "svc.core", "alpha", "svc/core.py")
            fn_beta = _symbol_with_path("svc", "svc.core", "beta", "svc/core.py")
            call = _calls_fact(fn_alpha, fn_beta)
            mod = _module("svc", "svc.core")

            base = _make_snapshot(root, "base", [mod, fn_alpha, fn_beta], [call])
            # head has neither alpha nor beta
            head = _make_snapshot(root, "head", [mod], [])
            delta = diff_snapshots(base, head)
            rows = removed_symbols_with_surviving_referrers(delta, base, head)

        self.assertEqual(rows, [])

    def test_negative_no_referrers_produces_no_row(self) -> None:
        """beta removed, but no fact points at it."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fn_alpha = _symbol_with_path("svc", "svc.core", "alpha", "svc/core.py")
            fn_beta = _symbol_with_path("svc", "svc.core", "beta", "svc/core.py")
            mod = _module("svc", "svc.core")

            base = _make_snapshot(root, "base", [mod, fn_alpha, fn_beta], [])
            head = _make_snapshot(root, "head", [mod, fn_alpha], [])
            delta = diff_snapshots(base, head)
            rows = removed_symbols_with_surviving_referrers(delta, base, head)

        self.assertEqual(rows, [])

    def test_empty_delta_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fn_alpha = _symbol_with_path("svc", "svc.core", "alpha", "svc/core.py")
            mod = _module("svc", "svc.core")
            snap = _make_snapshot(root, "snap", [mod, fn_alpha], [])
            delta = diff_snapshots(snap, snap)
            rows = removed_symbols_with_surviving_referrers(delta, snap, snap)

        self.assertEqual(rows, [])

    def test_coordinates_fall_back_to_evidence_bytes_ref(self) -> None:
        """Entities without properties.path get coordinates from evidence bytes_ref."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            # No properties.path on either symbol — forces the evidence fallback.
            fn_alpha = _symbol("svc", "svc.core", "alpha")
            fn_beta = _symbol("svc", "svc.core", "beta")
            call = _calls_fact(fn_alpha, fn_beta)
            mod = _module("svc", "svc.core")
            ev_beta = Evidence(
                target_type="entity",
                target_id=fn_beta.entity_id,
                derivation_class="deterministic_static",
                source_system="test-fixture",
                source_ref={"kind": "unit-test"},
                bytes_ref={"repo": "svc", "commit_sha": "0" * 40, "path": "svc/core.py", "line_start": 4, "line_end": 5},
            )
            ev_alpha = Evidence(
                target_type="entity",
                target_id=fn_alpha.entity_id,
                derivation_class="deterministic_static",
                source_system="test-fixture",
                source_ref={"kind": "unit-test"},
                bytes_ref={"repo": "svc", "commit_sha": "0" * 40, "path": "svc/core.py", "line_start": 1, "line_end": 2},
            )

            base = _make_snapshot(root, "base", [mod, fn_alpha, fn_beta], [call], evidence=[ev_alpha, ev_beta])
            head = _make_snapshot(root, "head", [mod, fn_alpha], [])
            delta = diff_snapshots(base, head)
            rows = removed_symbols_with_surviving_referrers(delta, base, head)

        self.assertEqual(len(rows), 1)
        removed_coords = rows[0]["removed_symbol"]["coordinates"]
        referrer_coords = rows[0]["surviving_referrers"][0]["coordinates"]
        self.assertEqual(removed_coords, {"path": "svc/core.py", "line": 4, "end_line": 5})
        self.assertEqual(referrer_coords, {"path": "svc/core.py", "line": 1, "end_line": 2})


class TestCallEdgeDeltaForPaths(unittest.TestCase):
    """
    Positive: CALLS fact whose subject entity has a path in the given set → included.
    Negative: CALLS fact whose neither subject nor object matches any given path → excluded.
    Non-CALLS removed/added facts are not returned.
    """

    def test_positive_added_fact_matching_path_included(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fn_alpha = _symbol_with_path("svc", "svc.core", "alpha", "svc/core.py")
            fn_beta = _symbol_with_path("svc", "svc.core", "beta", "svc/core.py")
            fn_gamma = _symbol_with_path("svc", "svc.util", "gamma", "svc/util.py")
            call_ab = _calls_fact(fn_alpha, fn_beta)
            call_ag = _calls_fact(fn_alpha, fn_gamma)
            mod_core = _module("svc", "svc.core")
            mod_util = _module("svc", "svc.util")

            base = _make_snapshot(root, "base", [mod_core, mod_util, fn_alpha, fn_beta, fn_gamma], [call_ab])
            head = _make_snapshot(root, "head", [mod_core, mod_util, fn_alpha, fn_beta, fn_gamma], [call_ag])
            delta = diff_snapshots(base, head)
            rows = call_edge_delta_for_paths(delta, base, head, ["svc/core.py"])

        # call_ag added — subject alpha is in svc/core.py → included
        added = [r for r in rows if r["change_kind"] == "added"]
        removed = [r for r in rows if r["change_kind"] == "removed"]
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]["subject_id"], fn_alpha.entity_id)
        # call_ab removed — subject alpha in svc/core.py → included
        self.assertEqual(len(removed), 1)
        self.assertEqual(removed[0]["subject_id"], fn_alpha.entity_id)

    def test_negative_edge_outside_paths_excluded(self) -> None:
        """Edge touches only svc/util.py; we query svc/core.py → excluded."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fn_alpha = _symbol_with_path("svc", "svc.util", "alpha", "svc/util.py")
            fn_beta = _symbol_with_path("svc", "svc.util", "beta", "svc/util.py")
            fn_gamma = _symbol_with_path("svc", "svc.util", "gamma", "svc/util.py")
            call_ab = _calls_fact(fn_alpha, fn_beta)
            call_ag = _calls_fact(fn_alpha, fn_gamma)
            mod = _module("svc", "svc.util")

            base = _make_snapshot(root, "base", [mod, fn_alpha, fn_beta, fn_gamma], [call_ab])
            head = _make_snapshot(root, "head", [mod, fn_alpha, fn_beta, fn_gamma], [call_ag])
            delta = diff_snapshots(base, head)
            rows = call_edge_delta_for_paths(delta, base, head, ["svc/core.py"])

        self.assertEqual(rows, [])

    def test_empty_paths_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fn_alpha = _symbol_with_path("svc", "svc.core", "alpha", "svc/core.py")
            fn_beta = _symbol_with_path("svc", "svc.core", "beta", "svc/core.py")
            fn_gamma = _symbol_with_path("svc", "svc.core", "gamma", "svc/core.py")
            call_ab = _calls_fact(fn_alpha, fn_beta)
            call_ag = _calls_fact(fn_alpha, fn_gamma)
            mod = _module("svc", "svc.core")

            base = _make_snapshot(root, "base", [mod, fn_alpha, fn_beta, fn_gamma], [call_ab])
            head = _make_snapshot(root, "head", [mod, fn_alpha, fn_beta, fn_gamma], [call_ag])
            delta = diff_snapshots(base, head)
            rows = call_edge_delta_for_paths(delta, base, head, [])

        self.assertEqual(rows, [])

    def test_dot_slash_prefix_normalized_but_parent_dir_not(self) -> None:
        """"./core.py" matches entity path "core.py"; "../core.py" does NOT
        (a character-class lstrip("./") would wrongly strip ".." and match)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fn_alpha = _symbol_with_path("svc", "core", "alpha", "core.py")
            fn_beta = _symbol_with_path("svc", "core", "beta", "core.py")
            call_ab = _calls_fact(fn_alpha, fn_beta)
            mod = _module("svc", "core")

            base = _make_snapshot(root, "base", [mod, fn_alpha, fn_beta], [call_ab])
            head = _make_snapshot(root, "head", [mod, fn_alpha, fn_beta], [])
            delta = diff_snapshots(base, head)

            dot_slash_rows = call_edge_delta_for_paths(delta, base, head, ["./core.py"])
            parent_dir_rows = call_edge_delta_for_paths(delta, base, head, ["../core.py"])

        self.assertEqual(len(dot_slash_rows), 1)
        self.assertEqual(dot_slash_rows[0]["change_kind"], "removed")
        self.assertEqual(parent_dir_rows, [])

    def test_object_path_match_includes_edge(self) -> None:
        """Removed CALLS fact where the OBJECT entity matches the given path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fn_alpha = _symbol_with_path("svc", "svc.util", "alpha", "svc/util.py")
            fn_beta = _symbol_with_path("svc", "svc.core", "beta", "svc/core.py")
            fn_gamma = _symbol_with_path("svc", "svc.util", "gamma", "svc/util.py")
            call_ab = _calls_fact(fn_alpha, fn_beta)
            call_ag = _calls_fact(fn_alpha, fn_gamma)
            mod_core = _module("svc", "svc.core")
            mod_util = _module("svc", "svc.util")

            base = _make_snapshot(root, "base", [mod_core, mod_util, fn_alpha, fn_beta, fn_gamma], [call_ab])
            head = _make_snapshot(root, "head", [mod_core, mod_util, fn_alpha, fn_beta, fn_gamma], [call_ag])
            delta = diff_snapshots(base, head)
            # query svc/core.py — only call_ab removed (object=beta is in svc/core.py)
            rows = call_edge_delta_for_paths(delta, base, head, ["svc/core.py"])

        removed = [r for r in rows if r["change_kind"] == "removed"]
        self.assertEqual(len(removed), 1)
        self.assertEqual(removed[0]["object_id"], fn_beta.entity_id)
        # call_ag added — neither subject nor object is in svc/core.py
        added = [r for r in rows if r["change_kind"] == "added"]
        self.assertEqual(len(added), 0)


class TestRemovedTestReferences(unittest.TestCase):
    """
    Positive: removed CALLS fact whose subject has a test-classified path and
              whose object still exists in head → one row.
    Negative: subject path not test-classified → excluded.
    Negative: object removed from head → excluded.
    """

    def test_positive_test_subject_surviving_object_in_head(self) -> None:
        """Test subject calls a non-test symbol; the test->sym call removed but sym survives."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fn_test = _symbol_with_path("svc", "tests.test_core", "test_alpha", "tests/test_core.py")
            fn_alpha = _symbol_with_path("svc", "svc.core", "alpha", "svc/core.py")
            fn_beta = _symbol_with_path("svc", "svc.core", "beta", "svc/core.py")
            call_ta = _calls_fact(fn_test, fn_alpha)
            call_tb = _calls_fact(fn_test, fn_beta)
            mod_test = _module("svc", "tests.test_core")
            mod_core = _module("svc", "svc.core")

            # base: test calls alpha AND beta
            base = _make_snapshot(root, "base", [mod_test, mod_core, fn_test, fn_alpha, fn_beta], [call_ta, call_tb])
            # head: test->beta call removed but fn_beta STILL EXISTS in head; test->alpha still there
            head = _make_snapshot(root, "head", [mod_test, mod_core, fn_test, fn_alpha, fn_beta], [call_ta])
            delta = diff_snapshots(base, head)
            rows = removed_test_references(delta, base, head)

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["predicate"], "CALLS")
        self.assertEqual(row["subject"]["urn"], fn_test.urn)
        self.assertEqual(row["object"]["urn"], fn_beta.urn)
        # Subject path is test-classified
        self.assertIn("test", row["subject"]["path"])

    def test_negative_non_test_subject_excluded(self) -> None:
        """Removed CALLS fact whose subject is NOT a test file → excluded."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fn_alpha = _symbol_with_path("svc", "svc.core", "alpha", "svc/core.py")
            fn_beta = _symbol_with_path("svc", "svc.core", "beta", "svc/core.py")
            fn_gamma = _symbol_with_path("svc", "svc.core", "gamma", "svc/core.py")
            call_ab = _calls_fact(fn_alpha, fn_beta)
            call_ag = _calls_fact(fn_alpha, fn_gamma)
            mod = _module("svc", "svc.core")

            base = _make_snapshot(root, "base", [mod, fn_alpha, fn_beta, fn_gamma], [call_ab])
            head = _make_snapshot(root, "head", [mod, fn_alpha, fn_beta, fn_gamma], [call_ag])
            delta = diff_snapshots(base, head)
            rows = removed_test_references(delta, base, head)

        self.assertEqual(rows, [])

    def test_negative_object_removed_from_head_excluded(self) -> None:
        """Object not in head → row excluded even when subject is a test file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fn_test = _symbol_with_path("svc", "tests.test_core", "test_alpha", "tests/test_core.py")
            fn_beta = _symbol_with_path("svc", "svc.core", "beta", "svc/core.py")
            call_tb = _calls_fact(fn_test, fn_beta)
            mod_test = _module("svc", "tests.test_core")
            mod_core = _module("svc", "svc.core")

            # base: test calls beta
            base = _make_snapshot(root, "base", [mod_test, mod_core, fn_test, fn_beta], [call_tb])
            # head: fn_beta removed from head entirely
            head = _make_snapshot(root, "head", [mod_test, mod_core, fn_test], [])
            delta = diff_snapshots(base, head)
            rows = removed_test_references(delta, base, head)

        self.assertEqual(rows, [])


class TestUninstrumentedScopes(unittest.TestCase):
    """GraphDelta.uninstrumented_scopes surfaces state='uninstrumented' coverage rows."""

    def test_uninstrumented_coverage_surfaces_in_summary(self) -> None:
        """Snapshot pair with an uninstrumented coverage row → scope appears in summary."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            mod = _module("svc", "svc.core")
            fn_a = _symbol("svc", "svc.core", "alpha")
            cov = _uninstrumented_coverage("svc", "shell", ".")

            # Both base and head are identical — entity/fact delta is empty.
            base = _make_snapshot(root, "base", [mod, fn_a], [], coverage=[cov])
            head = _make_snapshot(root, "head", [mod, fn_a], [], coverage=[cov])
            delta = diff_snapshots(base, head)

        # Entity and fact delta must be empty (same snapshots).
        self.assertEqual(delta.added_entities, {})
        self.assertEqual(delta.removed_entities, {})
        self.assertEqual(delta.added_facts, [])
        self.assertEqual(delta.removed_facts, [])

        # Uninstrumented scope must be surfaced.
        self.assertEqual(len(delta.uninstrumented_scopes), 1)
        scope = delta.uninstrumented_scopes[0]
        self.assertEqual(scope["repo"], "svc")
        self.assertEqual(scope["language"], "shell")

        # summary() must carry the same scope.
        counts = delta.summary()
        self.assertEqual(len(counts["uninstrumented_scopes"]), 1)
        # Non-vacuous: confirm the assertion actually executes on a live value.
        self.assertIsInstance(counts["uninstrumented_scopes"][0], dict)

    def test_no_uninstrumented_coverage_gives_empty_list(self) -> None:
        """Pair without uninstrumented rows → uninstrumented_scopes is empty."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            mod = _module("svc", "svc.core")
            fn_a = _symbol("svc", "svc.core", "alpha")
            # instrumented coverage row — must NOT appear in uninstrumented_scopes.
            instr_cov = Coverage(
                tenant_id=TENANT,
                predicate="LANGUAGE_SUPPORT",
                scope_ref={"language": "python", "path_prefix": ".", "repo": "svc"},
                state="instrumented",
                source_system="repo_discovery",
            )

            snap = _make_snapshot(root, "snap", [mod, fn_a], [], coverage=[instr_cov])
            delta = diff_snapshots(snap, snap)

        self.assertEqual(delta.uninstrumented_scopes, [])
        self.assertEqual(delta.summary()["uninstrumented_scopes"], [])

    def test_deduplication_across_base_and_head(self) -> None:
        """Same uninstrumented scope in both base and head appears only once."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            mod = _module("svc", "svc.core")
            fn_a = _symbol("svc", "svc.core", "alpha")
            cov = _uninstrumented_coverage("svc", "shell", ".")

            base = _make_snapshot(root, "base", [mod, fn_a], [], coverage=[cov])
            head = _make_snapshot(root, "head", [mod, fn_a], [], coverage=[cov])
            delta = diff_snapshots(base, head)

        self.assertEqual(len(delta.uninstrumented_scopes), 1)


def _calls_fact_with_qualifier(caller: Entity, callee: Entity, qualifier: dict) -> Fact:
    return Fact("CALLS", caller.entity_id, callee.entity_id, qualifier=qualifier)


class TestVolatileQualifierKeysDoNotCreateFalseDiffs(unittest.TestCase):
    """Inversion evidence: source_line/source_excerpt churn must NOT produce a delta."""

    def test_reformat_same_call_different_source_line_gives_empty_delta(self) -> None:
        """Same CALLS edge, only source_line/source_excerpt differ → empty delta (inversion test)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            mod = _module("svc", "svc.core")
            fn_alpha = _symbol("svc", "svc.core", "alpha")
            fn_beta = _symbol("svc", "svc.core", "beta")

            base_fact = _calls_fact_with_qualifier(
                fn_alpha, fn_beta,
                {"call": "beta", "source_line": "    beta()", "source_excerpt": "beta()"},
            )
            head_fact = _calls_fact_with_qualifier(
                fn_alpha, fn_beta,
                {"call": "beta", "source_line": "        beta()", "source_excerpt": "beta()  # reformatted"},
            )

            base = _make_snapshot(root, "base", [mod, fn_alpha, fn_beta], [base_fact])
            head = _make_snapshot(root, "head", [mod, fn_alpha, fn_beta], [head_fact])
            delta = diff_snapshots(base, head)

        # Hard assert: presentation churn must produce zero fact delta.
        self.assertEqual(delta.added_facts, [], msg="Reformat must not add facts")
        self.assertEqual(delta.removed_facts, [], msg="Reformat must not remove facts")

    def test_genuine_call_removal_still_detected(self) -> None:
        """Removing a CALLS edge (different callee) is still reported even with volatile stripping."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            mod = _module("svc", "svc.core")
            fn_alpha = _symbol("svc", "svc.core", "alpha")
            fn_beta = _symbol("svc", "svc.core", "beta")
            fn_gamma = _symbol("svc", "svc.core", "gamma")

            base_fact = _calls_fact_with_qualifier(
                fn_alpha, fn_beta,
                {"call": "beta", "source_line": "    beta()", "source_excerpt": "beta()"},
            )
            head_fact = _calls_fact_with_qualifier(
                fn_alpha, fn_gamma,
                {"call": "gamma", "source_line": "    gamma()", "source_excerpt": "gamma()"},
            )

            base = _make_snapshot(root, "base", [mod, fn_alpha, fn_beta, fn_gamma], [base_fact])
            head = _make_snapshot(root, "head", [mod, fn_alpha, fn_beta, fn_gamma], [head_fact])
            delta = diff_snapshots(base, head)

        removed_keys = {(f["predicate"], f["subject_id"], f["object_id"]) for f in delta.removed_facts}
        added_keys = {(f["predicate"], f["subject_id"], f["object_id"]) for f in delta.added_facts}
        self.assertIn(("CALLS", fn_alpha.entity_id, fn_beta.entity_id), removed_keys)
        self.assertIn(("CALLS", fn_alpha.entity_id, fn_gamma.entity_id), added_keys)

    def test_duplicate_call_collapse_is_pinned(self) -> None:
        """Two calls to the same callee from the same caller collapse to one identity key.

        Removing one of two duplicate calls is invisible to the set-diff — accepted behavior.
        This test pins that behavior so any future change is deliberate.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            mod = _module("svc", "svc.core")
            fn_alpha = _symbol("svc", "svc.core", "alpha")
            fn_beta = _symbol("svc", "svc.core", "beta")

            call1 = _calls_fact_with_qualifier(
                fn_alpha, fn_beta,
                {"call": "beta", "source_line": "    beta()  # first", "source_excerpt": "beta()"},
            )
            call2 = _calls_fact_with_qualifier(
                fn_alpha, fn_beta,
                {"call": "beta", "source_line": "    beta()  # second", "source_excerpt": "beta()"},
            )

            # base has both duplicate calls; head has only one
            base = _make_snapshot(root, "base", [mod, fn_alpha, fn_beta], [call1, call2])
            head = _make_snapshot(root, "head", [mod, fn_alpha, fn_beta], [call1])
            delta = diff_snapshots(base, head)

        # Accepted limitation: removing one of two identical-structure calls produces empty delta.
        self.assertEqual(delta.added_facts, [], msg="Duplicate-call collapse: no added facts expected")
        self.assertEqual(delta.removed_facts, [], msg="Duplicate-call collapse: no removed facts expected")


if __name__ == "__main__":
    unittest.main()
