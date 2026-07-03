"""Tests for by-risk-type hypothesis counts (Part A) and semantic row budget survival (Part B).

Part A: review_hypothesis_status carries available_by_risk_type, returned_by_risk_type,
        truncated_by_risk_type — all consistent with scalar counts.
Part B: contract_semantic_diff (diff-derived) rows survive budget truncation even when
        placed at a low-priority position in the hypothesis list.
"""
from __future__ import annotations

import unittest
from copy import deepcopy

from source.kg.core.models import canonical_json
from source.kg.product.output_budget import (
    REVIEW_CONTEXT_MAX_CHARS,
    enforce_review_context_budget,
    _DIFF_DERIVED_RISK_TYPES,
)
from source.kg.product.review_attribution import add_review_lead_ids, review_available_counts


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
        # Build a packet that is over budget (compact profile at 15K) so some hypotheses
        # are evicted. Use many large hypotheses so truncation is forced.
        hyps = []
        for i in range(8):
            rt = "type_a" if i < 6 else "rare_type"
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
    """contract_semantic_diff rows survive budget truncation even at low list position."""

    def _make_over_budget_packet_with_semantic(self) -> dict:
        """Build a compact-profile packet where:
        - 5 large deterministic hypotheses fill positions 0-4 (each ~2KB)
        - 1 contract_semantic_diff hypothesis is at position 5 (low position)
        - Budget forces truncation to fewer than 6 hypotheses
        """
        # Each det hypothesis is padded to ~2KB so 5 together exceed the hypothesis_budget.
        det_hyps = []
        for i in range(5):
            h = _make_hyp("direct_call_contract_drift", i, derivation="deterministic_static")
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
        # Place semantic last — this is the low-priority position
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
        """contract_semantic_diff row at position 5 must survive when budget forces cuts."""
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

    # INVERSION PROOF: if _DIFF_DERIVED_RISK_TYPES is emptied or the pinning block in
    # _hypothesis_first_compact_packet is removed, the semantic row at position 5 will
    # be truncated by the budget fill loop (only prefix survives). The test
    # test_semantic_row_survives_budget_truncation would then fail because
    # returned_risk_types would not contain "contract_semantic_diff".

    def test_diff_derived_constant_includes_semantic_type(self):
        """Constant sanity: contract_semantic_diff is in _DIFF_DERIVED_RISK_TYPES."""
        self.assertIn("contract_semantic_diff", _DIFF_DERIVED_RISK_TYPES)

    def test_multiple_diff_derived_types_all_survive(self):
        """All diff-derived types present in original_hypotheses survive budget."""
        diff_types = ["contract_semantic_diff", "guard_call_removed_drift", "abstract_contract_unimplemented"]
        # 4 non-diff hypotheses (large) + 3 diff-derived at the end
        non_diff = [_make_hyp("generic_drift", i) for i in range(4)]
        diff_hyps = [_make_hyp(rt, 100 + i, derivation="deterministic_static") for i, rt in enumerate(diff_types)]
        hypotheses = non_diff + diff_hyps

        packet = _base_packet(hypotheses, extra_bulk=40)
        self.assertGreater(len(canonical_json(packet)), REVIEW_CONTEXT_MAX_CHARS)

        result = enforce_review_context_budget(packet)
        returned_hyps = result.get("review_hypotheses") or []
        returned_risk_types = {h.get("risk_type") for h in returned_hyps if isinstance(h, dict)}

        for rt in diff_types:
            self.assertIn(rt, returned_risk_types, f"diff-derived type {rt!r} was evicted")
        # Budget still respected.
        self.assertLessEqual(len(canonical_json(result)), REVIEW_CONTEXT_MAX_CHARS)
