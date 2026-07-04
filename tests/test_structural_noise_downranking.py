from __future__ import annotations

import unittest

from source.kg.product.review_hypotheses import (
    _BOOST_CAUSE_IN_CHANGED_PROD_FILE,
    _BOOST_CONCRETE_FAILURE_MODE,
    _PENALTY_BOTH_PATHS_TEST,
    _PENALTY_EXTERNAL_CALL_TARGET,
    _PENALTY_MODULE_ROOT_TARGET,
    _structural_noise_score,
    apply_structural_noise_downranking,
)

# All fixtures below use generic synthetic names (foo/bar/mod_a/pkg_root, generic
# builtin-shaped names) — never the field-evidence names — proving each rule keys on
# STRUCTURE (KG entity kind, path segments, claim shape), not on any name/keyword list.

_EMPTY = frozenset()


def _guard_row(*, target_kind: str = "", target_urn: str = "", cause: dict | None = None,
               consequence: dict | None = None) -> dict:
    row: dict = {"risk_type": "guard_call_removed_drift", "derivation": "deterministic_static"}
    if target_kind:
        row["target_entity_kind"] = target_kind
    if target_urn:
        row["target_urn"] = target_urn
    if cause is not None:
        row["cause"] = cause
    if consequence is not None:
        row["consequence"] = consequence
    return row


def _moved_row(*, target_kind: str = "", cause: dict | None = None,
               consequence: dict | None = None) -> dict:
    row: dict = {"risk_type": "responsibility_moved_drift", "derivation": "deterministic_static"}
    if target_kind:
        row["target_entity_kind"] = target_kind
    if cause is not None:
        row["cause"] = cause
    if consequence is not None:
        row["consequence"] = consequence
    return row


class TestExternalCallTargetPenalty(unittest.TestCase):
    """Rule 1: removed/moved call into a builtin/external entity, no changed-symbol overlap."""

    def test_positive_builtin_target_penalized(self) -> None:
        # ExternalSymbol == language builtin callee (structural kind, not a name match).
        row = _guard_row(target_kind="ExternalSymbol", target_urn="urn:external-symbol:x")
        self.assertEqual(_structural_noise_score(row, _EMPTY, _EMPTY), _PENALTY_EXTERNAL_CALL_TARGET)

    def test_positive_external_package_target_penalized(self) -> None:
        row = _guard_row(target_kind="ExternalPackage", target_urn="urn:external-package:pkg_root")
        # ExternalPackage triggers BOTH the external-call and the module-root penalties.
        self.assertEqual(
            _structural_noise_score(row, _EMPTY, _EMPTY),
            _PENALTY_EXTERNAL_CALL_TARGET + _PENALTY_MODULE_ROOT_TARGET,
        )

    def test_negative_codesymbol_target_not_penalized(self) -> None:
        # Inversion: same row shape, only the entity KIND differs → no penalty.
        row = _guard_row(target_kind="CodeSymbol", target_urn="urn:code-symbol:foo")
        self.assertEqual(_structural_noise_score(row, _EMPTY, _EMPTY), 0.0)

    def test_negative_changed_symbol_overlap_suppresses_penalty(self) -> None:
        # Overlap = the target URN references a symbol the PR itself changed → not noise.
        row = _guard_row(target_kind="ExternalSymbol", target_urn="urn:external-symbol:foo_helper")
        self.assertEqual(
            _structural_noise_score(row, _EMPTY, frozenset({"foo_helper"})), 0.0
        )

    def test_negative_non_call_risk_type_not_penalized(self) -> None:
        # A non-call risk type with the same kind field must not fire this rule.
        row = {"risk_type": "test_reference_removed_drift", "target_entity_kind": "ExternalSymbol",
               "target_urn": "urn:external-symbol:x", "derivation": "deterministic_static"}
        self.assertEqual(_structural_noise_score(row, _EMPTY, _EMPTY), 0.0)


