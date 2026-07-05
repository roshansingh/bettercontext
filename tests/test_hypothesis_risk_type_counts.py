"""Tests for by-risk-type hypothesis counts (Part A), semantic row budget survival
(Part B), and the cap-time diff-family reservation (Part C).

Part A: review_hypothesis_status carries available_by_risk_type, returned_by_risk_type,
        truncated_by_risk_type — all consistent with scalar counts.
Part B: contract_semantic_diff (diff-derived) rows survive budget truncation even when
        placed at a low-priority position in the hypothesis list.
Part C: the top-level PLANNING_CONTEXT_SECTION_LIMIT cap reserves at least one row per
        GENERATED diff-derived risk type before slicing, so a generated diff family is
        never dropped before by-risk-type truncation counts run (cap-time twin of B).
"""
from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

import source.kg.product.mcp_tools as mcp_tools_module
import source.kg.product.output_budget as output_budget_module
from source.kg.build.pipeline import build_kg
from source.kg.core.models import canonical_json
from source.kg.product.mcp_tools import (
    PLANNING_CONTEXT_SECTION_LIMIT,
    _cap_review_hypotheses_reserving_diff_families,
    call_tool,
)
from source.kg.product.output_budget import (
    REVIEW_CONTEXT_MAX_CHARS,
    compute_hypothesis_seat_plan,
    enforce_review_context_budget,
    _DIFF_DERIVED_RISK_TYPES,
)
from source.kg.product.review_attribution import add_review_lead_ids, review_available_counts
from source.kg.query.snapshot import KgSnapshot


# ---------------------------------------------------------------------------
# Fixture helpers — mirror test_task_p_cluster_budget.py geometry exactly
# ---------------------------------------------------------------------------

def _make_symbol(lead_id: str, path: str, name: str) -> dict:
    return {
        "lead_id": lead_id,
        "lead_kind": "changed_symbol",
        "qualname": name,
        "name": name,
        "path": path,
        "repo": "repo-test",
        "line_start": 1,
        "line_end": 10,
    }


def _make_caller(lead_id: str, path: str, subject: str, obj: str) -> dict:
    return {
        "lead_id": lead_id,
        "lead_kind": "direct_caller",
        "subject": subject,
        "object": obj,
        "path": path,
        "repo": "repo-test",
        "line_start": 1,
        "line_end": 5,
    }


def _make_hyp(risk_type: str, idx: int, lead_ids: list[str] | None = None, derivation: str | None = None) -> dict:
    h: dict = {
        "hypothesis_id": f"hypothesis:{risk_type}:{idx:016x}",
        "risk_type": risk_type,
        "confidence": "high",
        "specificity": "high",
        "why": f"Risk {risk_type} idx {idx}. " + "Detail " * 20,
        "supporting_lead_ids": list(lead_ids or []),
        "source_checks": [{"reason": "check", "anchor": "src", "repo": "repo-test", "path": f"src/a{idx}.py"}],
        "evidence_refs": [{"repo": "repo-test", "path": f"src/a{idx}.py", "line_start": 1, "line_end": 5}],
    }
    if derivation is not None:
        h["derivation"] = derivation
    if risk_type == "contract_semantic_diff":
        h["verification"] = "verified"
    return h


def _base_packet(hypotheses: list[dict], extra_bulk: int = 0) -> dict:
    """Build a minimal well-formed review_context packet."""
    files = ["src/module_0.py", "src/module_1.py"]
    syms = [_make_symbol(f"lead:sym:{i}", files[i % 2], f"func_{i}") for i in range(4)]
    callers = [_make_caller(f"lead:caller:{i}", files[i % 2], f"caller_{i}", f"func_{i % 4}") for i in range(4)]

    leads_pre = {
        "changed_files": list(files),
        "changed_symbols": syms,
        "direct_callers": callers,
        "direct_callees": [],
        "transitive_callers": [],
        "source_coordinates": [],
    }
    leads = add_review_lead_ids(leads_pre)
    available = review_available_counts(
        changed_symbols=syms,
        direct_callers=callers,
        direct_callees=[],
        transitive_callers=[],
        source_coordinates=[],
    )
    lead_status = {
        "coverage_status": "useful",
        "recommended_action": "use_supercontext_packet",
        "changed_anchor_count": len(syms),
        "changed_symbol_count": len(syms),
        "direct_impact_count": len(callers),
        "transitive_impact_count": 0,
        "source_coordinate_count": 0,
        "file_anchor_count": len(files),
        "available": available,
        "returned": available,
    }
    # Bulk rows to push packet over REVIEW_CONTEXT_MAX_CHARS when extra_bulk > 0.
    bulk: list[dict] = []
    if extra_bulk:
        bulk = [{"subject": f"A", "object": f"B_{i}", "detail": "x" * 300, "path": files[i % 2]} for i in range(extra_bulk)]

    packet: dict = {
        "tool": "review_context",
        "status": "ok",
        "repo": "repo-test",
        "summary": {"symbol_anchor_count": len(syms), "file_anchor_count": len(files)},
        "review_leads": leads,
        "review_lead_status": lead_status,
        "review_hypotheses": deepcopy(hypotheses),
        "review_answer_packet": {
            "status": "ok",
            "top_changed_symbols": [],
            "top_review_hypotheses": deepcopy(hypotheses[:3]),
            "review_lead_status": lead_status,
        },
        "output_budget": {
            "truncated": False,
            "measured_chars": 0,
            "max_chars": REVIEW_CONTEXT_MAX_CHARS,
            "truncated_sections": [],
        },
    }
    if bulk:
        packet["application_impact"] = {"direct_callers": bulk}
    return packet


# ---------------------------------------------------------------------------
# Part A: available_by_risk_type / returned_by_risk_type / truncated_by_risk_type
# ---------------------------------------------------------------------------

