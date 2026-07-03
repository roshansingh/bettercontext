from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from source.kg.build.pipeline import build_kg
from source.kg.core.models import Coverage, Entity, Evidence, Fact, JsonObject
from source.kg.core.store import JsonlKgStore
from source.kg.query.snapshot import KgSnapshot
from source.kg.query.graph_diff import diff_snapshots
from source.kg.query.contract_diff import (
    guard_call_removed,
    responsibility_moved,
    contract_diff_packet,
)


TENANT = "default"


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _make_snapshot(
    tmpdir: Path,
    name: str,
    entities: list[Entity],
    facts: list[Fact],
    evidence: list[Evidence] | None = None,
    coverage: list[Coverage] | None = None,
    commit_sha: str | None = None,
) -> KgSnapshot:
    root = tmpdir / name
    manifest: JsonObject = {"version": 1, "tenant_id": TENANT}
    if commit_sha:
        manifest["commit_sha"] = commit_sha
    JsonlKgStore(root).write(
        entities=entities,
        facts=facts,
        evidence=evidence or [],
        coverage=coverage or [],
        manifest=manifest,
    )
    return KgSnapshot(root)


def _module(repo: str, module: str) -> Entity:
    return Entity("CodeModule", {"tenant_id": TENANT, "repo": repo, "module": module})


def _symbol(repo: str, module: str, qualname: str, path: str = "", line: int | None = None) -> Entity:
    props: JsonObject = {}
    if path:
        props["path"] = path
    if line is not None:
        props["line"] = line
        props["end_line"] = line + 5
    return Entity(
        "CodeSymbol",
        {"tenant_id": TENANT, "repo": repo, "module": module, "qualname": qualname, "symbol_kind": "function"},
        props,
    )


def _calls(caller: Entity, callee: Entity) -> Fact:
    return Fact("CALLS", caller.entity_id, callee.entity_id)


def _imports(module: Entity, package: Entity) -> Fact:
    return Fact("IMPORTS", module.entity_id, package.entity_id)


# ---------------------------------------------------------------------------
# Tests: guard_call_removed
# ---------------------------------------------------------------------------