class TestBothPathsTestPenalty(unittest.TestCase):
    """Rule 2: cause AND consequence are both test-classified paths."""

    def test_positive_both_test_paths_penalized(self) -> None:
        row = _guard_row(
            target_kind="CodeSymbol",
            cause={"path": "tests/test_mod_a.py", "line_start": 3},
            consequence={"path": "src/__tests__/mod_b.spec.ts", "line_start": 9},
        )
        self.assertEqual(_structural_noise_score(row, _EMPTY, _EMPTY), _PENALTY_BOTH_PATHS_TEST)

    def test_negative_only_cause_is_test_path(self) -> None:
        # Inversion: consequence is production → rule must NOT fire.
        row = _guard_row(
            target_kind="CodeSymbol",
            cause={"path": "tests/test_mod_a.py", "line_start": 3},
            consequence={"path": "src/mod_b.py", "line_start": 9},
        )
        self.assertEqual(_structural_noise_score(row, _EMPTY, _EMPTY), 0.0)

    def test_negative_both_production_paths(self) -> None:
        row = _guard_row(
            target_kind="CodeSymbol",
            cause={"path": "src/mod_a.py", "line_start": 3},
            consequence={"path": "src/mod_b.py", "line_start": 9},
        )
        self.assertEqual(_structural_noise_score(row, _EMPTY, _EMPTY), 0.0)


class TestModuleRootTargetPenalty(unittest.TestCase):
    """Rule 3: moved-call target is a bare module/package root entity."""

    def test_positive_module_root_target_penalized(self) -> None:
        # CodeModule == a bare module root (entity kind, not segment counting).
        row = _moved_row(target_kind="CodeModule")
        self.assertEqual(_structural_noise_score(row, _EMPTY, _EMPTY), _PENALTY_MODULE_ROOT_TARGET)

    def test_negative_symbol_target_not_penalized(self) -> None:
        # Inversion: a real CodeSymbol target → no module-root penalty.
        row = _moved_row(target_kind="CodeSymbol")
        self.assertEqual(_structural_noise_score(row, _EMPTY, _EMPTY), 0.0)


class TestConcreteFailureModeBoost(unittest.TestCase):
    """Rule 4: row carries a concrete failure mode in its claim structure."""

    def test_positive_unimplemented_members_boosted(self) -> None:
        row = {"risk_type": "abstract_contract_unimplemented", "derivation": "deterministic_static",
               "unimplemented_members": ["do_thing", "do_other"]}
        self.assertEqual(_structural_noise_score(row, _EMPTY, _EMPTY), _BOOST_CONCRETE_FAILURE_MODE)

    def test_negative_no_failure_mode_field(self) -> None:
        # Inversion: same derivation, no concrete-failure-mode structure → no boost.
        row = {"risk_type": "abstract_contract_unimplemented", "derivation": "deterministic_static"}
        self.assertEqual(_structural_noise_score(row, _EMPTY, _EMPTY), 0.0)

    def test_negative_empty_members_list(self) -> None:
        row = {"risk_type": "abstract_contract_unimplemented", "derivation": "deterministic_static",
               "unimplemented_members": []}
        self.assertEqual(_structural_noise_score(row, _EMPTY, _EMPTY), 0.0)


class TestChangedProdFileCauseBoost(unittest.TestCase):
    """Rule 5: cause path is a production file present in the PR's changed files."""

    def test_positive_changed_prod_file_boosted(self) -> None:
        row = _guard_row(target_kind="CodeSymbol", cause={"path": "src/mod_a.py", "line_start": 3},
                         consequence={"path": "src/mod_b.py", "line_start": 9})
        self.assertEqual(
            _structural_noise_score(row, frozenset({"src/mod_a.py"}), _EMPTY),
            _BOOST_CAUSE_IN_CHANGED_PROD_FILE,
        )

    def test_negative_cause_not_in_changed_files(self) -> None:
        # Inversion: cause path is production but NOT changed by this PR → no boost.
        row = _guard_row(target_kind="CodeSymbol", cause={"path": "src/mod_a.py", "line_start": 3})
        self.assertEqual(_structural_noise_score(row, frozenset({"src/other.py"}), _EMPTY), 0.0)

    def test_negative_test_file_cause_not_boosted(self) -> None:
        # A changed TEST file cause must not earn the production-file boost.
        row = _guard_row(target_kind="CodeSymbol", cause={"path": "tests/test_mod_a.py", "line_start": 3})
        self.assertEqual(
            _structural_noise_score(row, frozenset({"tests/test_mod_a.py"}), _EMPTY), 0.0
        )