class TestHypothesisRiskTypeCounts(unittest.TestCase):
    """review_hypothesis_status carries consistent by-risk-type count maps."""

    def test_counts_present_in_packet_under_budget(self):
        hyps = [_make_hyp("type_a", 1), _make_hyp("type_b", 2), _make_hyp("type_a", 3)]
        packet = _base_packet(hyps)
        result = enforce_review_context_budget(packet)

        hs = result.get("review_hypothesis_status")
        self.assertIsInstance(hs, dict, "review_hypothesis_status must be present")
        self.assertIn("available_by_risk_type", hs)
        self.assertIn("returned_by_risk_type", hs)
        self.assertIn("truncated_by_risk_type", hs)

    def test_available_by_risk_type_sums_to_available_count(self):
        hyps = [_make_hyp("type_a", 1), _make_hyp("type_b", 2), _make_hyp("type_c", 3)]
        packet = _base_packet(hyps)
        result = enforce_review_context_budget(packet)

        hs = result["review_hypothesis_status"]
        self.assertEqual(
            sum(hs["available_by_risk_type"].values()),
            hs["available_count"],
            "sum(available_by_risk_type) must equal available_count",
        )

    def test_returned_by_risk_type_sums_to_returned_count(self):
        hyps = [_make_hyp("type_a", 1), _make_hyp("type_b", 2), _make_hyp("type_c", 3)]
        packet = _base_packet(hyps)
        result = enforce_review_context_budget(packet)

        hs = result["review_hypothesis_status"]
        self.assertEqual(
            sum(hs["returned_by_risk_type"].values()),
            hs["returned_count"],
            "sum(returned_by_risk_type) must equal returned_count",
        )

    def test_truncated_by_risk_type_has_no_zero_entries(self):
        hyps = [_make_hyp("type_a", 1), _make_hyp("type_b", 2)]
        packet = _base_packet(hyps)
        result = enforce_review_context_budget(packet)

        hs = result["review_hypothesis_status"]
        for rt, count in hs["truncated_by_risk_type"].items():
            self.assertGreater(count, 0, f"truncated_by_risk_type[{rt!r}] must be > 0")

    def test_truncated_by_risk_type_arithmetic(self):
        """truncated[k] == available[k] - returned.get(k, 0) for all k in truncated."""
        hyps = [_make_hyp("type_a", 1), _make_hyp("type_b", 2), _make_hyp("type_c", 3)]
        packet = _base_packet(hyps)
        result = enforce_review_context_budget(packet)

        hs = result["review_hypothesis_status"]
        avail = hs["available_by_risk_type"]
        ret = hs["returned_by_risk_type"]
        trunc = hs["truncated_by_risk_type"]
        for rt, count in trunc.items():
            expected = avail.get(rt, 0) - ret.get(rt, 0)
            self.assertEqual(count, expected, f"truncated_by_risk_type[{rt!r}] arithmetic mismatch")

    def test_over_budget_truncated_type_appears_in_truncated_by_risk_type(self):
        """When truncation drops a risk_type entirely, it appears in truncated_by_risk_type."""
        # Build a packet that is over budget (compact profile at 15K). Use enough large
        # hypotheses that truncation is forced even after compact-before-evict shrinks each
        # row to its lean floor. All rows score 0, so eviction falls to list position: the
        # trailing rare_type family (last) is dropped, exercising the entire-type-dropped case.
        hyps = []
        for i in range(24):
            rt = "type_a" if i < 22 else "rare_type"
            h = _make_hyp(rt, i)
            h["why"] += " filler " * 200  # inflate each hypothesis
            hyps.append(h)
        packet = _base_packet(hyps, extra_bulk=30)

        self.assertGreater(
            len(canonical_json(packet)), REVIEW_CONTEXT_MAX_CHARS,
            "fixture must start over budget",
        )
        result = enforce_review_context_budget(packet)

        hs = result.get("review_hypothesis_status", {})
        avail = hs.get("available_by_risk_type", {})
        ret = hs.get("returned_by_risk_type", {})

        # Some truncation must have happened for this test to be meaningful.
        self.assertGreater(hs.get("truncated_count", 0), 0, "expected truncation to occur")

        trunc = hs.get("truncated_by_risk_type", {})
        # Every entry in truncated must be positive.
        for rt, count in trunc.items():
            self.assertGreater(count, 0)
        # Arithmetic consistency regardless of which rows survived.
        for rt, avail_count in avail.items():
            ret_count = ret.get(rt, 0)
            expected_trunc = avail_count - ret_count
            actual_trunc = trunc.get(rt, 0)
            self.assertEqual(
                actual_trunc,
                expected_trunc,
                f"truncated_by_risk_type[{rt!r}]: expected {expected_trunc}, got {actual_trunc}",
            )

    # INVERSION PROOF: the test above verifies truncated_by_risk_type[k] == avail[k] - ret[k].
    # If we corrupt the computation (e.g. set truncated_by_risk_type[k] = avail[k] instead of
    # avail[k] - ret[k]), the assertEqual on count==expected would catch it. Verified manually
    # by temporarily using `available_by_risk_type[rt]` in the body — test failed.


# ---------------------------------------------------------------------------
# Part B: diff-derived risk_type survival through budget truncation
# ---------------------------------------------------------------------------