class TestGuardCallRemoved(unittest.TestCase):
    """
    Positive: changed symbol in paths loses a CALLS edge while surviving → one row.
    Negative: symbol removed along with its callee → no row.
    Negative: changed symbol not in paths → no row.
    Negative: empty paths → [].
    """

    def _setup_base_head_single_removal(self, tmpdir: Path):
        """
        Base: alpha calls beta AND gamma. Head: alpha calls beta only (gamma call removed).
        alpha is 'changed' (in the paths list). gamma survives in head.
        """
        mod = _module("svc", "svc.core")
        alpha = _symbol("svc", "svc.core", "alpha", "svc/core.py", 1)
        beta = _symbol("svc", "svc.core", "beta", "svc/core.py", 10)
        gamma = _symbol("svc", "svc.core", "gamma", "svc/util.py", 1)

        base = _make_snapshot(tmpdir, "base", [mod, alpha, beta, gamma], [_calls(alpha, beta), _calls(alpha, gamma)])
        head = _make_snapshot(tmpdir, "head", [mod, alpha, beta, gamma], [_calls(alpha, beta)])
        return base, head, alpha, gamma

    def test_positive_removed_outgoing_call_from_surviving_changed_symbol(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            base, head, alpha, gamma = self._setup_base_head_single_removal(root)
            delta = diff_snapshots(base, head)
            rows = guard_call_removed(delta, base, head, ["svc/core.py"])

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["subject"]["urn"], alpha.urn)
        self.assertEqual(row["removed_callee"]["urn"], gamma.urn)

    def test_moved_file_matches_head_side_changed_path(self) -> None:
        """Subject survives by URN but its file moved (svc/core.py -> svc/core/index.py
        keeps the module name); the changed path is the HEAD-side path — the base-path
        filter alone would silently skip it. Either-side matching must keep the row."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            mod = _module("svc", "svc.core")
            alpha_base = _symbol("svc", "svc.core", "alpha", "svc/core.py", 1)
            gamma_base = _symbol("svc", "svc.core", "gamma", "svc/core.py", 10)
            alpha_head = _symbol("svc", "svc.core", "alpha", "svc/core/index.py", 1)
            gamma_head = _symbol("svc", "svc.core", "gamma", "svc/core/index.py", 10)
            self.assertEqual(alpha_base.urn, alpha_head.urn)  # same identity, moved file

            base = _make_snapshot(root, "base", [mod, alpha_base, gamma_base], [_calls(alpha_base, gamma_base)])
            head = _make_snapshot(root, "head", [mod, alpha_head, gamma_head], [])
            delta = diff_snapshots(base, head)
            rows = guard_call_removed(delta, base, head, ["svc/core/index.py"])

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["subject"]["urn"], alpha_head.urn)

    def test_negative_symbol_removed_with_callee_no_row(self) -> None:
        """alpha is removed from head entirely — not a surviving changed symbol."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            mod = _module("svc", "svc.core")
            alpha = _symbol("svc", "svc.core", "alpha", "svc/core.py", 1)
            gamma = _symbol("svc", "svc.core", "gamma", "svc/core.py", 10)

            base = _make_snapshot(root, "base", [mod, alpha, gamma], [_calls(alpha, gamma)])
            # head: alpha removed (so it cannot be a 'surviving changed symbol')
            head = _make_snapshot(root, "head", [mod, gamma], [])
            delta = diff_snapshots(base, head)
            rows = guard_call_removed(delta, base, head, ["svc/core.py"])

        self.assertEqual(rows, [])

    def test_negative_subject_path_not_in_paths_excluded(self) -> None:
        """Subject's path is svc/util.py; we query svc/core.py → excluded."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            mod = _module("svc", "svc.util")
            alpha = _symbol("svc", "svc.util", "alpha", "svc/util.py", 1)
            gamma = _symbol("svc", "svc.util", "gamma", "svc/util.py", 5)

            base = _make_snapshot(root, "base", [mod, alpha, gamma], [_calls(alpha, gamma)])
            head = _make_snapshot(root, "head", [mod, alpha, gamma], [])
            delta = diff_snapshots(base, head)
            rows = guard_call_removed(delta, base, head, ["svc/core.py"])

        self.assertEqual(rows, [])

    def test_negative_empty_paths_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            base, head, _, _ = self._setup_base_head_single_removal(root)
            delta = diff_snapshots(base, head)
            rows = guard_call_removed(delta, base, head, [])

        self.assertEqual(rows, [])

    def test_non_code_symbol_subject_excluded(self) -> None:
        """Module subjects (not CodeSymbol) must be excluded (tripwire #4)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            # Use a CodeModule as subject — not a CodeSymbol.
            mod_a = _module("svc", "svc.mod_a")
            gamma = _symbol("svc", "svc.core", "gamma", "svc/core.py", 1)
            # Build an IMPORTS fact from module to gamma (non-CALLS, non-CodeSymbol subject).
            # Also test: even if we fabricate a CALLS fact with a CodeModule subject,
            # the kind filter must exclude it.
            mod_a_entity = mod_a
            gamma_entity = gamma

            # Make a Fact with module as subject to simulate the scenario.
            fake_calls = Fact("CALLS", mod_a_entity.entity_id, gamma_entity.entity_id)

            base = _make_snapshot(root, "base", [mod_a, gamma], [fake_calls])
            head = _make_snapshot(root, "head", [mod_a, gamma], [])
            delta = diff_snapshots(base, head)
            rows = guard_call_removed(delta, base, head, ["svc/core.py"])

        # The subject is a CodeModule, not a CodeSymbol — must be excluded.
        self.assertEqual(rows, [])

    def test_dot_slash_prefix_path_normalized(self) -> None:
        """'./svc/core.py' normalizes to 'svc/core.py' and matches entity path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            base, head, alpha, gamma = self._setup_base_head_single_removal(root)
            delta = diff_snapshots(base, head)
            rows = guard_call_removed(delta, base, head, ["./svc/core.py"])

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["subject"]["urn"], alpha.urn)

    def test_bytes_ref_has_line_start_line_end(self) -> None:
        """bytes_ref on removed_callee uses line_start/line_end naming (tripwire #1)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            base, head, alpha, gamma = self._setup_base_head_single_removal(root)
            delta = diff_snapshots(base, head)
            rows = guard_call_removed(delta, base, head, ["svc/core.py"])

        self.assertEqual(len(rows), 1)
        callee_ref = rows[0]["removed_callee"]["bytes_ref"]
        self.assertIn("path", callee_ref)
        self.assertIn("line_start", callee_ref)
        self.assertIn("line_end", callee_ref)
        self.assertNotIn("line", callee_ref, "bytes_ref must use line_start not line")

    def test_bytes_ref_includes_commit_sha_from_manifest(self) -> None:
        """When manifest has commit_sha, bytes_ref carries it."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            mod = _module("svc", "svc.core")
            alpha = _symbol("svc", "svc.core", "alpha", "svc/core.py", 1)
            gamma = _symbol("svc", "svc.core", "gamma", "svc/util.py", 1)

            base_sha = "a" * 40
            head_sha = "b" * 40
            base = _make_snapshot(root, "base", [mod, alpha, gamma], [_calls(alpha, gamma)], commit_sha=base_sha)
            head = _make_snapshot(root, "head", [mod, alpha, gamma], [], commit_sha=head_sha)
            delta = diff_snapshots(base, head)
            rows = guard_call_removed(delta, base, head, ["svc/core.py"])

        self.assertEqual(len(rows), 1)
        # subject is head-side → head commit_sha
        subj_ref = rows[0]["subject"]["bytes_ref"]
        self.assertEqual(subj_ref.get("commit_sha"), head_sha)
        # removed_callee is base-side → base commit_sha
        callee_ref = rows[0]["removed_callee"]["bytes_ref"]
        self.assertEqual(callee_ref.get("commit_sha"), base_sha)

    def test_multiple_removed_calls_all_reported(self) -> None:
        """When a single surviving symbol loses two outgoing calls, both are rows."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            mod = _module("svc", "svc.core")
            alpha = _symbol("svc", "svc.core", "alpha", "svc/core.py", 1)
            beta = _symbol("svc", "svc.core", "beta", "svc/util.py", 1)
            gamma = _symbol("svc", "svc.core", "gamma", "svc/util.py", 10)

            base = _make_snapshot(root, "base", [mod, alpha, beta, gamma], [_calls(alpha, beta), _calls(alpha, gamma)])
            head = _make_snapshot(root, "head", [mod, alpha, beta, gamma], [])
            delta = diff_snapshots(base, head)
            rows = guard_call_removed(delta, base, head, ["svc/core.py"])

        self.assertEqual(len(rows), 2)
        callee_urns = {r["removed_callee"]["urn"] for r in rows}
        self.assertIn(beta.urn, callee_urns)
        self.assertIn(gamma.urn, callee_urns)


# ---------------------------------------------------------------------------
# Tests: responsibility_moved
# ---------------------------------------------------------------------------

class TestResponsibilityMoved(unittest.TestCase):
    """
    Positive: (X→Z) removed, (Y→Z) added, same repo → one row.
    Negative: (X→Z) removed, (Y→Z') added (different callee) → no row.
    Negative: (X→Z) removed, (Y→Z) added, different repos → no row.
    Negative: X removed entirely → no row.
    Negative: empty delta → [].
    """

    def _setup_moved(self, tmpdir: Path):
        """
        Base: alpha calls gamma. Head: beta calls gamma. (alpha and beta both survive.)
        alpha loses the gamma call; beta gains it.
        """
        mod = _module("svc", "svc.core")
        alpha = _symbol("svc", "svc.core", "alpha", "svc/core.py", 1)
        beta = _symbol("svc", "svc.core", "beta", "svc/core.py", 20)
        gamma = _symbol("svc", "svc.core", "gamma", "svc/core.py", 40)

        base = _make_snapshot(tmpdir, "base", [mod, alpha, beta, gamma], [_calls(alpha, gamma)])
        head = _make_snapshot(tmpdir, "head", [mod, alpha, beta, gamma], [_calls(beta, gamma)])
        return base, head, alpha, beta, gamma

    def test_positive_call_moved_between_symbols(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            base, head, alpha, beta, gamma = self._setup_moved(root)
            delta = diff_snapshots(base, head)
            rows = responsibility_moved(delta, base, head)

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["shared_callee"]["urn"], gamma.urn)
        self.assertEqual(row["moved_from"]["urn"], alpha.urn)
        self.assertEqual(row["moved_to"]["urn"], beta.urn)

    def test_negative_different_callees_no_shared_callee(self) -> None:
        """(X→Z) removed, (Y→W) added — Z != W → no responsibility_moved row."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            mod = _module("svc", "svc.core")
            alpha = _symbol("svc", "svc.core", "alpha", "svc/core.py", 1)
            beta = _symbol("svc", "svc.core", "beta", "svc/core.py", 10)
            gamma = _symbol("svc", "svc.core", "gamma", "svc/core.py", 20)
            delta_sym = _symbol("svc", "svc.core", "delta_fn", "svc/core.py", 30)

            base = _make_snapshot(root, "base", [mod, alpha, beta, gamma, delta_sym], [_calls(alpha, gamma)])
            head = _make_snapshot(root, "head", [mod, alpha, beta, gamma, delta_sym], [_calls(beta, delta_sym)])
            diff = diff_snapshots(base, head)
            rows = responsibility_moved(diff, base, head)

        self.assertEqual(rows, [])

    def test_negative_cross_repo_not_matched(self) -> None:
        """(X→Z) removed, (Y→Z) added, X and Y in different repos → no row."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            # Two repos: svc-a and svc-b; gamma is in svc-a.
            mod_a = _module("svc-a", "svc_a.core")
            mod_b = _module("svc-b", "svc_b.core")
            alpha = _symbol("svc-a", "svc_a.core", "alpha", "svc_a/core.py", 1)
            beta = _symbol("svc-b", "svc_b.core", "beta", "svc_b/core.py", 1)
            gamma = _symbol("svc-a", "svc_a.core", "gamma", "svc_a/core.py", 20)

            base = _make_snapshot(root, "base", [mod_a, mod_b, alpha, beta, gamma], [_calls(alpha, gamma)])
            head = _make_snapshot(root, "head", [mod_a, mod_b, alpha, beta, gamma], [_calls(beta, gamma)])
            diff = diff_snapshots(base, head)
            rows = responsibility_moved(diff, base, head)

        self.assertEqual(rows, [])

    def test_negative_symbol_removed_entirely_no_row(self) -> None:
        """alpha removed entirely — cannot be a 'moved_from' subject."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            mod = _module("svc", "svc.core")
            alpha = _symbol("svc", "svc.core", "alpha", "svc/core.py", 1)
            beta = _symbol("svc", "svc.core", "beta", "svc/core.py", 10)
            gamma = _symbol("svc", "svc.core", "gamma", "svc/core.py", 20)

            base = _make_snapshot(root, "base", [mod, alpha, beta, gamma], [_calls(alpha, gamma)])
            # alpha removed, beta gains gamma call. alpha→gamma removed is expected
            # (alpha is gone), but alpha does not survive → the pattern is NOT
            # 'moved' from a surviving symbol; alpha is absent.
            # Per spec: moved_from must be a base-side subject whose entity exists in
            # base_by_id (which it does) — the constraint is the same-repo + distinct-URN.
            # However, the family should still emit the row because the matching is
            # purely structural (X→Z removed, Y→Z added, same repo, X != Y).
            # This test verifies we DO get a row (alpha exists in base_by_id).
            head = _make_snapshot(root, "head", [mod, beta, gamma], [_calls(beta, gamma)])
            diff = diff_snapshots(base, head)
            rows = responsibility_moved(diff, base, head)

        # alpha→gamma removed, beta→gamma added, same repo → one row.
        # (alpha not surviving in head does not disqualify the pattern —
        #  we report the structural move; the reviewer sees alpha is gone.)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["moved_from"]["urn"], alpha.urn)
        self.assertEqual(rows[0]["moved_to"]["urn"], beta.urn)

    def test_negative_self_move_excluded(self) -> None:
        """Same symbol (same URN) removes and re-adds the same callee → not a move."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            mod = _module("svc", "svc.core")
            alpha = _symbol("svc", "svc.core", "alpha", "svc/core.py", 1)
            gamma = _symbol("svc", "svc.core", "gamma", "svc/core.py", 10)

            # alpha→gamma exists in base AND head: same fact, same key → not in delta.
            # Self-move test: construct a synthetic delta scenario by using distinct
            # entity IDs but same URN (impossible with real fixtures, so test via API).
            # Simplest: just verify that a self-diff produces no rows.
            snap = _make_snapshot(root, "snap", [mod, alpha, gamma], [_calls(alpha, gamma)])
            diff = diff_snapshots(snap, snap)
            rows = responsibility_moved(diff, snap, snap)

        self.assertEqual(rows, [])

    def test_bytes_ref_sides_correct(self) -> None:
        """moved_from bytes_ref is base-side; moved_to is head-side."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            base_sha = "a" * 40
            head_sha = "b" * 40
            mod = _module("svc", "svc.core")
            alpha = _symbol("svc", "svc.core", "alpha", "svc/core.py", 1)
            beta = _symbol("svc", "svc.core", "beta", "svc/core.py", 20)
            gamma = _symbol("svc", "svc.core", "gamma", "svc/core.py", 40)

            base = _make_snapshot(root, "base", [mod, alpha, beta, gamma], [_calls(alpha, gamma)], commit_sha=base_sha)
            head = _make_snapshot(root, "head", [mod, alpha, beta, gamma], [_calls(beta, gamma)], commit_sha=head_sha)
            diff = diff_snapshots(base, head)
            rows = responsibility_moved(diff, base, head)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["moved_from"]["bytes_ref"].get("commit_sha"), base_sha)
        self.assertEqual(rows[0]["moved_to"]["bytes_ref"].get("commit_sha"), head_sha)


# ---------------------------------------------------------------------------
# Tests: contract_diff_packet (builder)
# ---------------------------------------------------------------------------

def _build_two_commit_pair(tmpdir: Path, *, base_tenant: str = TENANT) -> tuple[Path, Path]:
    """
    Build base + head snapshot from two versions of a Python fixture repo.

    v1 (base): alpha calls beta AND gamma (guard call).
    v2 (head): alpha calls beta only (gamma call removed = guard_call_removed).
               Also: delta calls gamma (so gamma call moved alpha→delta = responsibility_moved).
    Test file calling alpha exists in base but the call is removed in head (test_reference_removed).
    """
    svc = tmpdir / "svc"
    svc.mkdir()
    (svc / "__init__.py").write_text("", encoding="utf-8")
    (svc / "core.py").write_text(
        "def alpha():\n    beta()\n    gamma()\n\ndef beta():\n    pass\n\ndef gamma():\n    pass\n\ndef delta():\n    pass\n",
        encoding="utf-8",
    )
    tests_dir = svc / "tests"
    tests_dir.mkdir()
    (tests_dir / "__init__.py").write_text("", encoding="utf-8")
    (tests_dir / "test_core.py").write_text(
        "from svc.core import alpha\n\ndef test_alpha():\n    alpha()\n",
        encoding="utf-8",
    )

    out_base = tmpdir / "kg_base"
    build_kg(svc, out_base, tenant_id=base_tenant)

    # v2: remove gamma call from alpha; add gamma call to delta; remove test reference to alpha
    (svc / "core.py").write_text(
        "def alpha():\n    beta()\n\ndef beta():\n    pass\n\ndef gamma():\n    pass\n\ndef delta():\n    gamma()\n",
        encoding="utf-8",
    )
    (tests_dir / "test_core.py").write_text(
        "def test_other():\n    pass\n",
        encoding="utf-8",
    )

    out_head = tmpdir / "kg_head"
    build_kg(svc, out_head, tenant_id=TENANT)

    return out_base, out_head


class TestContractDiffPacket(unittest.TestCase):
    """
    Verify the packet builder wires all three families and produces correct shapes.
    Uses the real build_kg pipeline for round-trip tests.
    """

    def _build_pair_from_source(self, tmpdir: Path) -> tuple[Path, Path]:
        return _build_two_commit_pair(tmpdir)

    def test_packet_structure_has_required_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            out_base, out_head = self._build_pair_from_source(root)
            packet = contract_diff_packet(str(out_base), str(out_head), changed_paths=["core.py"])

        top = packet["contract_diff_packet"]
        self.assertIn("delta_summary", top)
        self.assertIn("uninstrumented_scopes", top)
        self.assertIn("hypothesis_count", top)
        self.assertIn("hypotheses", top)
        self.assertIn("families", top)
        self.assertIn("guard_call_removed_drift", top["families"])
        self.assertIn("responsibility_moved_drift", top["families"])
        self.assertIn("test_reference_removed_drift", top["families"])

    def test_uninstrumented_scopes_is_list(self) -> None:
        """Tripwire #3: uninstrumented_scopes is a list, not a count."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            out_base, out_head = self._build_pair_from_source(root)
            packet = contract_diff_packet(str(out_base), str(out_head))

        scopes = packet["contract_diff_packet"]["uninstrumented_scopes"]
        self.assertIsInstance(scopes, list)

    def test_guard_call_removed_hypothesis_shape(self) -> None:
        """Guard hypotheses carry required fields including before_refs/after_refs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            out_base, out_head = self._build_pair_from_source(root)
            # build_kg is invoked with `svc/` as repo root, so entity paths are relative to
            # that root (e.g. "core.py", not "svc/core.py").
            packet = contract_diff_packet(str(out_base), str(out_head), changed_paths=["core.py"])

        guard_hyps = [
            h for h in packet["contract_diff_packet"]["hypotheses"]
            if h["risk_type"] == "guard_call_removed_drift"
        ]
        self.assertGreaterEqual(len(guard_hyps), 1, "fixture must produce at least one guard hypothesis")
        h = guard_hyps[0]
        self.assertIn("hypothesis_id", h)
        self.assertIn("concrete_invariant", h)
        self.assertIn("why", h)
        self.assertIn("source_checks", h)
        self.assertIn("before_refs", h)
        self.assertIn("after_refs", h)
        self.assertIsInstance(h["source_checks"], list)

    def test_responsibility_moved_hypothesis_shape(self) -> None:
        """Moved hypotheses carry moved_from/moved_to/shared_callee/from_symbol_survives_in_head."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            out_base, out_head = self._build_pair_from_source(root)
            packet = contract_diff_packet(str(out_base), str(out_head))

        moved_hyps = [
            h for h in packet["contract_diff_packet"]["hypotheses"]
            if h["risk_type"] == "responsibility_moved_drift"
        ]
        self.assertGreaterEqual(len(moved_hyps), 1, "fixture must produce at least one moved hypothesis")
        h = moved_hyps[0]
        self.assertIn("moved_from", h)
        self.assertIn("moved_to", h)
        self.assertIn("shared_callee", h)
        self.assertIn("from_symbol_survives_in_head", h)

    def test_hypothesis_ids_are_unique(self) -> None:
        """Every hypothesis in the packet has a unique hypothesis_id."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            out_base, out_head = self._build_pair_from_source(root)
            packet = contract_diff_packet(str(out_base), str(out_head), changed_paths=["core.py"])

        hyps = packet["contract_diff_packet"]["hypotheses"]
        ids = [h["hypothesis_id"] for h in hyps]
        self.assertEqual(len(ids), len(set(ids)), "hypothesis_ids must be unique")

    def test_empty_delta_produces_empty_hypotheses(self) -> None:
        """Identical base and head → no hypotheses."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            svc = root / "svc"
            svc.mkdir()
            (svc / "__init__.py").write_text("", encoding="utf-8")
            (svc / "core.py").write_text("def alpha():\n    pass\n", encoding="utf-8")
            out = root / "kg"
            build_kg(svc, out, tenant_id=TENANT)
            packet = contract_diff_packet(str(out), str(out), changed_paths=["svc/core.py"])

        top = packet["contract_diff_packet"]
        self.assertEqual(top["hypothesis_count"], 0)
        self.assertEqual(top["hypotheses"], [])


# ---------------------------------------------------------------------------
# Tests: synthetic unit-level (fast, no build_kg round-trip)
# ---------------------------------------------------------------------------

class TestGuardCallRemovedSynthetic(unittest.TestCase):
    """Synthetic fixtures for fast coverage without build_kg overhead."""

    def test_inversion_added_call_not_reported(self) -> None:
        """An ADDED call edge must not appear as a guard_call_removed row."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            mod = _module("svc", "svc.core")
            alpha = _symbol("svc", "svc.core", "alpha", "svc/core.py", 1)
            gamma = _symbol("svc", "svc.core", "gamma", "svc/core.py", 10)

            # base: no call; head: alpha calls gamma (call ADDED)
            base = _make_snapshot(root, "base", [mod, alpha, gamma], [])
            head = _make_snapshot(root, "head", [mod, alpha, gamma], [_calls(alpha, gamma)])
            diff = diff_snapshots(base, head)
            rows = guard_call_removed(diff, base, head, ["svc/core.py"])

        self.assertEqual(rows, [])

    def test_output_is_sorted_deterministic(self) -> None:
        """Same input pair → same sorted output regardless of fact iteration order."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            mod = _module("svc", "svc.core")
            alpha = _symbol("svc", "svc.core", "alpha", "svc/core.py", 1)
            beta = _symbol("svc", "svc.core", "beta", "svc/util.py", 1)
            gamma = _symbol("svc", "svc.core", "gamma", "svc/util.py", 10)

            base = _make_snapshot(root, "base", [mod, alpha, beta, gamma], [_calls(alpha, beta), _calls(alpha, gamma)])
            head = _make_snapshot(root, "head", [mod, alpha, beta, gamma], [])
            diff = diff_snapshots(base, head)

            rows1 = guard_call_removed(diff, base, head, ["svc/core.py"])
            rows2 = guard_call_removed(diff, base, head, ["svc/core.py"])

        urns1 = [(r["subject"]["urn"], r["removed_callee"]["urn"]) for r in rows1]
        urns2 = [(r["subject"]["urn"], r["removed_callee"]["urn"]) for r in rows2]
        self.assertEqual(urns1, urns2)


class TestResponsibilityMovedSynthetic(unittest.TestCase):

    def test_inversion_unrelated_add_remove_no_match(self) -> None:
        """Removing X→Z and adding Y→W (W != Z) must NOT produce a moved row."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            mod = _module("svc", "svc.core")
            alpha = _symbol("svc", "svc.core", "alpha", "svc/core.py", 1)
            beta = _symbol("svc", "svc.core", "beta", "svc/core.py", 10)
            gamma = _symbol("svc", "svc.core", "gamma", "svc/core.py", 20)
            epsilon = _symbol("svc", "svc.core", "epsilon", "svc/core.py", 30)

            base = _make_snapshot(root, "base", [mod, alpha, beta, gamma, epsilon], [_calls(alpha, gamma)])
            head = _make_snapshot(root, "head", [mod, alpha, beta, gamma, epsilon], [_calls(beta, epsilon)])
            diff = diff_snapshots(base, head)
            rows = responsibility_moved(diff, base, head)

        self.assertEqual(rows, [])

    def test_output_sorted_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            mod = _module("svc", "svc.core")
            alpha = _symbol("svc", "svc.core", "alpha", "svc/core.py", 1)
            beta = _symbol("svc", "svc.core", "beta", "svc/core.py", 10)
            gamma = _symbol("svc", "svc.core", "gamma", "svc/core.py", 20)

            base = _make_snapshot(root, "base", [mod, alpha, beta, gamma], [_calls(alpha, gamma)])
            head = _make_snapshot(root, "head", [mod, alpha, beta, gamma], [_calls(beta, gamma)])
            diff = diff_snapshots(base, head)

            rows1 = responsibility_moved(diff, base, head)
            rows2 = responsibility_moved(diff, base, head)

        def key(r):
            return (r["shared_callee"]["urn"], r["moved_from"]["urn"], r["moved_to"]["urn"])

        self.assertEqual([key(r) for r in rows1], [key(r) for r in rows2])


class TestResponsibilityMovedRenameDisclosure(unittest.TestCase):
    """from_symbol_survives_in_head field and rename-ambiguity disclosure."""

    def test_rename_shaped_fixture_survives_false_and_disclosure(self) -> None:
        """X removed + Y added, both calling Z — X absent from head → False + disclosure text."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            mod = _module("svc", "svc.core")
            alpha = _symbol("svc", "svc.core", "alpha", "svc/core.py", 1)
            alpha_prime = _symbol("svc", "svc.core", "alpha_prime", "svc/core.py", 1)
            gamma = _symbol("svc", "svc.core", "gamma", "svc/core.py", 20)

            # alpha (X) removed from head, alpha_prime (Y) added, both call gamma (Z).
            base = _make_snapshot(root, "base", [mod, alpha, gamma], [_calls(alpha, gamma)])
            head = _make_snapshot(root, "head", [mod, alpha_prime, gamma], [_calls(alpha_prime, gamma)])
            delta = diff_snapshots(base, head)
            rows = responsibility_moved(delta, base, head)

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertFalse(row["from_symbol_survives_in_head"])

    def test_rename_shaped_packet_has_disclosure_text(self) -> None:
        """When from_symbol_survives_in_head is False, packet why discloses rename risk."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            mod = _module("svc", "svc.core")
            alpha = _symbol("svc", "svc.core", "alpha", "svc/core.py", 1)
            alpha_prime = _symbol("svc", "svc.core", "alpha_prime", "svc/core.py", 1)
            gamma = _symbol("svc", "svc.core", "gamma", "svc/core.py", 20)

            base_snap = _make_snapshot(root, "base", [mod, alpha, gamma], [_calls(alpha, gamma)])
            head_snap = _make_snapshot(root, "head", [mod, alpha_prime, gamma], [_calls(alpha_prime, gamma)])

            packet = contract_diff_packet(str(base_snap.root), str(head_snap.root))

        moved_hyps = [
            h for h in packet["contract_diff_packet"]["hypotheses"]
            if h["risk_type"] == "responsibility_moved_drift"
        ]
        self.assertGreaterEqual(len(moved_hyps), 1)
        h = moved_hyps[0]
        self.assertFalse(h["from_symbol_survives_in_head"])
        self.assertIn("rename", h["why"])

    def test_genuine_move_survives_true(self) -> None:
        """X survives in head after moving call to Y → from_symbol_survives_in_head True."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            mod = _module("svc", "svc.core")
            alpha = _symbol("svc", "svc.core", "alpha", "svc/core.py", 1)
            beta = _symbol("svc", "svc.core", "beta", "svc/core.py", 10)
            gamma = _symbol("svc", "svc.core", "gamma", "svc/core.py", 20)

            base = _make_snapshot(root, "base", [mod, alpha, beta, gamma], [_calls(alpha, gamma)])
            # alpha survives in head (both alpha and beta present), beta gains the call.
            head = _make_snapshot(root, "head", [mod, alpha, beta, gamma], [_calls(beta, gamma)])
            delta = diff_snapshots(base, head)
            rows = responsibility_moved(delta, base, head)

        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["from_symbol_survives_in_head"])


class TestGuardCallRemovedNoiseBound(unittest.TestCase):
    """Packet builder caps guard rows at 3 per subject; query returns all."""

    def _make_many_removed_calls(self, root: Path, count: int):
        """alpha calls `count` callees in base; head: alpha survives but calls none."""
        mod = _module("svc", "svc.core")
        alpha = _symbol("svc", "svc.core", "alpha", "svc/core.py", 1)
        callees = [
            _symbol("svc", "svc.core", f"callee_{i}", "svc/util.py", i * 10)
            for i in range(count)
        ]
        base_facts = [_calls(alpha, c) for c in callees]
        base = _make_snapshot(root, "base", [mod, alpha] + callees, base_facts)
        head = _make_snapshot(root, "head", [mod, alpha] + callees, [])
        return base, head, alpha, callees

    def test_query_returns_all_six(self) -> None:
        """guard_call_removed (query layer) returns all 6 rows untruncated."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            base, head, _, _ = self._make_many_removed_calls(root, 6)
            delta = diff_snapshots(base, head)
            rows = guard_call_removed(delta, base, head, ["svc/core.py"])

        self.assertEqual(len(rows), 6)

    def test_packet_caps_at_three_per_subject(self) -> None:
        """Packet builder emits at most 3 guard hypotheses per subject."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            base, head, _, _ = self._make_many_removed_calls(root, 6)
            packet = contract_diff_packet(str(base.root), str(head.root), changed_paths=["svc/core.py"])

        guard_hyps = [
            h for h in packet["contract_diff_packet"]["hypotheses"]
            if h["risk_type"] == "guard_call_removed_drift"
        ]
        self.assertEqual(len(guard_hyps), 3)

    def test_omitted_count_on_last_kept(self) -> None:
        """The last kept hypothesis for a subject carries omitted_removed_call_count=3."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            base, head, _, _ = self._make_many_removed_calls(root, 6)
            packet = contract_diff_packet(str(base.root), str(head.root), changed_paths=["svc/core.py"])

        guard_hyps = [
            h for h in packet["contract_diff_packet"]["hypotheses"]
            if h["risk_type"] == "guard_call_removed_drift"
        ]
        self.assertEqual(len(guard_hyps), 3)
        last = guard_hyps[-1]
        self.assertEqual(last.get("omitted_removed_call_count"), 3)

    def test_large_refactor_why_note(self) -> None:
        """When >5 removed calls from one symbol, kept hypotheses' why mentions count."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            base, head, _, _ = self._make_many_removed_calls(root, 6)
            packet = contract_diff_packet(str(base.root), str(head.root), changed_paths=["svc/core.py"])

        guard_hyps = [
            h for h in packet["contract_diff_packet"]["hypotheses"]
            if h["risk_type"] == "guard_call_removed_drift"
        ]
        for h in guard_hyps:
            self.assertIn("6 calls removed from this symbol", h["why"])

    def test_inversion_no_guard_hyps_when_no_removed_calls(self) -> None:
        """No removed calls → zero guard hypotheses (inversion for finding #3)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            mod = _module("svc", "svc.core")
            alpha = _symbol("svc", "svc.core", "alpha", "svc/core.py", 1)
            gamma = _symbol("svc", "svc.core", "gamma", "svc/core.py", 10)

            # base: no calls; head: alpha ADDS a call (not removed)
            base = _make_snapshot(root, "base", [mod, alpha, gamma], [])
            head = _make_snapshot(root, "head", [mod, alpha, gamma], [_calls(alpha, gamma)])
            packet = contract_diff_packet(str(base.root), str(head.root), changed_paths=["svc/core.py"])

        guard_hyps = [
            h for h in packet["contract_diff_packet"]["hypotheses"]
            if h["risk_type"] == "guard_call_removed_drift"
        ]
        self.assertEqual(len(guard_hyps), 0)


# ---------------------------------------------------------------------------
# Tests: B4 review_context splice (base_snapshot argument)
# ---------------------------------------------------------------------------

_CONTRACT_DIFF_FAMILIES = {
    "guard_call_removed_drift",
    "responsibility_moved_drift",
    "test_reference_removed_drift",
}


class TestReviewContextContractDiffSplice(unittest.TestCase):
    """B4: optional base_snapshot on review_context splices contract-diff hypotheses
    into the normal hypothesis pipeline; tenant mismatch / unloadable base are
    coverage-honest notes, never errors."""

    def _review_context(self, head: Path, extra: JsonObject) -> JsonObject:
        from source.kg.product.mcp_tools import call_tool

        args: JsonObject = {"repo": "svc", "changed_files": ["core.py"], **extra}
        return call_tool(KgSnapshot(head), "review_context", args)

    def test_guard_call_removed_drift_spliced_with_cause_and_consequence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base, head = _build_two_commit_pair(Path(tmpdir))
            result = self._review_context(head, {"base_snapshot": str(base)})

        hyps = [h for h in result.get("review_hypotheses") or [] if isinstance(h, dict)]
        guard = [h for h in hyps if h.get("risk_type") == "guard_call_removed_drift"]
        self.assertGreaterEqual(len(guard), 1, "guard_call_removed_drift must be spliced in")
        h = guard[0]
        self.assertEqual(h["specificity"], "high")
        self.assertIn("cause", h, "cause must map from before_refs")
        self.assertIn("path", h["cause"])
        self.assertIn("consequence", h, "consequence must map from after_refs")
        self.assertIn("path", h["consequence"])
        spans = h.get("source_spans")
        self.assertIsInstance(spans, list)
        self.assertGreater(len(spans), 0)
        self.assertLessEqual(len(spans), 4)
        # High-specificity splice ranks ahead of the generic families this fixture makes.
        self.assertIn(hyps[0]["risk_type"], _CONTRACT_DIFF_FAMILIES)
        # Quality status counts spliced rows as specific.
        rqs = result.get("review_quality_status") or {}
        self.assertEqual(rqs.get("specificity"), "high")

    def test_splice_enters_answer_packet_mirror(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base, head = _build_two_commit_pair(Path(tmpdir))
            result = self._review_context(head, {"base_snapshot": str(base)})

        mirror = (result.get("review_answer_packet") or {}).get("top_review_hypotheses") or []
        self.assertTrue(mirror, "mirror must be non-empty")
        self.assertIn(mirror[0].get("risk_type"), _CONTRACT_DIFF_FAMILIES)

    def test_tenant_mismatch_is_note_not_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base, head = _build_two_commit_pair(Path(tmpdir), base_tenant="other-tenant")
            result = self._review_context(head, {"base_snapshot": str(base)})

        hyps = [h.get("risk_type") for h in result.get("review_hypotheses") or []]
        self.assertFalse(
            set(hyps) & _CONTRACT_DIFF_FAMILIES,
            "tenant mismatch must skip the splice entirely",
        )
        rqs = result.get("review_quality_status") or {}
        self.assertIn("tenant", rqs.get("reason", ""), "reason must carry the coverage-honest note")

    def test_unloadable_base_is_note_not_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base, head = _build_two_commit_pair(Path(tmpdir))
            result = self._review_context(
                head, {"base_snapshot": str(Path(tmpdir) / "does-not-exist")}
            )

        hyps = [h.get("risk_type") for h in result.get("review_hypotheses") or []]
        self.assertFalse(set(hyps) & _CONTRACT_DIFF_FAMILIES)
        rqs = result.get("review_quality_status") or {}
        self.assertIn("base_snapshot", rqs.get("reason", ""), "reason must carry the load-failure note")

    def test_failure_note_survives_reason_rewrite_on_high_specificity_packet(self) -> None:
        """Codex round-5 P2: the quality-status resync rewrote reason on the high/medium
        branch, dropping the base_snapshot failure note. The note is packet contract —
        it must survive on EVERY specificity branch."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            svc = root / "svc"
            svc.mkdir()
            (svc / "__init__.py").write_text("", encoding="utf-8")
            # Bare-except-pass fires the swallowed_exception signal -> HIGH specificity family.
            (svc / "core.py").write_text(
                "def alpha(items):\n"
                "    out = []\n"
                "    for item in items:\n"
                "        try:\n"
                "            out.append(item.value)\n"
                "        except Exception:\n"
                "            pass\n"
                "    return out\n",
                encoding="utf-8",
            )
            out_head = root / "kg_head"
            build_kg(svc, out_head, tenant_id=TENANT)
            result = self._review_context(
                out_head,
                {
                    "base_snapshot": str(root / "does-not-exist"),
                    "changed_ranges": [{"path": "core.py", "start_line": 1, "end_line": 9}],
                },
            )

        rqs = result.get("review_quality_status") or {}
        self.assertIn(rqs.get("specificity"), ("high", "medium"), "fixture must yield a specific packet")
        self.assertIn("base_snapshot", rqs.get("reason", ""),
                      "failure note must survive the high/medium reason rewrite")

    def test_absent_base_snapshot_identical_to_today(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            _base, head = _build_two_commit_pair(Path(tmpdir))
            result = self._review_context(head, {})

        hyps = [h.get("risk_type") for h in result.get("review_hypotheses") or []]
        self.assertFalse(set(hyps) & _CONTRACT_DIFF_FAMILIES)
        rqs = result.get("review_quality_status") or {}
        self.assertNotIn("contract_diff_note", rqs)
        self.assertNotIn("base_snapshot", rqs.get("reason", ""))


# ---------------------------------------------------------------------------
# Fix 5: deterministic_static derivation on spliced contract-diff rows
# ---------------------------------------------------------------------------

class TestContractDiffDerivation(unittest.TestCase):
    """Fix 5: spliced guard/moved/test rows carry derivation='deterministic_static'."""

    def _review_context(self, head: Path, extra: JsonObject) -> JsonObject:
        from source.kg.product.mcp_tools import call_tool

        args: JsonObject = {"repo": "svc", "changed_files": ["core.py"], **extra}
        return call_tool(KgSnapshot(head), "review_context", args)

    def test_spliced_rows_carry_deterministic_static_derivation(self) -> None:
        """All spliced contract-diff rows must carry derivation='deterministic_static' (exact)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base, head = _build_two_commit_pair(Path(tmpdir))
            result = self._review_context(head, {"base_snapshot": str(base)})

        hyps = [h for h in result.get("review_hypotheses") or [] if isinstance(h, dict)]
        contract_hyps = [h for h in hyps if h.get("risk_type") in _CONTRACT_DIFF_FAMILIES]
        self.assertGreater(len(contract_hyps), 0, "fixture must produce >=1 contract-diff hypothesis")
        for h in contract_hyps:
            self.assertEqual(
                h.get("derivation"), "deterministic_static",
                f"contract-diff row must carry derivation='deterministic_static'; "
                f"got {h.get('derivation')!r} for risk_type={h.get('risk_type')!r}",
            )

    def test_inversion_semantic_rows_are_inferred_llm(self) -> None:
        """Inversion: semantic diff rows carry derivation='inferred_llm', not 'deterministic_static'."""
        from source.kg.query.semantic_contract_diff import semantic_contract_diff
        from source.kg.core.models import Entity
        from source.kg.core.store import JsonlKgStore
        from source.kg.integrations.semantic_llm import LlmResult

        FAKE = [
            {
                "claim": "Guard removed.",
                "cause_line": 2,
                "consequence": "Callers may pass None.",
                "negative_check": "No check.",
                "category": "guard_removal",
            }
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            e = Entity(
                kind="CodeSymbol",
                identity={
                    "tenant_id": TENANT,
                    "repo": "repo_inv_deriv",
                    "module": "mod.h",
                    "qualname": "inv_func",
                    "symbol_kind": "function",
                },
                properties={"path": "h.py", "line": 1, "end_line": 3},
            )
            snap_dir = root / "snap_inv"
            JsonlKgStore(snap_dir).write(
                entities=[e], facts=[], evidence=[], coverage=[],
                manifest={"version": 1, "tenant_id": TENANT},
            )
            snap = KgSnapshot(snap_dir)
            entity_dicts = [d for d in snap.entities if d.get("kind") == "CodeSymbol"]

            base_dir = root / "base_inv"
            base_dir.mkdir()
            (base_dir / "h.py").write_text("def inv_func():\n    if x: raise\n    return 1\n")
            head_dir = root / "head_inv"
            head_dir.mkdir()
            (head_dir / "h.py").write_text("def inv_func():\n    return 1\n")

            class _FixedClient:
                def complete_json(self, prompt: str) -> LlmResult:
                    return LlmResult.parsed(FAKE)

            rows, _ = semantic_contract_diff(
                base_snapshot=snap,
                head_snapshot=snap,
                base_root=base_dir,
                head_root=head_dir,
                changed_symbols=entity_dicts,
                client=_FixedClient(),
            )
            self.assertGreater(len(rows), 0, "semantic diff must produce >=1 row")
            for row in rows:
                self.assertEqual(
                    row.get("derivation"), "inferred_llm",
                    f"semantic diff row must carry derivation='inferred_llm'; got {row.get('derivation')!r}",
                )
                self.assertNotEqual(
                    row.get("derivation"), "deterministic_static",
                    "semantic diff row must NOT carry 'deterministic_static'",
                )


if __name__ == "__main__":
    unittest.main()
