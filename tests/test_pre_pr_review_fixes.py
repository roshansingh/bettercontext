"""Regression tests for Copilot and Codex pre-PR review findings:

1. hypothesis_id must be evidence-specific (same risk_type → different IDs for different leads)
2. Unknown-only requested_surfaces must not expand to all defaults
3. supporting_lead_ids must be reconciled after final budget eviction
4. Whitespace tokens must be split into individual inspection terms
5. Hard-cap guarantee must hold even when no rows are evictable after status attach
6. Stale returned counts after floor eviction must be resynced
"""
from __future__ import annotations

import unittest

from source.kg.product.mcp_tools import (
    REVIEW_CONTEXT_SURFACES,
    _REVIEW_CONTEXT_SURFACE_TOKEN_MAX_LEN,
    _review_context_surface_status,
    _review_context_unknown_surface_status_row,
)
from source.kg.product.output_budget import (
    _finalize_review_hypothesis_budget,
    _protect_review_hypotheses_floor,
    enforce_review_context_budget,
)
from source.kg.product.review_attribution import review_lead_counts
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

    def test_mixed_none_int_line_start_no_exception(self):
        # Regression: refs sharing repo+path with None and int line_start must not raise TypeError.
        refs = [
            {"repo": "svc", "path": "src/a.py", "line_start": None, "line_end": 10},
            {"repo": "svc", "path": "src/a.py", "line_start": 3, "line_end": None},
            {"repo": "svc", "path": "src/a.py", "line_start": "5", "line_end": 7},
        ]
        id1 = hypothesis_stable_id("test_risk", ["lead-1"], refs)
        id2 = hypothesis_stable_id("test_risk", ["lead-1"], refs)
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


class TestUnknownSurfaceSymbolNameTruncation(unittest.TestCase):
    """Regression: symbol-derived terms in unknown surface rows must be capped at 200 chars."""

    def test_long_qualified_name_is_truncated_in_terms(self):
        long_qualname = "a.very.long." + "x" * 250
        row = _review_context_unknown_surface_status_row(
            "authz",
            changed_symbols=[{"qualified_name": long_qualname}],
        )
        for term in row["source_inspection_terms"]:
            self.assertLessEqual(
                len(term),
                _REVIEW_CONTEXT_SURFACE_TOKEN_MAX_LEN,
                f"term exceeds {_REVIEW_CONTEXT_SURFACE_TOKEN_MAX_LEN} chars: {term!r}",
            )


class TestUnknownSurfaceSpaceTokenSplit(unittest.TestCase):
    """Fix 4: tokens containing spaces must be split into individual inspection terms."""

    def test_space_containing_token_splits_into_words(self):
        row = _review_context_unknown_surface_status_row(
            "ability checks",
            changed_symbols=[],
        )
        terms = row["source_inspection_terms"]
        self.assertIn("ability", terms, f"expected 'ability' in terms: {terms}")
        self.assertIn("checks", terms, f"expected 'checks' in terms: {terms}")

    def test_hyphen_space_mixed_token_splits_all_words(self):
        row = _review_context_unknown_surface_status_row(
            "read-write access",
            changed_symbols=[],
        )
        terms = row["source_inspection_terms"]
        self.assertIn("read", terms)
        self.assertIn("write", terms)
        self.assertIn("access", terms)

    def test_original_token_still_in_surface_field(self):
        token = "ability checks"
        row = _review_context_unknown_surface_status_row(token, changed_symbols=[])
        # surface echoes the (possibly truncated) original token
        self.assertEqual(row["surface"], token)


class TestFinalizeReviewHypothesisBudgetHardCapNoEvictable(unittest.TestCase):
    """Fix 5: hard-cap holds when status attach overflows and nothing is evictable."""

    def _scalar_only_packet(self, original_count: int, returned_count: int) -> dict:
        """Packet with no evictable list-of-dict rows and fewer hypotheses returned than available."""
        hyps = [
            {
                "hypothesis_id": f"hypothesis:test_risk:id{i:04d}",
                "risk_type": "test_risk",
                "confidence": "weak",
                "why": "x",
                "evidence_refs": [],
                "source_checks": [],
                "supporting_lead_ids": [],
            }
            for i in range(returned_count)
        ]
        original = [
            {
                "hypothesis_id": f"hypothesis:test_risk:id{i:04d}",
                "risk_type": "test_risk",
                "confidence": "weak",
                "why": "x",
                "evidence_refs": [],
                "source_checks": [],
                "supporting_lead_ids": [],
            }
            for i in range(original_count)
        ]
        # Only scalar values + review_hypotheses (protected); no other list-of-dicts.
        return {
            "status": "found",
            "review_hypotheses": hyps,
        }, original

    def test_packet_does_not_exceed_max_chars_when_no_rows_evictable(self):
        packet, original = self._scalar_only_packet(original_count=5, returned_count=2)
        # Set max_chars to the size of the packet before status is attached.
        max_chars = len(canonical_json(packet))
        _finalize_review_hypothesis_budget(packet, original, max_chars=max_chars)
        self.assertLessEqual(
            len(canonical_json(packet)),
            max_chars,
            "packet exceeded max_chars after finalize with no evictable rows",
        )
        # review_hypothesis_status must be absent (was dropped to honour the cap)
        self.assertNotIn("review_hypothesis_status", packet)

    def test_status_present_when_it_fits(self):
        packet, original = self._scalar_only_packet(original_count=5, returned_count=2)
        # Provide ample budget so status can be retained.
        max_chars = len(canonical_json(packet)) + 500
        _finalize_review_hypothesis_budget(packet, original, max_chars=max_chars)
        self.assertIn(
            "review_hypothesis_status",
            packet,
            "review_hypothesis_status should be present when budget allows",
        )