class TestSemanticRowBudgetSurvival(unittest.TestCase):
    """A higher-scored contract_semantic_diff row survives budget truncation over lower-scored
    competitors — by SCORE, not by any diff-only carve-out. Its cause is a changed production
    file (+2 structural score); the competing rows have no such signal (score 0), so under the
    shared score-driven seat plan the semantic row seats and the lowest-scored competitors are
    the ones truncated."""

    def _make_over_budget_packet_with_semantic(self) -> dict:
        """Build a compact-profile packet where:
        - 20 large low-score deterministic hypotheses (no structural signal → score 0), each a
          distinct family, together far exceeding the hypothesis budget even fully compacted
        - 1 higher-scored contract_semantic_diff hypothesis at the LAST position (cause in a
          changed production file → +2)
        - Budget forces truncation; the +2 semantic row must survive while lowest-scored
          (0-score, deterministic-tier) det families are the ones dropped.
        """
        # Changed prod file present in _base_packet's changed_files → cause-in-changed-file boost.
        changed_prod = "src/module_0.py"
        # Each det hypothesis is padded so together they exceed the hypothesis_budget; none
        # carries a structural boost, so all score 0 (below the +2 semantic row).
        det_hyps = []
        for i in range(20):
            h = _make_hyp(f"direct_call_contract_drift_{i}", i, derivation="deterministic_static")
            h["why"] += (" Extended rationale with detailed analysis of the change. " * 30)
            h["source_checks"] = [
                {"reason": f"check_{j}", "anchor": f"src/a{i}.py", "repo": "repo-test", "path": f"src/a{i}.py"}
                for j in range(4)
            ]
            h["evidence_refs"] = [
                {"repo": "repo-test", "path": f"src/a{i}.py", "line_start": j * 10, "line_end": j * 10 + 5}
                for j in range(4)
            ]
            det_hyps.append(h)
        sem_hyp = _make_hyp("contract_semantic_diff", 99, derivation="inferred_llm")
        sem_hyp["why"] += " Semantic contract diff analysis. " * 10
        # +2 boost: cause path is a changed production file. This is why it wins by SCORE.
        sem_hyp["cause"] = {"repo": "repo-test", "path": changed_prod, "line_start": 1, "line_end": 4}
        # Place semantic last — a naive prefix fill or a low derivation tiebreak would drop it.
        hypotheses = det_hyps + [sem_hyp]

        # Create a packet that starts over the 15K compact limit.
        # Use extra bulk to push it over without broad context (compact profile).
        packet = _base_packet(hypotheses, extra_bulk=40)

        self.assertGreater(
            len(canonical_json(packet)), REVIEW_CONTEXT_MAX_CHARS,
            "fixture must start over budget",
        )
        return packet, hypotheses, sem_hyp

    def test_semantic_row_survives_budget_truncation(self):
        """Higher-scored contract_semantic_diff row at the last position survives budget cuts."""
        packet, _, sem_hyp = self._make_over_budget_packet_with_semantic()
        result = enforce_review_context_budget(packet)

        returned_hyps = result.get("review_hypotheses") or []
        returned_risk_types = {h.get("risk_type") for h in returned_hyps if isinstance(h, dict)}

        # The semantic row must be present — it's diff-derived, so pinning applies.
        self.assertIn(
            "contract_semantic_diff",
            returned_risk_types,
            f"contract_semantic_diff row was evicted; returned risk_types: {returned_risk_types}",
        )

    def test_semantic_row_survival_reflected_in_returned_by_risk_type(self):
        """returned_by_risk_type must show contract_semantic_diff >= 1 after budget."""
        packet, _, _ = self._make_over_budget_packet_with_semantic()
        result = enforce_review_context_budget(packet)

        hs = result.get("review_hypothesis_status", {})
        ret = hs.get("returned_by_risk_type", {})
        self.assertGreaterEqual(
            ret.get("contract_semantic_diff", 0), 1,
            "returned_by_risk_type[contract_semantic_diff] must be >= 1 when row survives",
        )

    def test_truncated_by_risk_type_not_counting_semantic_as_lost(self):
        """When semantic row survives, truncated_by_risk_type[contract_semantic_diff] must be 0."""
        packet, _, _ = self._make_over_budget_packet_with_semantic()
        result = enforce_review_context_budget(packet)

        hs = result.get("review_hypothesis_status", {})
        trunc = hs.get("truncated_by_risk_type", {})
        self.assertEqual(
            trunc.get("contract_semantic_diff", 0), 0,
            "contract_semantic_diff should not appear in truncated_by_risk_type when it survives",
        )

    def test_budget_still_respected_after_semantic_pinning(self):
        """Budget cap must not be exceeded after pinning the semantic row."""
        packet, _, _ = self._make_over_budget_packet_with_semantic()
        result = enforce_review_context_budget(packet)

        size = len(canonical_json(result))
        self.assertLessEqual(
            size, REVIEW_CONTEXT_MAX_CHARS,
            f"packet size {size} exceeds {REVIEW_CONTEXT_MAX_CHARS} after semantic row pinning",
        )

    def test_truncation_did_occur(self):
        """Sanity: fixture must have caused truncation (otherwise test proves nothing)."""
        packet, hypotheses, _ = self._make_over_budget_packet_with_semantic()
        result = enforce_review_context_budget(packet)

        returned_hyps = result.get("review_hypotheses") or []
        self.assertLess(
            len(returned_hyps), len(hypotheses),
            "expected truncation to drop at least one hypothesis",
        )

    # INVERSION PROOF: if the budget layer's eviction reverts to diff-first pinning (evicting
    # non-diff / lower-tier rows to keep diff families regardless of score), the +2 semantic
    # row would still survive but for the WRONG reason. The load-bearing inversion is the
    # field-replay regression below (test_field_replay_zero_score_diff_family_evicted): restore
    # diff-first pinning and it fails. Here, if score ceased to drive survival and the semantic
    # row lost its +2 boost (cause not in a changed file), it would be evicted first on the
    # derivation tiebreak — verified by removing sem_hyp["cause"]: the test then fails.

    def test_diff_derived_constant_includes_semantic_type(self):
        """Constant sanity: contract_semantic_diff is in _DIFF_DERIVED_RISK_TYPES."""
        self.assertIn("contract_semantic_diff", _DIFF_DERIVED_RISK_TYPES)

    def test_higher_scored_diff_types_survive_over_lower_scored_competitors(self):
        """Diff-derived families that outscore their competitors survive budget — by SCORE.

        Three diff-derived families carry a +2 boost (cause in a changed production file);
        four low-score generic families (score 0) are the ones truncated under pressure.
        """
        changed_prod = "src/module_0.py"  # present in _base_packet changed_files
        diff_types = ["contract_semantic_diff", "guard_call_removed_drift", "abstract_contract_unimplemented"]
        # 4 low-score generic hypotheses (large, score 0) + 3 higher-scored diff-derived rows.
        non_diff = [_make_hyp("generic_drift", i) for i in range(4)]
        for h in non_diff:
            h["why"] += (" filler rationale " * 60)  # inflate so pressure forces truncation
        diff_hyps = []
        for i, rt in enumerate(diff_types):
            h = _make_hyp(rt, 100 + i, derivation="deterministic_static")
            h["cause"] = {"repo": "repo-test", "path": changed_prod, "line_start": 1, "line_end": 4}  # +2
            diff_hyps.append(h)
        hypotheses = non_diff + diff_hyps

        packet = _base_packet(hypotheses, extra_bulk=40)
        self.assertGreater(len(canonical_json(packet)), REVIEW_CONTEXT_MAX_CHARS)

        result = enforce_review_context_budget(packet)
        returned_hyps = result.get("review_hypotheses") or []
        returned_risk_types = {h.get("risk_type") for h in returned_hyps if isinstance(h, dict)}

        for rt in diff_types:
            self.assertIn(rt, returned_risk_types, f"higher-scored diff family {rt!r} was evicted")
        # Budget still respected.
        self.assertLessEqual(len(canonical_json(result)), REVIEW_CONTEXT_MAX_CHARS)


# ---------------------------------------------------------------------------
# Part B1: compact-before-evict family floor holds under char pressure
# ---------------------------------------------------------------------------