class TestReorderWithinPeerGroups(unittest.TestCase):
    """apply_structural_noise_downranking: reorders within (derivation, risk_type) groups only."""

    def test_noisy_row_sinks_below_clean_peer_same_family(self) -> None:
        # Two deterministic guard rows (SAME family): noisy (builtin target) first, clean second.
        noisy = _guard_row(target_kind="ExternalSymbol", target_urn="urn:external-symbol:x")
        noisy["hypothesis_id"] = "noisy"
        clean = _guard_row(target_kind="CodeSymbol", target_urn="urn:code-symbol:foo",
                           cause={"path": "src/mod_a.py", "line_start": 1},
                           consequence={"path": "src/mod_b.py", "line_start": 2})
        clean["hypothesis_id"] = "clean"
        out = apply_structural_noise_downranking([noisy, clean], changed_files=[], changed_symbols=[])
        self.assertEqual([r["hypothesis_id"] for r in out], ["clean", "noisy"])

    def test_does_not_reorder_across_derivation_tiers(self) -> None:
        # Two rows, same risk_type but DIFFERENT derivation tiers: a HIGH-scoring inferred_llm
        # row must NOT jump ahead of a low-scoring deterministic_static row (tier boundary).
        det_noisy = _guard_row(target_kind="ExternalSymbol", target_urn="urn:external-symbol:x")
        det_noisy["hypothesis_id"] = "det"
        inferred_boosted = {"risk_type": "guard_call_removed_drift",
                            "derivation": "inferred_llm", "unimplemented_members": ["m"],
                            "hypothesis_id": "inf"}
        out = apply_structural_noise_downranking(
            [det_noisy, inferred_boosted], changed_files=[], changed_symbols=[]
        )
        # Different tiers → not peers → order preserved (det first) despite score inversion.
        self.assertEqual([r["hypothesis_id"] for r in out], ["det", "inf"])

    def test_does_not_reorder_across_families_same_tier(self) -> None:
        # Same derivation tier, DIFFERENT risk_type families: a boosted abstract-contract
        # row must NOT jump ahead of a noisy guard row — family interleaving is preserved
        # so the downstream cap's per-family survival guarantee is untouched.
        noisy_guard = _guard_row(target_kind="ExternalSymbol", target_urn="urn:external-symbol:x")
        noisy_guard["hypothesis_id"] = "guard"
        boosted_abstract = {"risk_type": "abstract_contract_unimplemented",
                            "derivation": "deterministic_static",
                            "unimplemented_members": ["m"], "hypothesis_id": "abstract"}
        out = apply_structural_noise_downranking(
            [noisy_guard, boosted_abstract], changed_files=[], changed_symbols=[]
        )
        self.assertEqual([r["hypothesis_id"] for r in out], ["guard", "abstract"])

    def test_stable_tiebreak_preserves_prior_order(self) -> None:
        a = _guard_row(target_kind="CodeSymbol", target_urn="urn:code-symbol:a")
        a["hypothesis_id"] = "a"
        b = _guard_row(target_kind="CodeSymbol", target_urn="urn:code-symbol:b")
        b["hypothesis_id"] = "b"
        out = apply_structural_noise_downranking([a, b], changed_files=[], changed_symbols=[])
        self.assertEqual([r["hypothesis_id"] for r in out], ["a", "b"])

    def test_length_and_membership_preserved(self) -> None:
        # Scorer reorders only; it must never drop or add rows.
        rows = [
            _guard_row(target_kind="ExternalSymbol", target_urn="urn:external-symbol:x"),
            _moved_row(target_kind="CodeModule"),
            {"risk_type": "direct_call_contract_drift"},  # generic, no derivation tier
        ]
        for i, r in enumerate(rows):
            r["hypothesis_id"] = f"h{i}"
        out = apply_structural_noise_downranking(rows, changed_files=[], changed_symbols=[])
        self.assertEqual(len(out), 3)
        self.assertEqual({r["hypothesis_id"] for r in out}, {"h0", "h1", "h2"})