class TestFloorEvictionResyncsReturnedCounts(unittest.TestCase):
    """Fix 6: floor eviction in _protect_review_hypotheses_floor must resync returned counts."""

    def _packet_with_lead_rows(self, n_callers: int) -> dict:
        """Packet with review_lead_status.returned set to n_callers, plus one hypothesis."""
        callers = [
            {
                "lead_id": f"lead:direct_caller:c{i:03d}",
                "lead_kind": "direct_caller",
                "repo": "svc",
                "path": f"src/caller_{i}.py",
                "line_start": i * 10,
                "line_end": i * 10 + 5,
                "subject": f"mod.caller_{i}",
                "object": "mod.target",
            }
            for i in range(n_callers)
        ]
        all_lead_ids = [r["lead_id"] for r in callers]
        review_leads = {
            "changed_symbols": [],
            "direct_callers": callers,
            "direct_callees": [],
            "transitive_callers": [],
            "source_coordinates": [],
        }
        hypothesis = {
            "hypothesis_id": hypothesis_stable_id("direct_call_contract_drift", all_lead_ids, []),
            "risk_type": "direct_call_contract_drift",
            "confidence": "strong",
            "why": "Callers may drift.",
            "evidence_refs": [],
            "source_checks": ["check callers"],
            "supporting_lead_ids": all_lead_ids,
        }
        original_returned = review_lead_counts(review_leads)
        return {
            "status": "found",
            "review_leads": review_leads,
            "review_lead_status": {
                "available": original_returned,
                "returned": original_returned,
                "changed_symbol_count": 0,
                "direct_impact_count": n_callers,
                "transitive_impact_count": 0,
                "source_coordinate_count": 0,
            },
            "review_hypotheses": [hypothesis],
            "output_budget": {"truncated": False, "truncated_sections": []},
        }

    def test_returned_counts_equal_surviving_rows_after_floor_eviction(self):
        # Force a packet large enough that _protect_review_hypotheses_floor will evict
        # some review_leads rows when it restores the hypothesis.
        n = 30
        packet = self._packet_with_lead_rows(n)

        # Remove hypotheses so the floor fires, then set a tight budget.
        original_hypotheses = list(packet["review_hypotheses"])
        packet["review_hypotheses"] = []
        tight_budget = len(canonical_json(packet)) // 2

        result = _protect_review_hypotheses_floor(packet, original_hypotheses, max_chars=tight_budget)

        review_leads = result.get("review_leads") or {}
        status = result.get("review_lead_status") or {}
        returned = status.get("returned") or {}

        # Count actual surviving direct_callers
        surviving_callers = review_leads.get("direct_callers") or []
        actual_direct_impact = len(surviving_callers)

        # returned["direct_caller_count"] must equal the actual surviving count
        reported_direct_callers = returned.get("direct_caller_count", -1) if isinstance(returned, dict) else -1
        self.assertEqual(
            reported_direct_callers,
            actual_direct_impact,
            f"returned.direct_caller_count={reported_direct_callers} != surviving={actual_direct_impact}",
        )

    def test_available_counts_preserved_after_floor_eviction(self):
        n = 20
        packet = self._packet_with_lead_rows(n)
        original_available = dict(packet["review_lead_status"]["available"])
        original_hypotheses = list(packet["review_hypotheses"])
        packet["review_hypotheses"] = []
        tight_budget = len(canonical_json(packet)) // 2

        result = _protect_review_hypotheses_floor(packet, original_hypotheses, max_chars=tight_budget)

        status = result.get("review_lead_status") or {}
        available = status.get("available")
        self.assertEqual(available, original_available, "available counts must not be mutated by floor eviction")


if __name__ == "__main__":
    unittest.main()