def _big_diff_hyp(risk_type: str, idx: int) -> dict:
    """A diff-derived hypothesis row fat enough that four together overflow the
    hypothesis budget at 15K — but carrying trimmable optional fields (many
    source_checks / negative_checks / evidence_refs / long why) so compact-before-evict
    can shrink it rather than dropping the whole family."""
    h = _make_hyp(risk_type, idx, derivation="deterministic_static")
    h["postable_claim"] = f"Contract for {risk_type} changed at src/mod_{idx}.py. " * 3
    h["label"] = f"{risk_type}#{idx}"
    h["cause"] = {"repo": "repo-test", "path": f"src/mod_{idx}.py", "line_start": 1, "line_end": 4}
    h["consequence"] = {"repo": "repo-test", "path": f"src/mod_{idx}.py", "line_start": 5, "line_end": 9}
    h["why"] = f"Detailed rationale for {risk_type}. " + ("analysis " * 120)
    h["source_checks"] = [
        {"reason": f"inspect check {j}", "anchor": f"src/mod_{idx}.py", "repo": "repo-test",
         "path": f"src/mod_{idx}.py", "detail": "verify the invariant holds " * 8}
        for j in range(6)
    ]
    h["negative_checks"] = [
        {"reason": f"rule out {j}", "detail": "confirm this is not a false positive " * 8}
        for j in range(6)
    ]
    h["evidence_refs"] = [
        {"repo": "repo-test", "path": f"src/mod_{idx}.py", "line_start": j * 10, "line_end": j * 10 + 5}
        for j in range(6)
    ]
    return h


class TestCompactBeforeEvictFamilyFloor(unittest.TestCase):
    """B1: char pressure that would naively keep only 2 of 4 generated diff families must
    instead keep >=1 row per family by compacting rows before dropping any family."""

    def _four_family_over_budget_packet(self):
        diff_types = [
            "guard_call_removed_drift",
            "responsibility_moved_drift",
            "test_reference_removed_drift",
            "abstract_contract_unimplemented",
        ]
        hyps = [_big_diff_hyp(rt, 100 + i) for i, rt in enumerate(diff_types)]
        packet = _base_packet(hyps, extra_bulk=40)
        self.assertGreater(
            len(canonical_json(packet)), REVIEW_CONTEXT_MAX_CHARS,
            "fixture must start over the 15K compact budget",
        )
        return packet, diff_types, hyps

    def test_naive_fill_would_drop_families(self):
        """Inversion precondition: at the hypothesis budget, four full lean rows do NOT
        all fit — so a naive prefix fill keeps fewer than four families."""
        from source.kg.product.output_budget import _lean_review_hypothesis
        _packet, _diff_types, hyps = self._four_family_over_budget_packet()
        # Emulate the fill budget geometry: the compact profile funds hypotheses under a
        # budget well below 15K after reserving anchor/edge/profile headroom. Four full
        # lean rows must exceed a realistic hypothesis slice, proving naive fill truncates.
        lean_sizes = [len(canonical_json(_lean_review_hypothesis(h))) for h in hyps]
        self.assertGreater(
            sum(lean_sizes), REVIEW_CONTEXT_MAX_CHARS // 2,
            "fixture rows must be fat enough that naive fill cannot keep all four",
        )

    def test_all_four_diff_families_survive(self):
        """The floor: all four generated diff families return >=1 row post-budget."""
        packet, diff_types, _ = self._four_family_over_budget_packet()
        result = enforce_review_context_budget(packet)
        returned_hyps = result.get("review_hypotheses") or []
        returned_types = {h.get("risk_type") for h in returned_hyps if isinstance(h, dict)}
        for rt in diff_types:
            self.assertIn(
                rt, returned_types,
                f"diff family {rt!r} was evicted under char pressure; returned={sorted(returned_types)}",
            )

    def test_budget_respected_after_compaction(self):
        """Compact-before-evict must not push the packet over the cap."""
        packet, _, _ = self._four_family_over_budget_packet()
        result = enforce_review_context_budget(packet)
        size = len(canonical_json(result))
        self.assertLessEqual(size, REVIEW_CONTEXT_MAX_CHARS, f"packet {size} exceeds cap")

    def test_load_bearing_fields_preserved_after_compaction(self):
        """Compaction trims optional fields only — load-bearing fields survive on every row."""
        packet, _, _ = self._four_family_over_budget_packet()
        result = enforce_review_context_budget(packet)
        for h in result.get("review_hypotheses") or []:
            self.assertIn("risk_type", h)
            self.assertIn("hypothesis_id", h)
            self.assertIn("postable_claim", h)
            self.assertIn("cause", h)
            self.assertIn("consequence", h)

    def test_dropped_family_would_show_in_truncated_by_risk_type(self):
        """If a family truly cannot fit even fully compacted, its absence is recorded in
        truncated_by_risk_type — never silent. Here all fit, so it is zero, but the
        arithmetic invariant (available - returned) must still hold per family."""
        packet, _, _ = self._four_family_over_budget_packet()
        result = enforce_review_context_budget(packet)
        hs = result.get("review_hypothesis_status", {})
        avail = hs.get("available_by_risk_type", {})
        ret = hs.get("returned_by_risk_type", {})
        trunc = hs.get("truncated_by_risk_type", {})
        for rt, a in avail.items():
            self.assertEqual(trunc.get(rt, 0), a - ret.get(rt, 0),
                             f"truncated_by_risk_type[{rt!r}] must equal available-returned")

    # INVERSION PROOF: if _compact_hypothesis_rows_to_fit is disabled (return False
    # immediately), the four fat rows cannot all fit and the naive fill + all-or-nothing
    # pin path drops at least one family — test_all_four_diff_families_survive would fail
    # because returned_types would omit a diff family. Verified by temporarily making
    # _compact_hypothesis_rows_to_fit a no-op.


# ---------------------------------------------------------------------------
# Part B2: budget-time score-ordered eviction (field-replay regression)
# ---------------------------------------------------------------------------

_FIELD_REPLAY_CHANGED_FILES = ["src/module_0.py", "src/module_1.py"]
_FIELD_REPLAY_CHANGED_PROD = "src/module_0.py"


def _score_shaped_hyp(risk_type: str, derivation: str | None, *, cause=None, consequence=None) -> dict:
    """A hypothesis row whose structural score is controlled by its cause/consequence paths.

    postable_claim is inflated to a size that survives compaction (it is a load-bearing
    field), so each row has a large irreducible floor — the budget can hold only two.
    """
    idx = abs(hash(risk_type)) % 100000
    h = _make_hyp(risk_type, idx, derivation=derivation)
    if derivation is None:
        h.pop("derivation", None)
    if cause is not None:
        h["cause"] = cause
    if consequence is not None:
        h["consequence"] = consequence
    h["postable_claim"] = "claim word " * 350
    return h


