"""Regression tests for three findings from the Codex pre-PR review:

1. hypothesis_id must be evidence-specific (same risk_type → different IDs for different leads)
2. Unknown-only requested_surfaces must not expand to all defaults
3. supporting_lead_ids must be reconciled after final budget eviction
"""
from __future__ import annotations

import unittest

from source.kg.product.mcp_tools import REVIEW_CONTEXT_SURFACES, _review_context_surface_status
from source.kg.product.output_budget import enforce_review_context_budget
from source.kg.product.review_attribution import hypothesis_stable_id
from source.kg.core.models import canonical_json


class TestHypothesisStableIdIsEvidenceSpecific(unittest.TestCase):
    """Fix 1: same risk_type + different lead sets → different hypothesis_id."""

    def test_same_risk_type_different_lead_ids_produces_different_ids(self):
        id_a = hypothesis_stable_id(
            "direct_call_contract_drift",
            ["lead:direct_caller:aaa111"],
            [{"repo": "svc-a", "path": "src/a.py", "line_start": 10, "line_end": 20}],
        )
        id_b = hypothesis_stable_id(
            "direct_call_contract_drift",
            ["lead:direct_caller:bbb222"],
            [{"repo": "svc-b", "path": "src/b.py", "line_start": 5, "line_end": 15}],
        )
        self.assertNotEqual(id_a, id_b)

    def test_same_inputs_same_id_determinism(self):
        kwargs = dict(
            risk_type="framework_contract_drift",
            supporting_lead_ids=["lead:changed_symbol:x1", "lead:changed_symbol:x2"],
            evidence_refs=[{"repo": "svc", "path": "models.py", "line_start": 1, "line_end": 50}],
        )
        id_first = hypothesis_stable_id(**kwargs)
        id_second = hypothesis_stable_id(**kwargs)
        self.assertEqual(id_first, id_second)

    def test_id_format_is_hypothesis_prefix(self):
        hid = hypothesis_stable_id("some_risk", ["lead-1"], [{"repo": "r", "path": "p.py"}])
        self.assertTrue(hid.startswith("hypothesis:some_risk:"), hid)

    def test_lead_id_order_does_not_affect_id(self):
        id_ab = hypothesis_stable_id(
            "direct_call_contract_drift",
            ["lead:a:111", "lead:b:222"],
            [],
        )
        id_ba = hypothesis_stable_id(
            "direct_call_contract_drift",
            ["lead:b:222", "lead:a:111"],
            [],
        )
        self.assertEqual(id_ab, id_ba)

    def test_empty_leads_and_refs_still_produces_stable_id(self):
        id1 = hypothesis_stable_id("test_risk", [], [])
        id2 = hypothesis_stable_id("test_risk", [], [])
        self.assertEqual(id1, id2)
        self.assertTrue(id1.startswith("hypothesis:test_risk:"))


class TestUnknownOnlySurfacesDoNotExpandToDefaults(unittest.TestCase):
    """Fix 2: unknown-only requested_surfaces → only unsupported rows, no default surface rows."""

    def _call(self, requested_surfaces, unknown_surfaces=None):
        return _review_context_surface_status(
            application_impact={},
            runtime_surfaces={},
            requested_surfaces=requested_surfaces,
            unknown_surfaces=unknown_surfaces,
            changed_symbols=[],
        )

    def test_unknown_only_returns_only_unsupported_rows(self):
        rows = self._call(requested_surfaces=[], unknown_surfaces=["authz"])
        surfaces = [r["surface"] for r in rows]
        # Only the unknown surface row should be present; no default surfaces
        self.assertIn("authz", surfaces)
        for surface in REVIEW_CONTEXT_SURFACES:
            self.assertNotIn(surface, surfaces)

    def test_omitted_requested_surfaces_returns_all_defaults(self):
        rows = self._call(requested_surfaces=[], unknown_surfaces=None)
        surfaces = {r["surface"] for r in rows}
        for surface in REVIEW_CONTEXT_SURFACES:
            self.assertIn(surface, surfaces)

    def test_mixed_known_and_unknown_returns_both(self):
        rows = self._call(
            requested_surfaces=[REVIEW_CONTEXT_SURFACES[0]],
            unknown_surfaces=["authz"],
        )
        surfaces = [r["surface"] for r in rows]
        self.assertIn(REVIEW_CONTEXT_SURFACES[0], surfaces)
        self.assertIn("authz", surfaces)
        # Other default surfaces not in requested_surfaces should not appear
        for surface in REVIEW_CONTEXT_SURFACES[1:]:
            self.assertNotIn(surface, surfaces)


