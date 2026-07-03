from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from source.kg.build.pipeline import build_kg
from source.kg.core.models import Entity, Fact
from source.kg.core.store import JsonlKgStore
from source.kg.query.snapshot import KgSnapshot
from source.scripts import diff_kg as diff_kg_cli


TENANT = "default"


def _make_snapshot(
    tmpdir: Path,
    name: str,
    entities: list[Entity],
    facts: list[Fact],
) -> Path:
    root = tmpdir / name
    JsonlKgStore(root).write(
        entities=entities,
        facts=facts,
        evidence=[],
        coverage=[],
        manifest={"version": 1, "tenant_id": TENANT},
    )
    return root


def _module(repo: str, module: str) -> Entity:
    return Entity("CodeModule", {"tenant_id": TENANT, "repo": repo, "module": module})


def _symbol(repo: str, module: str, qualname: str, path: str | None = None) -> Entity:
    props: dict = {}
    if path is not None:
        props["path"] = path
        props["line"] = 1
    return Entity(
        "CodeSymbol",
        {"tenant_id": TENANT, "repo": repo, "module": module, "qualname": qualname, "symbol_kind": "function"},
        props,
    )


def _calls(caller: Entity, callee: Entity) -> Fact:
    return Fact("CALLS", caller.entity_id, callee.entity_id)


def _run_main(argv: list[str]) -> str:
    """Run diff_kg_cli.main() with given argv and return captured stdout."""
    buf = io.StringIO()
    with patch.object(sys, "argv", argv):
        with patch("sys.stdout", buf):
            diff_kg_cli.main()
    return buf.getvalue()


class TestSummaryCommand(unittest.TestCase):
    def test_summary_returns_json_with_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            mod = _module("svc", "svc.core")
            fn_alpha = _symbol("svc", "svc.core", "alpha")
            fn_beta = _symbol("svc", "svc.core", "beta")
            fn_gamma = _symbol("svc", "svc.core", "gamma")
            call_ab = _calls(fn_alpha, fn_beta)
            call_ag = _calls(fn_alpha, fn_gamma)

            base_dir = _make_snapshot(root, "base", [mod, fn_alpha, fn_beta], [call_ab])
            head_dir = _make_snapshot(root, "head", [mod, fn_alpha, fn_gamma], [call_ag])

            output = _run_main([
                "supercontext-diff-kg",
                "--base-snapshot", str(base_dir),
                "--head-snapshot", str(head_dir),
                "summary",
            ])

        result = json.loads(output)
        # beta removed (1), gamma added (1), call_ab removed (1), call_ag added (1)
        self.assertEqual(result["removed_entities"], 1)
        self.assertEqual(result["added_entities"], 1)
        self.assertEqual(result["removed_facts"], 1)
        self.assertEqual(result["added_facts"], 1)

    def test_summary_self_diff_all_zeros(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            mod = _module("svc", "svc.core")
            fn_alpha = _symbol("svc", "svc.core", "alpha")
            snap_dir = _make_snapshot(root, "snap", [mod, fn_alpha], [])

            output = _run_main([
                "supercontext-diff-kg",
                "--base-snapshot", str(snap_dir),
                "--head-snapshot", str(snap_dir),
                "summary",
            ])

        result = json.loads(output)
        self.assertEqual(result["removed_entities"], 0)
        self.assertEqual(result["added_entities"], 0)
        self.assertEqual(result["removed_facts"], 0)
        self.assertEqual(result["added_facts"], 0)


class TestRemovedFormerlyReferencedCommand(unittest.TestCase):
    def test_removed_formerly_referenced_returns_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            mod = _module("svc", "svc.core")
            fn_alpha = _symbol("svc", "svc.core", "alpha", "svc/core.py")
            fn_beta = _symbol("svc", "svc.core", "beta", "svc/core.py")
            call_ab = _calls(fn_alpha, fn_beta)

            # base has alpha+beta; head only has alpha — beta removed, alpha survives
            base_dir = _make_snapshot(root, "base", [mod, fn_alpha, fn_beta], [call_ab])
            head_dir = _make_snapshot(root, "head", [mod, fn_alpha], [])

            output = _run_main([
                "supercontext-diff-kg",
                "--base-snapshot", str(base_dir),
                "--head-snapshot", str(head_dir),
                "removed-formerly-referenced",
            ])

        result = json.loads(output)
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 1)
        row = result[0]
        # removed_symbol must be beta
        self.assertEqual(row["removed_symbol"]["urn"], fn_beta.urn)
        # alpha is the former referrer — field is "former_referrers", not "surviving_referrers"
        self.assertIn("former_referrers", row)
        self.assertNotIn("surviving_referrers", row)
        self.assertEqual(len(row["former_referrers"]), 1)
        self.assertEqual(row["former_referrers"][0]["urn"], fn_alpha.urn)

    def test_removed_formerly_referenced_empty_when_no_surviving_referrers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            mod = _module("svc", "svc.core")
            fn_alpha = _symbol("svc", "svc.core", "alpha", "svc/core.py")
            fn_beta = _symbol("svc", "svc.core", "beta", "svc/core.py")
            call_ab = _calls(fn_alpha, fn_beta)

            # both removed in head
            base_dir = _make_snapshot(root, "base", [mod, fn_alpha, fn_beta], [call_ab])
            head_dir = _make_snapshot(root, "head", [mod], [])

            output = _run_main([
                "supercontext-diff-kg",
                "--base-snapshot", str(base_dir),
                "--head-snapshot", str(head_dir),
                "removed-formerly-referenced",
            ])

        result = json.loads(output)
        self.assertEqual(result, [])