class TestBudgetScoreOrderedEviction(unittest.TestCase):
    """Field-replay regression: under char pressure the budget layer evicts the LOWEST-scored
    family first — diff-derived or not — reversing the shared seat plan the cap seated by.

    Geometry (engine f1c93a0 field packet): families scoring
      {+2 inferred_llm (contract_semantic_diff, DIFF), +2 non-diff, +2 non-diff,
       0 deterministic-diff, -2 deterministic-diff}
    with a char budget that fits only 2 rows after compaction. Correct result: the +2 inferred
    row and the FIRST +2 non-diff row survive; the 0-score and -2 diff families are evicted and
    visible in truncated_by_risk_type. The prior split policy (budget-time diff-first pinning)
    kept the 0-score diff family and dropped a +2 non-diff row — the exact bug this fixes.
    """

    _PROD_CAUSE_1 = {"repo": "repo-test", "path": _FIELD_REPLAY_CHANGED_PROD, "line_start": 1, "line_end": 2}
    _PROD_CAUSE_2 = {"repo": "repo-test", "path": _FIELD_REPLAY_CHANGED_PROD, "line_start": 3, "line_end": 4}
    _PROD_CAUSE_3 = {"repo": "repo-test", "path": _FIELD_REPLAY_CHANGED_PROD, "line_start": 5, "line_end": 6}
    _TEST_CAUSE = {"repo": "repo-test", "path": "tests/t.py", "line_start": 1, "line_end": 2}
    _TEST_CONS = {"repo": "repo-test", "path": "tests/u.py", "line_start": 1, "line_end": 2}

    def _field_replay_packet(self):
        sem = _score_shaped_hyp("contract_semantic_diff", "inferred_llm", cause=self._PROD_CAUSE_1)  # +2 DIFF
        async_row = _score_shaped_hyp("async_side_effect_lifecycle_drift", None, cause=self._PROD_CAUSE_2)  # +2 non-diff
        destr = _score_shaped_hyp("destructive_mutation_test_gap", None, cause=self._PROD_CAUSE_3)  # +2 non-diff
        moved = _score_shaped_hyp("responsibility_moved_drift", "deterministic_static")  # 0 DIFF
        guard = _score_shaped_hyp(
            "guard_call_removed_drift", "deterministic_static", cause=self._TEST_CAUSE, consequence=self._TEST_CONS
        )  # -2 DIFF
        rows = [sem, async_row, destr, moved, guard]
        # Thread the shared plan exactly as the producer does — scored over the full changed set.
        plan = compute_hypothesis_seat_plan(
            rows, changed_files=_FIELD_REPLAY_CHANGED_FILES, changed_symbols=[]
        )
        packet = _base_packet(rows, extra_bulk=40)
        packet["_hypothesis_seat_plan"] = plan
        self.assertGreater(len(canonical_json(packet)), REVIEW_CONTEXT_MAX_CHARS, "fixture must start over budget")
        return packet, rows

    def test_score_ordered_eviction_keeps_top_two_by_score(self):
        packet, _rows = self._field_replay_packet()
        result = enforce_review_context_budget(packet)
        returned = [h.get("risk_type") for h in (result.get("review_hypotheses") or [])]

        # Only two rows fit; they must be the two highest-scored families.
        self.assertEqual(len(returned), 2, f"budget must hold exactly 2 rows; got {returned}")
        self.assertIn("contract_semantic_diff", returned, "the +2 inferred_llm diff row must survive")
        self.assertIn(
            "async_side_effect_lifecycle_drift", returned,
            f"the first +2 non-diff row must survive over a 0-score diff family; got {returned}",
        )
        # The 0-score diff family is EVICTED — it does not outlive a +2 non-diff row anymore.
        self.assertNotIn(
            "responsibility_moved_drift", returned,
            f"0-score diff family must be evicted, not pinned; got {returned}",
        )

    def test_evicted_families_visible_in_truncated_by_risk_type(self):
        packet, _rows = self._field_replay_packet()
        result = enforce_review_context_budget(packet)
        trunc = result.get("review_hypothesis_status", {}).get("truncated_by_risk_type", {})
        # Evicted families are recorded — never silent absence.
        self.assertEqual(trunc.get("responsibility_moved_drift"), 1, f"0-score diff family must be recorded; {trunc}")
        self.assertEqual(trunc.get("guard_call_removed_drift"), 1, f"-2 diff family must be recorded; {trunc}")

    def test_budget_respected(self):
        packet, _rows = self._field_replay_packet()
        result = enforce_review_context_budget(packet)
        size = len(canonical_json(result))
        self.assertLessEqual(size, REVIEW_CONTEXT_MAX_CHARS, f"packet {size} exceeds cap")

    def test_inversion_diff_first_eviction_would_keep_the_zero_score_diff_family(self):
        """INVERSION PROOF: restore diff-first pinning (evict non-diff before diff) and the
        0-score diff family survives while a +2 non-diff row is dropped — the old bug. We
        emulate that by patching the eviction selector to prefer evicting non-diff rows, and
        assert the WRONG result appears, proving the score-ordered selector is load-bearing.
        """
        from source.kg.product.output_budget import _DIFF_DERIVED_RISK_TYPES

        def _diff_first_evict(hypotheses, keep_rank, *, protected):
            # Old policy: drop the last non-diff row (never a diff family) — ignore score.
            for i in range(len(hypotheses) - 1, -1, -1):
                if id(hypotheses[i]) in protected:
                    continue
                if hypotheses[i].get("risk_type") not in _DIFF_DERIVED_RISK_TYPES:
                    return i
            # Fall back to the last non-protected row if only diff rows remain.
            for i in range(len(hypotheses) - 1, -1, -1):
                if id(hypotheses[i]) not in protected:
                    return i
            return None

        packet, _rows = self._field_replay_packet()
        with mock.patch.object(
            output_budget_module, "_lowest_scored_hypothesis_index", _diff_first_evict
        ):
            result = enforce_review_context_budget(packet)
        returned = {h.get("risk_type") for h in (result.get("review_hypotheses") or [])}
        # Under the restored diff-first policy the 0-score diff family survives and a +2 non-diff
        # family is the one dropped — demonstrating the regression the real selector prevents.
        self.assertIn(
            "responsibility_moved_drift", returned,
            f"inversion: diff-first pinning keeps the 0-score diff family; got {returned}",
        )
        self.assertNotIn(
            "async_side_effect_lifecycle_drift", returned,
            f"inversion: diff-first pinning drops a +2 non-diff row; got {returned}",
        )


# ---------------------------------------------------------------------------
# Part C: cap-time diff-family reservation (before PLANNING_CONTEXT_SECTION_LIMIT)
# ---------------------------------------------------------------------------

def _cap_hyp(risk_type: str, idx: int, derivation: str | None) -> dict:
    h: dict = {
        "hypothesis_id": f"hypothesis:{risk_type}:{idx:016x}",
        "risk_type": risk_type,
    }
    if derivation is not None:
        h["derivation"] = derivation
    if risk_type == "contract_semantic_diff":
        h["verification"] = "verified"
    return h