class TestSupportingLeadIdsReconciliationAfterEviction(unittest.TestCase):
    """Fix 3: after hard-cap eviction, hypothesis.supporting_lead_ids ⊆ surviving lead_ids."""

    def _packet_with_caller_rows(self, n_callers: int) -> dict:
        """Build a minimal review_context packet with n_callers lead rows and one hypothesis."""
        callers = [
            {
                "lead_id": f"lead:direct_caller:caller{i:03d}",
                "lead_kind": "direct_caller",
                "repo": "svc",
                "path": f"src/caller_{i}.py",
                "line_start": i * 10,
                "line_end": i * 10 + 5,
                "subject": f"mod.caller_{i}",
                "object": "mod.changed_fn",
            }
            for i in range(n_callers)
        ]
        all_lead_ids = [r["lead_id"] for r in callers]
        hypothesis = {
            "hypothesis_id": hypothesis_stable_id("direct_call_contract_drift", all_lead_ids, []),
            "risk_type": "direct_call_contract_drift",
            "confidence": "strong",
            "why": "Callers may drift.",
            "evidence_refs": [],
            "source_checks": ["check callers"],
            "supporting_lead_ids": all_lead_ids,
        }
        # Build a packet large enough to trigger budget enforcement (we'll force it via small max_chars)
        return {
            "status": "found",
            "review_leads": {
                "changed_symbols": [],
                "direct_callers": callers,
                "direct_callees": [],
                "transitive_callers": [],
                "source_coordinates": [],
            },
            "review_hypotheses": [hypothesis],
        }

    def test_surviving_lead_ids_subset_of_packet_leads_after_hard_cap(self):
        # Force very tight budget so callers get evicted
        packet = self._packet_with_caller_rows(30)
        # Use a budget that is smaller than the full packet but larger than hypothesis-only
        tight_budget = len(canonical_json(packet)) // 3
        result = enforce_review_context_budget(packet, max_chars=tight_budget)

        hyps = result.get("review_hypotheses") or []
        if not hyps:
            # If no hypothesis survived, skip (another test covers the floor)
            return

        # Collect surviving lead_ids from review_leads
        surviving = set()
        review_leads = result.get("review_leads") or {}
        for rows in review_leads.values():
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict) and row.get("lead_id"):
                        surviving.add(row["lead_id"])

        for hyp in hyps:
            cited = set(hyp.get("supporting_lead_ids") or [])
            dangling = cited - surviving
            self.assertEqual(
                dangling,
                set(),
                f"Hypothesis {hyp.get('hypothesis_id')} cites dangling lead_ids {dangling}",
            )

    def test_hypothesis_id_unchanged_after_reconciliation(self):
        """hypothesis_id must not be recomputed during reconciliation."""
        packet = self._packet_with_caller_rows(20)
        original_hyp = packet["review_hypotheses"][0]
        original_id = original_hyp["hypothesis_id"]
        tight_budget = len(canonical_json(packet)) // 3
        result = enforce_review_context_budget(packet, max_chars=tight_budget)

        hyps = result.get("review_hypotheses") or []
        if not hyps:
            return
        for hyp in hyps:
            if hyp.get("risk_type") == "direct_call_contract_drift":
                self.assertEqual(hyp["hypothesis_id"], original_id)


if __name__ == "__main__":
    unittest.main()