class TestRemovedTestReferencesCommand(unittest.TestCase):
    def test_removed_test_references_returns_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            mod_test = _module("svc", "tests.test_core")
            mod_core = _module("svc", "svc.core")
            fn_test = _symbol("svc", "tests.test_core", "test_alpha", "tests/test_core.py")
            fn_beta = _symbol("svc", "svc.core", "beta", "svc/core.py")
            call_tb = _calls(fn_test, fn_beta)

            # base: test calls beta; head: test file gone but fn_beta still exists
            base_dir = _make_snapshot(
                root, "base",
                [mod_test, mod_core, fn_test, fn_beta],
                [call_tb],
            )
            head_dir = _make_snapshot(
                root, "head",
                [mod_test, mod_core, fn_test, fn_beta],
                [],
            )

            output = _run_main([
                "supercontext-diff-kg",
                "--base-snapshot", str(base_dir),
                "--head-snapshot", str(head_dir),
                "removed-test-references",
            ])

        result = json.loads(output)
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 1)
        row = result[0]
        self.assertEqual(row["predicate"], "CALLS")
        self.assertEqual(row["subject"]["urn"], fn_test.urn)
        self.assertEqual(row["object"]["urn"], fn_beta.urn)

    def test_removed_test_references_empty_for_non_test_subject(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            mod = _module("svc", "svc.core")
            fn_alpha = _symbol("svc", "svc.core", "alpha", "svc/core.py")
            fn_beta = _symbol("svc", "svc.core", "beta", "svc/core.py")
            call_ab = _calls(fn_alpha, fn_beta)

            base_dir = _make_snapshot(root, "base", [mod, fn_alpha, fn_beta], [call_ab])
            head_dir = _make_snapshot(root, "head", [mod, fn_alpha, fn_beta], [])

            output = _run_main([
                "supercontext-diff-kg",
                "--base-snapshot", str(base_dir),
                "--head-snapshot", str(head_dir),
                "removed-test-references",
            ])

        result = json.loads(output)
        self.assertEqual(result, [])


class TestCallEdgeDeltaCommand(unittest.TestCase):
    def test_call_edge_delta_returns_changed_facts_for_matching_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            mod = _module("svc", "svc.core")
            fn_alpha = _symbol("svc", "svc.core", "alpha", "svc/core.py")
            fn_beta = _symbol("svc", "svc.core", "beta", "svc/core.py")
            fn_gamma = _symbol("svc", "svc.core", "gamma", "svc/core.py")
            call_ab = _calls(fn_alpha, fn_beta)
            call_ag = _calls(fn_alpha, fn_gamma)

            base_dir = _make_snapshot(root, "base", [mod, fn_alpha, fn_beta, fn_gamma], [call_ab])
            head_dir = _make_snapshot(root, "head", [mod, fn_alpha, fn_beta, fn_gamma], [call_ag])

            output = _run_main([
                "supercontext-diff-kg",
                "--base-snapshot", str(base_dir),
                "--head-snapshot", str(head_dir),
                "call-edge-delta",
                "--path", "svc/core.py",
            ])

        result = json.loads(output)
        self.assertIsInstance(result, list)
        added = [r for r in result if r["change_kind"] == "added"]
        removed = [r for r in result if r["change_kind"] == "removed"]
        self.assertEqual(len(added), 1)
        self.assertEqual(len(removed), 1)
        self.assertEqual(added[0]["subject_id"], fn_alpha.entity_id)
        self.assertEqual(removed[0]["subject_id"], fn_alpha.entity_id)

    def test_call_edge_delta_repeatable_path_arg(self) -> None:
        """Two --path flags both get respected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            mod = _module("svc", "svc.core")
            fn_a = _symbol("svc", "svc.core", "a", "svc/a.py")
            fn_b = _symbol("svc", "svc.core", "b", "svc/b.py")
            call_ab = _calls(fn_a, fn_b)

            base_dir = _make_snapshot(root, "base", [mod, fn_a, fn_b], [call_ab])
            head_dir = _make_snapshot(root, "head", [mod, fn_a, fn_b], [])

            output = _run_main([
                "supercontext-diff-kg",
                "--base-snapshot", str(base_dir),
                "--head-snapshot", str(head_dir),
                "call-edge-delta",
                "--path", "svc/a.py",
                "--path", "svc/b.py",
            ])

        result = json.loads(output)
        removed = [r for r in result if r["change_kind"] == "removed"]
        self.assertEqual(len(removed), 1)

    def test_call_edge_delta_no_paths_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            mod = _module("svc", "svc.core")
            fn_a = _symbol("svc", "svc.core", "a", "svc/a.py")
            fn_b = _symbol("svc", "svc.core", "b", "svc/b.py")
            call_ab = _calls(fn_a, fn_b)

            base_dir = _make_snapshot(root, "base", [mod, fn_a, fn_b], [call_ab])
            head_dir = _make_snapshot(root, "head", [mod, fn_a, fn_b], [])

            output = _run_main([
                "supercontext-diff-kg",
                "--base-snapshot", str(base_dir),
                "--head-snapshot", str(head_dir),
                "call-edge-delta",
            ])

        result = json.loads(output)
        self.assertEqual(result, [])


class TestBuildKgRoundTripE2E(unittest.TestCase):
    """End-to-end: build two real snapshots via build_kg, drive every CLI command."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        root = Path(self._tmpdir.name)

        svc = root / "svc"
        svc.mkdir()
        (svc / "__init__.py").write_text("", encoding="utf-8")
        (svc / "core.py").write_text(
            "def alpha():\n    beta()\n\ndef beta():\n    pass\n",
            encoding="utf-8",
        )
        (svc / "tests").mkdir()
        (svc / "tests" / "__init__.py").write_text("", encoding="utf-8")
        (svc / "tests" / "test_core.py").write_text(
            "from svc.core import beta\ndef test_beta():\n    beta()\n",
            encoding="utf-8",
        )

        out_base = root / "kg_base"
        build_kg(svc, out_base, tenant_id=TENANT)

        # v2: beta removed, gamma added; test still imports beta (which is gone)
        (svc / "core.py").write_text(
            "def alpha():\n    gamma()\n\ndef gamma():\n    pass\n",
            encoding="utf-8",
        )
        out_head = root / "kg_head"
        build_kg(svc, out_head, tenant_id=TENANT)

        self._base_dir = out_base
        self._head_dir = out_head
        self._root = root

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_summary_shows_real_adds_and_removes(self) -> None:
        output = _run_main([
            "supercontext-diff-kg",
            "--base-snapshot", str(self._base_dir),
            "--head-snapshot", str(self._head_dir),
            "summary",
        ])
        result = json.loads(output)
        # beta removed, gamma added → at least 1 each
        self.assertGreaterEqual(result["removed_entities"], 1)
        self.assertGreaterEqual(result["added_entities"], 1)

    def test_removed_formerly_referenced_real_snapshots(self) -> None:
        # Scenario: base alpha→beta; head removes beta, alpha→gamma.
        # beta must appear with alpha as a FORMER referrer; no field claims current reference.
        output = _run_main([
            "supercontext-diff-kg",
            "--base-snapshot", str(self._base_dir),
            "--head-snapshot", str(self._head_dir),
            "removed-formerly-referenced",
        ])
        result = json.loads(output)
        self.assertIsInstance(result, list)
        removed_qualnames = {r["removed_symbol"]["qualname"] for r in result}
        self.assertIn("beta", removed_qualnames)

        # Verify honest contract: field is "former_referrers", never "surviving_referrers".
        for row in result:
            self.assertIn("former_referrers", row)
            self.assertNotIn("surviving_referrers", row)
            # alpha updated to call gamma — referrer_likely_unchanged must be False for alpha.
            alpha_rows = [r for r in row["former_referrers"] if r.get("qualname") == "alpha"]
            for alpha_ref in alpha_rows:
                self.assertIs(alpha_ref.get("referrer_likely_unchanged"), False)

    def test_call_edge_delta_real_snapshots(self) -> None:
        # core.py path in the real snapshot
        base_snap = KgSnapshot(self._base_dir)
        sample_path = next(
            e.get("properties", {}).get("path")
            for e in base_snap.entities
            if e.get("identity", {}).get("qualname") == "alpha"
            and e.get("properties", {}).get("path")
        )
        output = _run_main([
            "supercontext-diff-kg",
            "--base-snapshot", str(self._base_dir),
            "--head-snapshot", str(self._head_dir),
            "call-edge-delta",
            "--path", sample_path,
        ])
        result = json.loads(output)
        self.assertIsInstance(result, list)
        # At least the CALLS(alpha->beta) removed and CALLS(alpha->gamma) added
        self.assertGreaterEqual(len(result), 2)
        change_kinds = {r["change_kind"] for r in result}
        self.assertIn("added", change_kinds)
        self.assertIn("removed", change_kinds)


class TestErrorPaths(unittest.TestCase):
    def test_missing_base_snapshot_exits_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            mod = _module("svc", "svc.core")
            head_dir = _make_snapshot(root, "head", [mod], [])

            with patch.object(sys, "argv", [
                "supercontext-diff-kg",
                "--base-snapshot", str(root / "nonexistent"),
                "--head-snapshot", str(head_dir),
                "summary",
            ]):
                with self.assertRaises(SystemExit) as ctx:
                    diff_kg_cli.main()

        self.assertNotEqual(ctx.exception.code, 0)

    def test_missing_head_snapshot_exits_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            mod = _module("svc", "svc.core")
            base_dir = _make_snapshot(root, "base", [mod], [])

            with patch.object(sys, "argv", [
                "supercontext-diff-kg",
                "--base-snapshot", str(base_dir),
                "--head-snapshot", str(root / "nonexistent"),
                "summary",
            ]):
                with self.assertRaises(SystemExit) as ctx:
                    diff_kg_cli.main()

        self.assertNotEqual(ctx.exception.code, 0)

    def test_tenant_mismatch_propagates_as_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            mod = _module("svc", "svc.core")

            base_root = root / "base"
            JsonlKgStore(base_root).write(
                entities=[mod],
                facts=[],
                evidence=[],
                coverage=[],
                manifest={"version": 1, "tenant_id": "tenant-a"},
            )
            head_root = root / "head"
            JsonlKgStore(head_root).write(
                entities=[mod],
                facts=[],
                evidence=[],
                coverage=[],
                manifest={"version": 1, "tenant_id": "tenant-b"},
            )

            with patch.object(sys, "argv", [
                "supercontext-diff-kg",
                "--base-snapshot", str(base_root),
                "--head-snapshot", str(head_root),
                "summary",
            ]):
                with self.assertRaises(SystemExit) as ctx:
                    diff_kg_cli.main()

        self.assertNotEqual(ctx.exception.code, 0)

    def test_missing_required_args_exits_2(self) -> None:
        """No --base-snapshot → argparse error → exit code 2."""
        with patch.object(sys, "argv", ["supercontext-diff-kg"]):
            with self.assertRaises(SystemExit) as ctx:
                diff_kg_cli.main()

        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