class TestCapTimeDiffFamilyReservation(unittest.TestCase):
    """_cap_review_hypotheses_reserving_diff_families keeps >=1 row per generated
    diff-derived family in the first PLANNING_CONTEXT_SECTION_LIMIT slots."""

    def test_deterministic_and_semantic_families_survive_naive_slice(self):
        """3 contract-diff + 1 abstract (deterministic) + 1 semantic + generic rows,
        all generated, total > cap → every generated diff-derived family survives."""
        # Generic rows occupy the front (as generic families rank last in the real list
        # only after splices, but here we place them first to prove reservation evicts
        # them rather than the diff families they would otherwise crowd out).
        generic = [_cap_hyp("generic_drift", i, None) for i in range(5)]
        det_diff = [
            _cap_hyp("guard_call_removed_drift", 10, "deterministic_static"),
            _cap_hyp("responsibility_moved_drift", 11, "deterministic_static"),
            _cap_hyp("test_reference_removed_drift", 12, "deterministic_static"),
            _cap_hyp("abstract_contract_unimplemented", 13, "deterministic_static"),
        ]
        semantic = [_cap_hyp("contract_semantic_diff", 20, "inferred_llm")]
        # Deterministic front-ranked (mirrors the real splice), then semantic, then generic.
        ordered = det_diff + semantic + generic
        kept = _cap_review_hypotheses_reserving_diff_families(ordered, PLANNING_CONTEXT_SECTION_LIMIT)

        self.assertEqual(len(kept), PLANNING_CONTEXT_SECTION_LIMIT)
        kept_types = {h["risk_type"] for h in kept}
        for rt in (
            "guard_call_removed_drift",
            "responsibility_moved_drift",
            "test_reference_removed_drift",
            "abstract_contract_unimplemented",
        ):
            self.assertIn(rt, kept_types, f"deterministic diff family {rt!r} dropped by cap")

    def test_inversion_naive_slice_drops_a_family(self):
        """Inversion: the naive [:cap] slice DOES drop a generated diff family the
        reservation cap preserves — proves the reservation is load-bearing."""
        det_diff = [
            _cap_hyp("guard_call_removed_drift", 10, "deterministic_static"),
            _cap_hyp("responsibility_moved_drift", 11, "deterministic_static"),
        ]
        semantic = [_cap_hyp("contract_semantic_diff", 20, "inferred_llm")]
        generic = [_cap_hyp("generic_drift", i, None) for i in range(5)]
        # Semantic sits at position 3, but generic rows crowd it past the cap in a naive slice.
        ordered = det_diff + generic[:3] + semantic + generic[3:]
        naive = {h["risk_type"] for h in ordered[:PLANNING_CONTEXT_SECTION_LIMIT]}
        self.assertNotIn(
            "contract_semantic_diff", naive,
            "inversion precondition: naive slice must drop the semantic family",
        )
        kept = _cap_review_hypotheses_reserving_diff_families(ordered, PLANNING_CONTEXT_SECTION_LIMIT)
        kept_types = {h["risk_type"] for h in kept}
        self.assertIn(
            "contract_semantic_diff", kept_types,
            "reservation cap must retain the semantic family the naive slice dropped",
        )

    def test_deterministic_wins_scarce_slot_over_semantic(self):
        """When distinct diff families exceed a tight cap, deterministic families are
        kept and the inferred_llm family is the one dropped (derivation tier)."""
        # 4 distinct deterministic diff families + 1 semantic = 5 distinct diff types,
        # capped to 4 slots → the inferred_llm family must be the one left out.
        det_diff = [
            _cap_hyp("guard_call_removed_drift", 10, "deterministic_static"),
            _cap_hyp("responsibility_moved_drift", 11, "deterministic_static"),
            _cap_hyp("test_reference_removed_drift", 12, "deterministic_static"),
            _cap_hyp("abstract_contract_unimplemented", 13, "deterministic_static"),
        ]
        semantic = [_cap_hyp("contract_semantic_diff", 20, "inferred_llm")]
        ordered = det_diff + semantic
        kept = _cap_review_hypotheses_reserving_diff_families(ordered, 4)
        kept_types = {h["risk_type"] for h in kept}
        self.assertEqual(len(kept), 4)
        for rt in (
            "guard_call_removed_drift",
            "responsibility_moved_drift",
            "test_reference_removed_drift",
            "abstract_contract_unimplemented",
        ):
            self.assertIn(rt, kept_types, f"deterministic family {rt!r} must win scarce slot")
        self.assertNotIn(
            "contract_semantic_diff", kept_types,
            "inferred_llm family must be the one dropped when slots are scarce",
        )


TENANT = "default"


def _build_combined_pair(tmpdir: Path) -> tuple[Path, Path, Path, Path]:
    """base + head where core.py has BOTH contract-diff churn (guard/moved/test-ref)
    AND a class reparented onto an abstract base with unimplemented members.

    Base and head live in SEPARATE checkout dirs (the abstract/semantic detectors diff
    the two checkouts on disk, so an in-place overwrite would produce an empty diff).
    Yields up to 4 generated diff-derived families (3 contract + 1 abstract) that all
    exceed the top-level cap once generic caller hypotheses are added.
    """
    abstract_base = (
        "import abc\n\n\n"
        "class BaseThing(abc.ABC):\n"
        "    @abc.abstractmethod\n"
        "    def build_it(self, x):\n"
        "        ...\n\n\n"
        "class Concrete:\n"
        "    def evaluate(self):\n"
        "        return 1\n\n\n"
    )
    # Heavy churn: multiple guard-call removals + responsibility moves so guard/moved
    # families each produce >1 row — enough total diff-derived rows that a naive [:cap]
    # slice would drop entire later families (test-ref, abstract, semantic).
    base_core = abstract_base + (
        "def alpha():\n    beta()\n    gamma()\n\n"
        "def beta():\n    delta()\n    gamma()\n\n"
        "def gamma():\n    pass\n\n"
        "def delta():\n    pass\n\n"
        "def eps():\n    gamma()\n\n"
        "class Widget(Concrete):\n"
        "    def evaluate(self):\n"
        "        return 2\n"
    )
    head_core = abstract_base + (
        "def alpha():\n    beta()\n\n"
        "def beta():\n    pass\n\n"
        "def gamma():\n    pass\n\n"
        "def delta():\n    gamma()\n\n"
        "def eps():\n    pass\n\n"
        "class Widget(BaseThing):\n"
        "    pass\n"
    )

    def _write_repo(root: Path, core: str, test_body: str) -> None:
        svc = root / "svc"
        (svc / "tests").mkdir(parents=True)
        (svc / "__init__.py").write_text("", encoding="utf-8")
        (svc / "core.py").write_text(core, encoding="utf-8")
        (svc / "tests" / "__init__.py").write_text("", encoding="utf-8")
        (svc / "tests" / "test_core.py").write_text(test_body, encoding="utf-8")

    base_ck = tmpdir / "base"
    head_ck = tmpdir / "head"
    _write_repo(base_ck, base_core, "from svc.core import alpha\n\ndef test_alpha():\n    alpha()\n")
    _write_repo(head_ck, head_core, "def test_other():\n    pass\n")

    out_base = tmpdir / "kg_base"
    out_head = tmpdir / "kg_head"
    build_kg(base_ck / "svc", out_base, tenant_id=TENANT)
    build_kg(head_ck / "svc", out_head, tenant_id=TENANT)
    return out_base, out_head, base_ck / "svc", head_ck / "svc"


