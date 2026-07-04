"""Score-driven seat allocation: a HIGH-VALUE non-diff-derived hypothesis wins a seat on
the same structural-score basis as diff families, so diff-derived families can never crowd
a high-signal non-diff row out purely by their derivation tier.

Real-pipeline test through call_tool("review_context"): the fixture over-produces 5+
diff-derived families across multiple risk types. The base (non-splice) hypothesis
generator is stubbed to return ONE real-shaped non-diff-derived row whose cause is a
CHANGED PRODUCTION file (a positive structural score) — a legitimate seam: the stub
controls only the producer's base input, never the cap/budget logic under test — so the
packet has 5+ diff families PLUS 1 high-score non-diff row. The non-diff row must survive
the cap AND the budget by outscoring the noisier diff families.

Inversion proof: a naive [:limit] slice drops the trailing non-diff row; the score-driven
seat policy keeps it because its structural score beats the zero-score diff families it
sits behind.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import source.kg.product.mcp_tools as mcp_tools_module
from source.kg.product.mcp_tools import (
    PLANNING_CONTEXT_SECTION_LIMIT,
    _cap_review_hypotheses_reserving_diff_families,
    call_tool,
)
from source.kg.product.output_budget import (
    REVIEW_CONTEXT_MAX_CHARS,
    _DIFF_DERIVED_RISK_TYPES,
    enforce_review_context_budget,
)
from source.kg.query.snapshot import KgSnapshot

from tests.test_hypothesis_risk_type_counts import _base_packet, _build_combined_pair, _make_hyp

_NON_DIFF_RISK_TYPE = "swallowed_exception_state_drift"


def _non_diff_hypothesis() -> dict:
    """A real-shaped non-diff-derived hypothesis row (mirrors _make_hypothesis output)."""
    return {
        "hypothesis_id": "hypothesis:swallowed_exception_state_drift:00000000deadbeef",
        "label": "H-swallowed_exception_state_drift",
        "risk_type": _NON_DIFF_RISK_TYPE,
        "confidence": "medium",
        "specificity": "high",
        "why": "A changed symbol swallows a broad exception whose silent outcome may mask a "
        "state transition callers depend on.",
        "postable_claim": "svc.core.alpha swallows a broad exception; the silent outcome may "
        "mask a state transition callers depend on.",
        "cause": {"repo": "svc", "path": "core.py", "line_start": 20, "line_end": 24},
        "consequence": {"repo": "svc", "path": "core.py", "line_start": 20, "line_end": 24},
        "source_checks": ["Trace each flagged broad-exception handler."],
        "negative_checks": ["If the handler is intentional and documented, it does not apply."],
        "evidence_refs": [{"repo": "svc", "path": "core.py", "line_start": 20, "line_end": 24}],
        "supporting_lead_ids": [],
    }


class TestNonDiffSeatRealPipeline(unittest.TestCase):
    def _run(self, *, patch_producer: bool):
        """Run call_tool('review_context'); optionally inject one non-diff base hypothesis.

        Returns (result, pre_cap_types, naive_types).
        """
        captured: dict = {}
        cap_original = mcp_tools_module._cap_review_hypotheses_reserving_diff_families

        def _cap_spy(rows, limit, **kwargs):
            captured["pre"] = [r.get("risk_type") for r in rows if isinstance(r, dict)]
            captured["naive"] = [r.get("risk_type") for r in rows[:limit] if isinstance(r, dict)]
            return cap_original(rows, limit, **kwargs)

        producer_original = mcp_tools_module.review_hypotheses_for_context

        def _producer_stub(**kwargs):
            base = producer_original(**kwargs)
            # Inject one real-shaped non-diff row as the base producer output; the real
            # splices then prepend the diff-derived families ahead of it.
            return [_non_diff_hypothesis(), *base]

        with tempfile.TemporaryDirectory() as td:
            tmpdir = Path(td)
            out_base, out_head, base_ck, head_ck = _build_combined_pair(tmpdir)
            head_kg = KgSnapshot(out_head)
            ctx = mock.patch.object(
                mcp_tools_module, "_cap_review_hypotheses_reserving_diff_families", _cap_spy
            )
            with ctx:
                if patch_producer:
                    producer_ctx = mock.patch.object(
                        mcp_tools_module, "review_hypotheses_for_context", _producer_stub
                    )
                else:
                    producer_ctx = mock.patch.object(
                        mcp_tools_module, "review_hypotheses_for_context", producer_original
                    )
                with producer_ctx:
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

    def test_non_diff_row_survives_cap_and_budget(self):
        result, pre_types, naive_types = self._run(patch_producer=True)

        # Precondition: the producer over-generates diff families beyond the cap.
        pre_diff = {rt for rt in pre_types if rt in _DIFF_DERIVED_RISK_TYPES}
        self.assertGreaterEqual(
            len(pre_diff), 4,
            f"fixture must span >=4 diff families; got {pre_diff}",
        )
        self.assertGreater(
            len([rt for rt in pre_types if rt in _DIFF_DERIVED_RISK_TYPES]),
            PLANNING_CONTEXT_SECTION_LIMIT,
            f"fixture must over-produce diff rows beyond the cap; pre={pre_types}",
        )
        # Precondition: a non-diff row was generated.
        self.assertIn(
            _NON_DIFF_RISK_TYPE, pre_types,
            f"non-diff row must be present pre-cap; pre={pre_types}",
        )

        # The reserved seat: the non-diff row survives the FINAL packet (cap + budget).
        returned = [h.get("risk_type") for h in (result.get("review_hypotheses") or [])]
        self.assertIn(
            _NON_DIFF_RISK_TYPE, returned,
            f"non-diff row was evicted; returned={returned}",
        )

    def test_diff_families_still_present_alongside_non_diff_seat(self):
        """The non-diff seat takes exactly one slot; the remaining slots still hold diff
        families, so reserving the seat does not silence diff-derived evidence."""
        result, _pre, _naive = self._run(patch_producer=True)
        returned = {h.get("risk_type") for h in (result.get("review_hypotheses") or [])}
        diff_returned = {rt for rt in returned if rt in _DIFF_DERIVED_RISK_TYPES}
        self.assertGreaterEqual(
            len(diff_returned), 3,
            f"diff families must still occupy most slots; returned={sorted(returned)}",
        )

    # INVERSION PROOF: cap-layer unit inversion — a HIGH-SCORE non-diff row placed AFTER
    # five zero-score diff families is seated by score; a naive [:limit] slice drops it.
    def test_cap_inversion_naive_slice_drops_non_diff(self):
        # Diff families score 0 (no structural signal); the non-diff row's cause is a
        # CHANGED production file → +_BOOST_CAUSE_IN_CHANGED_PROD_FILE, so it outscores
        # every diff family and wins a seat on the shared score basis.
        diff_rows = [
            {"risk_type": "guard_call_removed_drift", "derivation": "deterministic_static"},
            {"risk_type": "responsibility_moved_drift", "derivation": "deterministic_static"},
            {"risk_type": "test_reference_removed_drift", "derivation": "deterministic_static"},
            {"risk_type": "abstract_contract_unimplemented", "derivation": "deterministic_static"},
            {"risk_type": "contract_semantic_diff", "derivation": "inferred_llm"},
        ]
        non_diff = {
            "risk_type": _NON_DIFF_RISK_TYPE,
            "derivation": None,
            "cause": {"path": "src/core.py", "line_start": 1},
        }
        ordered = [*diff_rows, non_diff]
        changed_files = ["src/core.py"]

        naive = {r["risk_type"] for r in ordered[:PLANNING_CONTEXT_SECTION_LIMIT]}
        self.assertNotIn(
            _NON_DIFF_RISK_TYPE, naive,
            "inversion precondition: naive slice must drop the non-diff row",
        )
        kept = _cap_review_hypotheses_reserving_diff_families(
            ordered, PLANNING_CONTEXT_SECTION_LIMIT, changed_files=changed_files
        )
        kept_types = {r["risk_type"] for r in kept}
        self.assertIn(
            _NON_DIFF_RISK_TYPE, kept_types,
            f"high-score non-diff row must win a seat by score; kept={kept_types}",
        )
        self.assertEqual(len(kept), PLANNING_CONTEXT_SECTION_LIMIT)
        # One slot goes to the score-winning non-diff family; the rest to diff families.
        diff_kept = {rt for rt in kept_types if rt in _DIFF_DERIVED_RISK_TYPES}
        self.assertEqual(len(diff_kept), PLANNING_CONTEXT_SECTION_LIMIT - 1)

    def test_cap_keeps_scoring_representative_not_family_first_row(self):
        # P2: A family's earlier row scores low (cause in an UNchanged prod file → 0) and a
        # later row scores high (cause in a CHANGED prod file → +2). The seat plan ranks the
        # family by its best (later) row; the cap must return that high-score representative,
        # not the family's first occurrence.
        rt = _NON_DIFF_RISK_TYPE
        low_first = {
            "hypothesis_id": "hyp:low",
            "risk_type": rt,
            "derivation": "inferred_llm",
            "cause": {"path": "src/other.py", "line_start": 1},
        }
        high_later = {
            "hypothesis_id": "hyp:high",
            "risk_type": rt,
            "derivation": "inferred_llm",
            "cause": {"path": "src/core.py", "line_start": 5},
        }
        ordered = [low_first, high_later]
        changed_files = ["src/core.py"]

        # Inversion precondition: keeping the family's FIRST row would return the low-score row.
        self.assertEqual(ordered[0]["hypothesis_id"], "hyp:low")

        kept = _cap_review_hypotheses_reserving_diff_families(
            ordered, 1, changed_files=changed_files
        )
        self.assertEqual(len(kept), 1)
        self.assertEqual(
            kept[0]["hypothesis_id"], "hyp:high",
            "cap must keep the score-winning representative, not the family's first row",
        )

    def test_cap_representative_comes_first_when_family_rows_spare_slots(self):
        # When both rows of the seated family fit (spare slots), the representative comes
        # first and the other follows in existing order.
        rt = _NON_DIFF_RISK_TYPE
        low_first = {
            "hypothesis_id": "hyp:low",
            "risk_type": rt,
            "derivation": "inferred_llm",
            "cause": {"path": "src/other.py", "line_start": 1},
        }
        high_later = {
            "hypothesis_id": "hyp:high",
            "risk_type": rt,
            "derivation": "inferred_llm",
            "cause": {"path": "src/core.py", "line_start": 5},
        }
        ordered = [low_first, high_later]
        kept = _cap_review_hypotheses_reserving_diff_families(
            ordered, 5, changed_files=["src/core.py"]
        )
        self.assertEqual([r["hypothesis_id"] for r in kept], ["hyp:high", "hyp:low"])


class TestBudgetRepresentativeOrdering(unittest.TestCase):
    """P2 regression: budget path keeps the seat-plan representative, not the family's first row.

    Two rows share one risk_type. The first row has cause in an unchanged file (score=0);
    the second is the representative with cause in a CHANGED file (higher score). When only
    one row fits through the budget, the representative must survive. Tested both with a
    threaded seat plan AND without one (standalone-caller fallback recomputes from hypotheses).
    """

    _RT = _NON_DIFF_RISK_TYPE
    _CHANGED_FILE = "src/module_0.py"

    def _low_first(self) -> dict:
        """Earlier row: cause in an UNchanged file → score=0, NOT the representative."""
        return {
            "hypothesis_id": "hyp:low:first",
            "risk_type": self._RT,
            "derivation": "inferred_llm",
            "confidence": "medium",
            "specificity": "medium",
            "why": "Low-score earlier row.",
            "postable_claim": "low claim",
            "cause": {"repo": "repo-test", "path": "src/other.py", "line_start": 1, "line_end": 2},
            "consequence": {"repo": "repo-test", "path": "src/other.py", "line_start": 1, "line_end": 2},
            "source_checks": ["check A"],
            "evidence_refs": [{"repo": "repo-test", "path": "src/other.py", "line_start": 1, "line_end": 2}],
            "supporting_lead_ids": [],
        }

    def _high_later(self) -> dict:
        """Later row: cause in the CHANGED file → higher score, IS the representative."""
        return {
            "hypothesis_id": "hyp:high:later",
            "risk_type": self._RT,
            "derivation": "inferred_llm",
            "confidence": "high",
            "specificity": "high",
            "why": "High-score representative row — cause is in the changed production file.",
            "postable_claim": "high claim",
            "cause": {"repo": "repo-test", "path": self._CHANGED_FILE, "line_start": 10, "line_end": 20},
            "consequence": {"repo": "repo-test", "path": self._CHANGED_FILE, "line_start": 10, "line_end": 20},
            "source_checks": ["check B"],
            "evidence_refs": [{"repo": "repo-test", "path": self._CHANGED_FILE, "line_start": 10, "line_end": 20}],
            "supporting_lead_ids": [],
        }

    def _tight_budget_packet(self, hypotheses: list[dict], *, with_seat_plan: bool) -> dict:
        """Packet with extra bulk so only one hypothesis fits after budgeting."""
        from copy import deepcopy
        from source.kg.product.output_budget import compute_hypothesis_seat_plan

        packet = _base_packet(hypotheses, extra_bulk=50)
        # Verify it IS over budget before adding the seat plan.
        from source.kg.core.models import canonical_json
        assert len(canonical_json(packet)) > REVIEW_CONTEXT_MAX_CHARS, (
            "fixture must exceed budget; increase extra_bulk"
        )
        if with_seat_plan:
            changed_files = ["src/module_0.py", "src/module_1.py"]
            plan = compute_hypothesis_seat_plan(
                hypotheses,
                changed_files=changed_files,
                changed_symbols=[],
            )
            packet["_hypothesis_seat_plan"] = plan
        # No _hypothesis_seat_plan key → standalone-caller fallback recomputes inside budget.
        return packet

    def _assert_representative_kept(self, result: dict, label: str) -> None:
        returned_ids = [
            h.get("hypothesis_id")
            for h in (result.get("review_hypotheses") or [])
            if isinstance(h, dict)
        ]
        self.assertIn(
            "hyp:high:later", returned_ids,
            f"{label}: representative must be kept; returned_ids={returned_ids}",
        )

    def _assert_low_row_evicted(self, result: dict, label: str) -> None:
        returned_ids = [
            h.get("hypothesis_id")
            for h in (result.get("review_hypotheses") or [])
            if isinstance(h, dict)
        ]
        self.assertNotIn(
            "hyp:low:first", returned_ids,
            f"{label}: low-score first row must be evicted when only one fits; returned_ids={returned_ids}",
        )

    def test_budget_keeps_representative_with_threaded_seat_plan(self):
        """With seat_plan threaded: representative survives, low-score row evicted."""
        hypotheses = [self._low_first(), self._high_later()]
        packet = self._tight_budget_packet(hypotheses, with_seat_plan=True)
        result = enforce_review_context_budget(packet)

        returned_ids = [
            h.get("hypothesis_id")
            for h in (result.get("review_hypotheses") or [])
            if isinstance(h, dict)
        ]
        # Inversion precondition: if the original order were kept the first (low) row would
        # appear before the representative, and under tight budget the first row survives.
        self.assertIn(
            "hyp:high:later", returned_ids,
            f"threaded plan: representative must be kept; returned_ids={returned_ids}",
        )

    def test_budget_keeps_representative_without_seat_plan(self):
        """Without seat_plan (standalone-caller fallback recomputes): representative survives."""
        hypotheses = [self._low_first(), self._high_later()]
        packet = self._tight_budget_packet(hypotheses, with_seat_plan=False)
        result = enforce_review_context_budget(packet)

        returned_ids = [
            h.get("hypothesis_id")
            for h in (result.get("review_hypotheses") or [])
            if isinstance(h, dict)
        ]
        self.assertIn(
            "hyp:high:later", returned_ids,
            f"standalone fallback: representative must be kept; returned_ids={returned_ids}",
        )

    def test_budget_representative_ordering_inversion_proof(self):
        """Inversion: if sorted by family rank only (old code), the low-score row comes first
        and under tight budget the low-score row is the floor and the representative is evicted.
        The fix must prevent this.
        """
        hypotheses = [self._low_first(), self._high_later()]
        # Confirm list order: low-score is index 0 (would be the floor under the old sort).
        self.assertEqual(hypotheses[0]["hypothesis_id"], "hyp:low:first")
        self.assertEqual(hypotheses[1]["hypothesis_id"], "hyp:high:later")

        packet = self._tight_budget_packet(hypotheses, with_seat_plan=False)
        result = enforce_review_context_budget(packet)

        returned_ids = [
            h.get("hypothesis_id")
            for h in (result.get("review_hypotheses") or [])
            if isinstance(h, dict)
        ]
        # Under the old sort (family rank only), index 0 ("hyp:low:first") would be the
        # floor and index 1 ("hyp:high:later") the eviction candidate. The fix must place
        # the representative first so the representative is the floor.
        self.assertIn(
            "hyp:high:later", returned_ids,
            f"inversion proof: representative must not be evicted ahead of low-score row; returned_ids={returned_ids}",
        )


if __name__ == "__main__":
    unittest.main()