class TestRealPipelineNoisyRowRanksBelowClean(unittest.TestCase):
    """End-to-end through call_tool('review_context') on a real build_kg fixture pair.

    Proves a noisy deterministic row (a removed call whose target is a language BUILTIN,
    resolved as an ExternalSymbol entity) ranks BELOW a clean peer (a removed call whose
    target is a real intra-repo CodeSymbol) in the returned review_hypotheses — through the
    full splice → score → cap pipeline, not a unit stub. Names in the fixture are generic
    (alpha/beta/helper2); the rule keys on the KG entity kind, not the name.
    """

    def _build_pair(self, tmpdir):
        from pathlib import Path
        from source.kg.build.pipeline import build_kg

        svc = tmpdir / "svc"
        svc.mkdir()
        (svc / "__init__.py").write_text("", encoding="utf-8")
        # base: alpha calls a builtin (len) + kept(); beta calls intra-repo helper2() + kept2()
        (svc / "core.py").write_text(
            "def alpha(items):\n    n = len(items)\n    kept()\n    return n\n\n"
            "def beta():\n    helper2()\n    kept2()\n\n"
            "def helper2():\n    pass\n\ndef kept():\n    pass\n\ndef kept2():\n    pass\n",
            encoding="utf-8",
        )
        out_base = tmpdir / "kg_base"
        build_kg(svc, out_base, tenant_id="default")
        # head: alpha drops the builtin call (ExternalSymbol target removed);
        #       beta drops helper2() (CodeSymbol target removed).
        (svc / "core.py").write_text(
            "def alpha(items):\n    kept()\n    return 0\n\n"
            "def beta():\n    kept2()\n\n"
            "def helper2():\n    pass\n\ndef kept():\n    pass\n\ndef kept2():\n    pass\n",
            encoding="utf-8",
        )
        out_head = tmpdir / "kg_head"
        build_kg(svc, out_head, tenant_id="default")
        return out_base, out_head

    def test_builtin_target_row_ranks_below_codesymbol_target_row(self) -> None:
        import tempfile
        from pathlib import Path
        from source.kg.query.snapshot import KgSnapshot
        from source.kg.product.mcp_tools import call_tool

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out_base, out_head = self._build_pair(root)
            head_kg = KgSnapshot(out_head)
            # changed_ranges omitted deliberately: avoids the compact-unanchored path that
            # would strip hypotheses before we can inspect their order.
            result = call_tool(head_kg, "review_context", {
                "repo": "default",
                "changed_files": ["core.py"],
                "base_snapshot": str(out_base),
            })

        hyps = result.get("review_hypotheses") or []
        guard = [h for h in hyps if h.get("risk_type") == "guard_call_removed_drift"]
        # Hard assert: fixture must produce BOTH guard rows or the ordering claim is vacuous.
        self.assertEqual(
            len(guard), 2,
            f"fixture must yield exactly two guard rows; got {[h.get('concrete_invariant') for h in guard]}",
        )
        invariants = [str(h.get("concrete_invariant") or "") for h in guard]
        # The builtin target is an ExternalSymbol whose URN carries the external-symbol slug;
        # the CodeSymbol target does not. Identify each row structurally by that slug.
        builtin_idx = next(i for i, s in enumerate(invariants) if "external-symbol" in s)
        codesymbol_idx = next(i for i, s in enumerate(invariants) if "external-symbol" not in s)
        self.assertLess(
            codesymbol_idx, builtin_idx,
            f"clean CodeSymbol-target row must rank above builtin-target row; order={invariants}",
        )


if __name__ == "__main__":
    unittest.main()