class TestCapReservationRealPipeline(unittest.TestCase):
    """Real call_tool('review_context') pipeline: heavy contract-diff churn + abstract
    reparent generate more diff-derived rows than the cap, spanning multiple distinct
    risk types. The reservation cap keeps >=1 row per generated diff-derived type; a naive
    slice would drop entire later families. Inversion is proved on the real pre-cap list."""

    def _run_capturing_precap(self):
        """Run the real pipeline; return (result, pre_cap_types, naive_types)."""
        captured: dict = {}
        original = mcp_tools_module._cap_review_hypotheses_reserving_diff_families

        def _spy(rows, limit, **kwargs):
            captured["pre"] = [r.get("risk_type") for r in rows if isinstance(r, dict)]
            captured["naive"] = [
                r.get("risk_type") for r in rows[:limit] if isinstance(r, dict)
            ]
            return original(rows, limit, **kwargs)

        with tempfile.TemporaryDirectory() as td:
            tmpdir = Path(td)
            out_base, out_head, base_ck, head_ck = _build_combined_pair(tmpdir)
            head_kg = KgSnapshot(out_head)
            with mock.patch.object(
                mcp_tools_module,
                "_cap_review_hypotheses_reserving_diff_families",
                _spy,
            ):
                result = call_tool(
                    head_kg,
                    "review_context",
                    {
                        "repo": "svc",
                        "changed_files": ["core.py"],
                        "base_snapshot": str(out_base),
                        "base_checkout": str(base_ck),
                        "head_checkout": str(head_ck),
                    },
                )
        return result, captured["pre"], captured["naive"]

    def test_every_generated_diff_family_survives_the_cap(self):
        result, pre_types, naive_types = self._run_capturing_precap()

        # Hard precondition: the fixture must over-produce (more diff-derived rows than
        # the cap) across multiple distinct types, or the test proves nothing.
        pre_diff = [rt for rt in pre_types if rt in _DIFF_DERIVED_RISK_TYPES]
        self.assertGreater(
            len(pre_diff), PLANNING_CONTEXT_SECTION_LIMIT,
            f"fixture must generate more diff-derived rows than the cap; pre={pre_types}",
        )
        pre_diff_type_set = set(pre_diff)
        self.assertGreaterEqual(
            len(pre_diff_type_set), 4,
            f"fixture must span >=4 distinct diff-derived types; got {pre_diff_type_set}",
        )

        # Inversion precondition: the naive [:cap] slice DROPS at least one generated
        # diff-derived type entirely — the exact regression this fix prevents.
        naive_type_set = {rt for rt in naive_types if rt in _DIFF_DERIVED_RISK_TYPES}
        dropped_by_naive = pre_diff_type_set - naive_type_set
        self.assertTrue(
            dropped_by_naive,
            f"inversion precondition failed: naive slice kept all diff types "
            f"(pre={pre_diff_type_set}, naive={naive_type_set})",
        )

        # The fix: every generated diff-derived type has a RETURNED row in the FINAL
        # packet — none silently absent, including those the naive slice would drop.
        returned_hyps = result.get("review_hypotheses") or []
        returned_types = {
            h.get("risk_type") for h in returned_hyps if isinstance(h, dict)
        }
        for rt in pre_diff_type_set:
            self.assertIn(
                rt, returned_types,
                f"generated diff family {rt!r} silently dropped by the cap; "
                f"returned={sorted(returned_types)}",
            )

        # by-risk-type status reflects the survival (no silent absence).
        hs = result.get("review_hypothesis_status") or {}
        returned_by = hs.get("returned_by_risk_type") or {}
        for rt in pre_diff_type_set:
            self.assertGreaterEqual(
                returned_by.get(rt, 0), 1,
                f"returned_by_risk_type[{rt!r}] must be >=1; got {returned_by}",
            )

    def test_available_counts_reflect_precap_generated_not_capped(self):
        """P2: available_count / available_by_risk_type reflect the PRE-CAP generated set;
        returned_by_risk_type reflects the final packet; truncated = available - returned
        (including cap-time drops). This is the exact regression the producer-side counts fix.
        """
        result, pre_types, _naive_types = self._run_capturing_precap()

        # The producer over-generated beyond the cap — precondition for the fix to matter.
        self.assertGreater(
            len(pre_types), PLANNING_CONTEXT_SECTION_LIMIT,
            f"fixture must generate more rows than the cap; pre={pre_types}",
        )
        expected_available_by_type: dict[str, int] = {}
        for rt in pre_types:
            if rt:
                expected_available_by_type[rt] = expected_available_by_type.get(rt, 0) + 1

        hs = result.get("review_hypothesis_status") or {}
        avail = hs.get("available_by_risk_type") or {}
        ret = hs.get("returned_by_risk_type") or {}
        trunc = hs.get("truncated_by_risk_type") or {}

        # available_count is the pre-cap generated total, NOT the post-cap survivors.
        self.assertEqual(
            hs.get("available_count"), len(pre_types),
            f"available_count must equal pre-cap generated total {len(pre_types)}; got {hs.get('available_count')}",
        )
        self.assertGreater(
            hs.get("available_count", 0), hs.get("returned_count", 0),
            "available_count (pre-cap) must exceed returned_count when the cap dropped rows",
        )
        # available_by_risk_type matches the PRE-CAP by-type counts exactly.
        self.assertEqual(
            avail, expected_available_by_type,
            "available_by_risk_type must reflect the PRE-CAP generated set",
        )
        # returned reflects the FINAL packet.
        final_types = [
            h.get("risk_type") for h in (result.get("review_hypotheses") or [])
            if isinstance(h, dict) and h.get("risk_type")
        ]
        expected_returned: dict[str, int] = {}
        for rt in final_types:
            expected_returned[rt] = expected_returned.get(rt, 0) + 1
        self.assertEqual(ret, expected_returned, "returned_by_risk_type must reflect the final packet")
        # truncated = available - returned per type, and includes at least one CAP-time drop.
        for rt, avail_count in avail.items():
            expected_trunc = avail_count - ret.get(rt, 0)
            self.assertEqual(
                trunc.get(rt, 0), expected_trunc,
                f"truncated_by_risk_type[{rt!r}] must be available - returned",
            )
        self.assertGreater(
            sum(trunc.values()), 0,
            "cap dropped rows, so truncated_by_risk_type must report the difference",
        )

    # INVERSION PROOF: if the producer counts are removed (available computed from the
    # CAPPED original_hypotheses as before), available_count would equal returned_count and
    # available_by_risk_type would omit the cap-dropped families — this test's
    # assertGreater(available_count, returned_count) and the available_by_risk_type equality
    # against the pre-cap set would both fail. Verified by temporarily passing
    # generated_counts=None in _finalize_review_hypothesis_budget.


# ---------------------------------------------------------------------------
# Part D: score-driven seat allocation (field-replay regressions)
# ---------------------------------------------------------------------------
#
# The seat policy (plan_hypothesis_seats) allocates slots to families by their best row's
# STRUCTURAL score, not by derivation tier. Derivation is a tiebreak only on equal scores.
# These two regressions reproduce the geometry of two real field-replay failures the old
# derivation-first policy handled wrongly:
#   * Sentry: a test-helper (test+test cause/consequence) deterministic family outranked a
#     production-file inferred family purely because deterministic > inferred.
#   * Cal.com: a high-value non-diff production row lost its slot to noisy diff families
#     (external/module-root call targets) because diff families had a reserved carve-out.
# Names below are generic; the rules key on structural signals only.

class TestScoreDrivenSeatAllocation(unittest.TestCase):
    def test_sentry_geometry_low_score_deterministic_family_truncated(self):
        """3 slots: high-score deterministic (concrete failure mode, +3), low-score
        deterministic (test+test cause/consequence, -2), mid-score inferred_llm (changed
        production-file cause, +2), plus a neutral next-best family (0). The low-score
        deterministic family must be the one truncated — its derivation tier does NOT save
        it under the score-driven policy."""
        high_det = {
            "risk_type": "abstract_contract_unimplemented",
            "derivation": "deterministic_static",
            "unimplemented_members": ["do_thing"],  # concrete failure mode → +3
        }
        low_det = {
            "risk_type": "responsibility_moved_drift",
            "derivation": "deterministic_static",
            "cause": {"path": "tests/test_mod.py", "line_start": 1},
            "consequence": {"path": "tests/test_other.py", "line_start": 2},  # both test → -2
        }
        mid_inferred = {
            "risk_type": "contract_semantic_diff",
            "derivation": "inferred_llm",
            "verification": "verified",
            "cause": {"path": "src/core.py", "line_start": 3},  # changed prod file → +2
        }
        next_best = {  # neutral non-diff family, score 0
            "risk_type": "swallowed_exception_state_drift",
            "derivation": None,
        }
        # low_det placed FIRST so a naive slice / derivation-first order would keep it.
        ordered = [low_det, high_det, mid_inferred, next_best]
        changed_files = ["src/core.py"]

        # Inversion precondition: a naive [:3] slice keeps the low-score deterministic family
        # and drops the neutral next-best row.
        naive = {r["risk_type"] for r in ordered[:3]}
        self.assertIn("responsibility_moved_drift", naive)
        self.assertNotIn("swallowed_exception_state_drift", naive)

        kept = _cap_review_hypotheses_reserving_diff_families(
            ordered, 3, changed_files=changed_files
        )
        kept_types = {r["risk_type"] for r in kept}
        self.assertEqual(len(kept), 3)
        self.assertIn("abstract_contract_unimplemented", kept_types, "high-score det must seat")
        self.assertIn("contract_semantic_diff", kept_types, "mid-score inferred must seat")
        self.assertIn("swallowed_exception_state_drift", kept_types, "neutral next-best must seat")
        self.assertNotIn(
            "responsibility_moved_drift", kept_types,
            f"low-score deterministic family must be truncated; kept={kept_types}",
        )

    def test_calcom_geometry_noisy_diff_family_truncated(self):
        """3 slots: two noisy diff families (external-target guard, -3; module-root moved,
        -2), a high-score non-diff production row (+2), and a semantic row with a changed
        production-file cause (+2). The non-diff row AND the semantic row must BOTH seat;
        the noisiest diff family (external target) is truncated."""
        external_target = {  # removed call into a language builtin/external → -3
            "risk_type": "guard_call_removed_drift",
            "derivation": "deterministic_static",
            "target_entity_kind": "ExternalSymbol",
            "target_urn": "urn:external-symbol:x",
        }
        module_root = {  # moved call whose target is a bare module root → -2
            "risk_type": "responsibility_moved_drift",
            "derivation": "deterministic_static",
            "target_entity_kind": "CodeModule",
        }
        non_diff_prod = {  # non-diff row, changed production-file cause → +2
            "risk_type": "swallowed_exception_state_drift",
            "derivation": None,
            "cause": {"path": "src/core.py", "line_start": 4},
        }
        semantic = {  # inferred_llm diff row, changed production-file cause → +2
            "risk_type": "contract_semantic_diff",
            "derivation": "inferred_llm",
            "verification": "verified",
            "cause": {"path": "src/core.py", "line_start": 9},
        }
        # Noisy diff families FIRST so a derivation-first / carve-out policy keeps them.
        ordered = [external_target, module_root, non_diff_prod, semantic]
        changed_files = ["src/core.py"]

        # Inversion precondition: a naive [:3] slice keeps both noisy diff families and drops
        # the semantic row.
        naive = {r["risk_type"] for r in ordered[:3]}
        self.assertIn("guard_call_removed_drift", naive)
        self.assertNotIn("contract_semantic_diff", naive)

        kept = _cap_review_hypotheses_reserving_diff_families(
            ordered, 3, changed_files=changed_files
        )
        kept_types = {r["risk_type"] for r in kept}
        self.assertEqual(len(kept), 3)
        self.assertIn(
            "swallowed_exception_state_drift", kept_types,
            f"high-score non-diff row must seat; kept={kept_types}",
        )
        self.assertIn(
            "contract_semantic_diff", kept_types,
            f"semantic row must seat; kept={kept_types}",
        )
        self.assertNotIn(
            "guard_call_removed_drift", kept_types,
            f"noisiest diff family (external target) must be truncated; kept={kept_types}",
        )
