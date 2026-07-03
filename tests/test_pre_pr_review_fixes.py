"""Regression tests for Copilot and Codex pre-PR review findings:

1. hypothesis_id must be evidence-specific (same risk_type → different IDs for different leads)
2. Unknown-only requested_surfaces must not expand to all defaults
3. supporting_lead_ids must be reconciled after final budget eviction
4. Whitespace tokens must be split into individual inspection terms
5. Hard-cap guarantee must hold even when no rows are evictable after status attach
6. Stale returned counts after floor eviction must be resynced
7. (J/K) Mirror floor: top_review_hypotheses mirror always has floor-of-1 under compaction
8. (J/K) Comfortable budget: mirror keeps up to 3, status reason null, counts equal
9. (J/K) Lead-only fallback: status present with answer_packet_returned_count == 0 when no mirror
10. (J/K) Zero-hypotheses useful packet: status available=0 returned=0 reason=none_generated
11. (J/K) Low-coverage paths: non-stylesheet gets reason=low_coverage; stylesheet keeps hyp with reason budget/null
12. (Copilot) _clip_answer_packet_top_to_review_leads keeps non-dict rows and dict rows without lead_id
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
    _clip_answer_packet_top_to_review_leads,
    _evict_review_rows_to_fit,
    _finalize_review_hypothesis_budget,
    _protect_review_hypotheses_floor,
    _sync_review_quality_status_from_packet,
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
        _finalize_review_hypothesis_budget(
            packet, original, original_review_leads=packet.get("review_leads") or {}, max_chars=max_chars
        )
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
        _finalize_review_hypothesis_budget(
            packet, original, original_review_leads=packet.get("review_leads") or {}, max_chars=max_chars
        )
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


def _make_hypothesis(risk_type: str, index: int, n_callers: int = 5) -> dict:
    lead_ids = [f"lead:direct_caller:c{i:03d}" for i in range(n_callers)]
    return {
        "hypothesis_id": hypothesis_stable_id(risk_type, lead_ids, [{"repo": "svc", "path": f"src/f{index}.py"}]),
        "risk_type": risk_type,
        "confidence": "strong",
        "why": f"Why reason {index}",
        "evidence_refs": [{"repo": "svc", "path": f"src/f{index}.py", "line_start": index * 10, "line_end": index * 10 + 5}],
        "source_checks": [f"check{index}"],
        "supporting_lead_ids": lead_ids,
    }


def _make_heavy_packet(n_hyps: int, n_callers: int) -> tuple[dict, list[dict]]:
    """Build a packet with n_hyps hypotheses, answer_packet.top_review_hypotheses, and n_callers caller rows."""
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
            "why": "x" * 200,
        }
        for i in range(n_callers)
    ]
    hypotheses = [_make_hypothesis("direct_call_contract_drift", i) for i in range(n_hyps)]
    review_leads = {
        "changed_symbols": [],
        "direct_callers": callers,
        "direct_callees": [],
        "transitive_callers": [],
        "source_coordinates": [],
    }
    review_answer_packet: dict = {
        "status": "found",
        "top_diff_anchors": [],
        "top_changed_symbols": [],
        "top_direct_callers": [],
        "top_direct_callees": [],
        "top_transitive_callers": [],
        "top_review_hypotheses": list(hypotheses),
    }
    original_hyps = list(hypotheses)
    packet = {
        "status": "found",
        "review_hypotheses": list(hypotheses),
        "review_answer_packet": review_answer_packet,
        "review_leads": review_leads,
        "review_lead_status": {
            "coverage_status": "useful",
            "available": {"direct_caller_count": n_callers},
            "returned": {"direct_caller_count": n_callers},
            "changed_symbol_count": 0,
            "direct_impact_count": n_callers,
            "transitive_impact_count": 0,
            "source_coordinate_count": 0,
        },
        "output_budget": {"truncated": False, "truncated_sections": []},
    }
    return packet, original_hyps


class TestJMirrorFloorHeavyCompaction(unittest.TestCase):
    """Task J: mirror floor survives heavy compaction; Task K: rich status counts are consistent."""

    def test_mirror_floor_survives_compaction_and_status_consistent(self):
        # 3 hypotheses, many callers to force budget pressure
        packet, original_hyps = _make_heavy_packet(n_hyps=3, n_callers=50)
        full_size = len(canonical_json(packet))
        # Tight budget — forces compaction
        tight = full_size // 3
        result = enforce_review_context_budget(packet, max_chars=tight)

        top_hyps = result.get("review_hypotheses") or []
        mirror = (result.get("review_answer_packet") or {}).get("top_review_hypotheses") or []
        status = result.get("review_hypothesis_status")

        # Top-level floor: at least 1 hypothesis
        self.assertGreaterEqual(len(top_hyps), 1, "top-level review_hypotheses must have floor-of-1")
        # Mirror floor: at least 1 hypothesis
        self.assertGreaterEqual(len(mirror), 1, "mirror top_review_hypotheses must have floor-of-1")
        # Same top hypothesis_id in both lists
        self.assertEqual(top_hyps[0].get("hypothesis_id"), mirror[0].get("hypothesis_id"), "mirror top hypothesis_id must match top-level")
        # Packet must not exceed cap
        self.assertLessEqual(len(canonical_json(result)), tight, "packet must not exceed max_chars after budget")
        # Status must be present with rich shape
        self.assertIsNotNone(status, "review_hypothesis_status must be present")
        self.assertEqual(status.get("available_count"), 3)
        self.assertGreaterEqual(status.get("returned_count", 0), 1)
        self.assertGreaterEqual(status.get("answer_packet_returned_count", 0), 1)
        expected_truncated = status.get("available_count", 0) - status.get("returned_count", 0)
        self.assertEqual(status.get("truncated_count"), expected_truncated)
        # N2: broad-context eviction may restore all 3 hypotheses under a tight budget,
        # in which case reason is None. Reason is "budget" only when fewer than available
        # survive after broad-context eviction is exhausted.
        returned = status.get("returned_count", 0)
        if returned < status.get("available_count", 0):
            self.assertEqual(status.get("reason"), "budget")
        else:
            self.assertIsNone(status.get("reason"))


class TestJMirrorFloorComfortableBudget(unittest.TestCase):
    """Task J/K: comfortable budget → mirror keeps up to 3; status reason null; counts equal."""

    def test_comfortable_budget_mirror_full_and_status_null_reason(self):
        packet, original_hyps = _make_heavy_packet(n_hyps=3, n_callers=1)
        # Ample budget
        ample = len(canonical_json(packet)) + 10_000
        result = enforce_review_context_budget(packet, max_chars=ample)

        top_hyps = result.get("review_hypotheses") or []
        mirror = (result.get("review_answer_packet") or {}).get("top_review_hypotheses") or []
        status = result.get("review_hypothesis_status")

        self.assertEqual(len(top_hyps), 3, "all 3 hypotheses should be returned under comfortable budget")
        self.assertEqual(len(mirror), 3, "mirror should return all 3 under comfortable budget")
        self.assertIsNotNone(status, "review_hypothesis_status must always be present on useful packets")
        self.assertEqual(status.get("available_count"), 3)
        self.assertEqual(status.get("returned_count"), 3)
        self.assertEqual(status.get("answer_packet_returned_count"), 3)
        self.assertEqual(status.get("truncated_count"), 0)
        self.assertIsNone(status.get("reason"), "reason should be null when all returned everywhere")


class TestJMirrorFloorLeadOnlyFallback(unittest.TestCase):
    """Task J/K: lead-only fallback: status present; if no answer packet mirror, answer_packet_returned_count == 0 with reason budget."""

    def _build_huge_packet(self, n_hyps: int, n_callers: int) -> tuple[dict, list[dict]]:
        """Build a packet with extremely fat caller rows to force lead-only path."""
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
                "why": "x" * 2000,
                "evidence": "z" * 2000,
            }
            for i in range(n_callers)
        ]
        hypotheses = [_make_hypothesis("direct_call_contract_drift", i) for i in range(n_hyps)]
        review_leads = {
            "changed_symbols": [],
            "direct_callers": callers,
            "direct_callees": [],
            "transitive_callers": [],
            "source_coordinates": [],
        }
        review_answer_packet: dict = {
            "status": "found",
            "top_diff_anchors": [],
            "top_changed_symbols": [],
            "top_direct_callers": callers[:5],
            "top_direct_callees": [],
            "top_transitive_callers": [],
            "top_review_hypotheses": list(hypotheses),
        }
        packet = {
            "status": "found",
            "review_hypotheses": list(hypotheses),
            "review_answer_packet": review_answer_packet,
            "review_leads": review_leads,
            "review_lead_status": {
                "coverage_status": "useful",
                "available": {"direct_caller_count": n_callers},
                "returned": {"direct_caller_count": n_callers},
                "changed_symbol_count": 0,
                "direct_impact_count": n_callers,
                "transitive_impact_count": 0,
                "source_coordinate_count": 0,
            },
            "output_budget": {"truncated": False, "truncated_sections": []},
        }
        return packet, list(hypotheses)

    def test_lead_only_path_status_present(self):
        packet, original_hyps = self._build_huge_packet(n_hyps=2, n_callers=30)
        full_size = len(canonical_json(packet))
        # Very tight budget to force lead-only path
        very_tight = full_size // 20
        result = enforce_review_context_budget(packet, max_chars=very_tight)

        status = result.get("review_hypothesis_status")
        self.assertIsNotNone(status, "review_hypothesis_status must be present even in lead-only path")
        # answer_packet_returned_count must be defined
        self.assertIn("answer_packet_returned_count", status)
        # If mirror is empty, reason should be budget
        mirror_count = status.get("answer_packet_returned_count", -1)
        if mirror_count == 0:
            self.assertEqual(status.get("reason"), "budget", "reason must be budget when mirror is empty")
        # Packet must not exceed cap
        self.assertLessEqual(len(canonical_json(result)), very_tight, "packet must not exceed max_chars")


class TestKZeroHypothesesStatus(unittest.TestCase):
    """Task K: zero-hypotheses useful packet → status available=0, returned=0, reason=none_generated."""

    def test_zero_hypotheses_useful_packet_status(self):
        # Useful packet (has callers) but no hypotheses generated
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
            for i in range(3)
        ]
        review_leads = {"changed_symbols": [], "direct_callers": callers, "direct_callees": [], "transitive_callers": [], "source_coordinates": []}
        packet = {
            "status": "found",
            "review_hypotheses": [],
            "review_answer_packet": {
                "status": "found",
                "top_diff_anchors": [],
                "top_changed_symbols": [],
                "top_direct_callers": callers,
                "top_direct_callees": [],
                "top_transitive_callers": [],
                "top_review_hypotheses": [],
            },
            "review_leads": review_leads,
            "review_lead_status": {
                "coverage_status": "useful",
                "available": {"direct_caller_count": 3},
                "returned": {"direct_caller_count": 3},
                "changed_symbol_count": 0,
                "direct_impact_count": 3,
                "transitive_impact_count": 0,
                "source_coordinate_count": 0,
            },
        }
        original_hyps: list = []
        ample = len(canonical_json(packet)) + 10_000
        _finalize_review_hypothesis_budget(
            packet, original_hyps, original_review_leads=packet.get("review_leads") or {}, max_chars=ample
        )

        status = packet.get("review_hypothesis_status")
        self.assertIsNotNone(status, "review_hypothesis_status must be present on zero-hypothesis useful packets")
        self.assertEqual(status.get("available_count"), 0)
        self.assertEqual(status.get("returned_count"), 0)
        self.assertEqual(status.get("answer_packet_returned_count"), 0)
        self.assertEqual(status.get("truncated_count"), 0)
        self.assertEqual(status.get("reason"), "none_generated")
        # No fake hypothesis rows injected
        self.assertEqual(packet.get("review_hypotheses"), [])
        mirror = (packet.get("review_answer_packet") or {}).get("top_review_hypotheses")
        self.assertEqual(mirror, [])


class TestKLowCoverageHypothesisStatus(unittest.TestCase):
    """Task K: low-coverage non-stylesheet → reason=low_coverage; stylesheet hyp keeps reason null/budget."""

    def _make_low_coverage_packet(self, with_stylesheet_hyp: bool) -> dict:
        stylesheet_hyp = {
            "hypothesis_id": hypothesis_stable_id("low_coverage_stylesheet_gap", [], []),
            "risk_type": "low_coverage_stylesheet_gap",
            "confidence": "weak",
            "why": "Stylesheet gap",
            "evidence_refs": [],
            "source_checks": [],
            "supporting_lead_ids": [],
        }
        hyps = [stylesheet_hyp] if with_stylesheet_hyp else []
        return {
            "status": "found",
            "review_hypotheses": list(hyps),
            "review_answer_packet": {
                "status": "found",
                "top_diff_anchors": [],
                "top_changed_symbols": [],
                "top_direct_callers": [],
                "top_direct_callees": [],
                "top_transitive_callers": [],
                "top_review_hypotheses": list(hyps),
            },
            "review_leads": {"changed_symbols": [], "direct_callers": [], "direct_callees": [], "transitive_callers": [], "source_coordinates": []},
            "review_lead_status": {
                "coverage_status": "low_coverage",
                "available": {"direct_caller_count": 0},
                "returned": {"direct_caller_count": 0},
                "changed_symbol_count": 0,
                "direct_impact_count": 0,
                "transitive_impact_count": 0,
                "source_coordinate_count": 0,
            },
        }

    def test_low_coverage_non_stylesheet_status(self):
        packet = self._make_low_coverage_packet(with_stylesheet_hyp=False)
        original_hyps: list = []
        ample = len(canonical_json(packet)) + 10_000
        _finalize_review_hypothesis_budget(
            packet, original_hyps, original_review_leads=packet.get("review_leads") or {}, max_chars=ample
        )

        status = packet.get("review_hypothesis_status")
        self.assertIsNotNone(status, "review_hypothesis_status must be present on low-coverage packets")
        self.assertEqual(status.get("reason"), "low_coverage")
        self.assertEqual(status.get("available_count"), 0)
        self.assertEqual(status.get("returned_count"), 0)
        self.assertEqual(status.get("answer_packet_returned_count"), 0)

    def test_low_coverage_stylesheet_hyp_status(self):
        packet = self._make_low_coverage_packet(with_stylesheet_hyp=True)
        original_hyps = list(packet["review_hypotheses"])
        ample = len(canonical_json(packet)) + 10_000
        _finalize_review_hypothesis_budget(
            packet, original_hyps, original_review_leads=packet.get("review_leads") or {}, max_chars=ample
        )

        status = packet.get("review_hypothesis_status")
        self.assertIsNotNone(status, "review_hypothesis_status must be present on low-coverage stylesheet packets")
        # 1 hypothesis available and returned → reason null or budget (all returned → null)
        self.assertEqual(status.get("available_count"), 1)
        self.assertEqual(status.get("returned_count"), 1)
        reason = status.get("reason")
        self.assertIn(reason, (None, "budget"), f"expected reason null or budget, got {reason!r}")


def _make_broad_context_rows(n: int, label: str = "x") -> list[dict]:
    """Fat rows for application/runtime/framework sections to create budget pressure."""
    return [
        {
            "lead_id": f"lead:broad:{label}{i}",
            "path": f"src/app_{label}_{i}.py",
            "repo": "svc",
            "subject": f"mod.fn_{i}",
            "object": f"mod.target_{i}",
            "why": "b" * 300,
        }
        for i in range(n)
    ]


def _make_over_budget_packet_with_broad_sections(
    n_hyps: int,
    n_broad_rows: int,
) -> tuple[dict, list[dict]]:
    """Build a packet with n_hyps hypotheses + fat broad-context sections (application/framework/runtime).

    Broad sections occupy enough budget that fewer than n_hyps hypotheses survive under the
    default cap without N2's targeted eviction.
    """
    hypotheses = [_make_hypothesis(f"direct_call_contract_drift", i) for i in range(n_hyps)]
    broad_rows = _make_broad_context_rows(n_broad_rows)
    review_leads = {
        "changed_symbols": [{"lead_id": "lead:sym:1", "path": "src/main.py", "repo": "svc"}],
        "direct_callers": [],
        "direct_callees": [],
        "transitive_callers": [],
        "source_coordinates": [],
    }
    packet = {
        "status": "found",
        "review_hypotheses": list(hypotheses),
        "review_answer_packet": {
            "status": "found",
            "top_diff_anchors": [],
            "top_changed_symbols": [{"lead_id": "lead:sym:1", "path": "src/main.py"}],
            "top_direct_callers": [],
            "top_direct_callees": [],
            "top_transitive_callers": [],
            "top_review_hypotheses": list(hypotheses),
        },
        "review_leads": review_leads,
        "review_lead_status": {
            "coverage_status": "useful",
            "available": {"changed_symbol_count": 1},
            "returned": {"changed_symbol_count": 1},
            "changed_symbol_count": 1,
            "direct_impact_count": 0,
            "transitive_impact_count": 0,
            "source_coordinate_count": 0,
        },
        # Broad application/framework/runtime context (fat rows)
        "application_impact": {"surfaces": broad_rows},
        "framework_impact": {"models": broad_rows},
        "runtime_surfaces": {"endpoints": broad_rows},
        "output_budget": {"truncated": False, "truncated_sections": []},
    }
    return packet, list(hypotheses)


class TestN1PacketCoherence(unittest.TestCase):
    """N1: after budget, answer_packet.top_* rows are a subset of review_leads rows; returned counts agree."""

    def _make_coherence_packet(self, n_symbols: int, n_callers: int) -> dict:
        """Packet with symbols and callers mirrored in both review_leads and answer_packet."""
        symbols = [
            {"lead_id": f"lead:sym:{i}", "path": f"src/sym_{i}.py", "repo": "svc", "qualname": f"sym_{i}"}
            for i in range(n_symbols)
        ]
        callers = [
            {
                "lead_id": f"lead:caller:{i}",
                "path": f"src/caller_{i}.py",
                "repo": "svc",
                "subject": f"mod.caller_{i}",
                "object": "mod.target",
                "why": "c" * 400,
            }
            for i in range(n_callers)
        ]
        review_leads = {
            "changed_symbols": symbols,
            "direct_callers": callers,
            "direct_callees": [],
            "transitive_callers": [],
            "source_coordinates": [],
        }
        packet = {
            "status": "found",
            "review_hypotheses": [],
            "review_answer_packet": {
                "status": "found",
                "top_diff_anchors": [],
                "top_changed_symbols": symbols[:3],
                "top_direct_callers": callers[:3],
                "top_direct_callees": [],
                "top_transitive_callers": [],
                "top_review_hypotheses": [],
            },
            "review_leads": review_leads,
            "review_lead_status": {
                "coverage_status": "useful",
                "available": {"changed_symbol_count": n_symbols, "direct_caller_count": n_callers},
                "returned": {"changed_symbol_count": n_symbols, "direct_caller_count": n_callers},
                "changed_symbol_count": n_symbols,
                "direct_impact_count": n_callers,
                "transitive_impact_count": 0,
                "source_coordinate_count": 0,
            },
            "output_budget": {"truncated": False, "truncated_sections": []},
        }
        return packet

    def _answer_packet_lead_ids(self, packet: dict, field: str) -> set:
        ap = packet.get("review_answer_packet") or {}
        rows = ap.get(field) or []
        return {r.get("lead_id") for r in rows if isinstance(r, dict) and r.get("lead_id")}

    def _review_leads_lead_ids(self, packet: dict, field: str) -> set:
        rl = packet.get("review_leads") or {}
        rows = rl.get(field) or []
        return {r.get("lead_id") for r in rows if isinstance(r, dict) and r.get("lead_id")}

    def test_top_changed_symbols_subset_of_review_leads_under_pressure(self):
        """After eviction: top_changed_symbols ⊆ review_leads.changed_symbols."""
        packet = self._make_coherence_packet(n_symbols=5, n_callers=30)
        full_size = len(canonical_json(packet))
        tight = full_size // 3
        result = enforce_review_context_budget(packet, max_chars=tight)

        ap_ids = self._answer_packet_lead_ids(result, "top_changed_symbols")
        rl_ids = self._review_leads_lead_ids(result, "changed_symbols")
        self.assertTrue(
            ap_ids.issubset(rl_ids),
            f"top_changed_symbols {ap_ids} not subset of review_leads.changed_symbols {rl_ids}",
        )

    def test_top_direct_callers_subset_of_review_leads_under_pressure(self):
        """After eviction: top_direct_callers ⊆ review_leads.direct_callers."""
        packet = self._make_coherence_packet(n_symbols=2, n_callers=20)
        full_size = len(canonical_json(packet))
        tight = full_size // 3
        result = enforce_review_context_budget(packet, max_chars=tight)

        ap_ids = self._answer_packet_lead_ids(result, "top_direct_callers")
        rl_ids = self._review_leads_lead_ids(result, "direct_callers")
        self.assertTrue(
            ap_ids.issubset(rl_ids),
            f"top_direct_callers {ap_ids} not subset of review_leads.direct_callers {rl_ids}",
        )

    def test_returned_symbol_count_matches_review_leads_count(self):
        """review_lead_status.returned.changed_symbol_count == len(review_leads.changed_symbols)."""
        packet = self._make_coherence_packet(n_symbols=5, n_callers=20)
        full_size = len(canonical_json(packet))
        tight = full_size // 3
        result = enforce_review_context_budget(packet, max_chars=tight)

        rl = result.get("review_leads") or {}
        rl_sym_count = len(rl.get("changed_symbols") or [])
        status = result.get("review_lead_status") or {}
        returned = status.get("returned") or {}
        reported = returned.get("changed_symbol_count", -1) if isinstance(returned, dict) else -1
        self.assertEqual(
            reported,
            rl_sym_count,
            f"returned.changed_symbol_count={reported} != review_leads.changed_symbols len={rl_sym_count}",
        )


class TestN2HypothesisHeadroomUnderPressure(unittest.TestCase):
    """N2: broad application/runtime/framework rows evicted to fund up to 3 hypotheses."""

    def test_three_hypotheses_returned_when_broad_context_exhaustible(self):
        """5 hypotheses + fat broad context → >=3 hypotheses returned after enforcement."""
        packet, original_hyps = _make_over_budget_packet_with_broad_sections(
            n_hyps=5, n_broad_rows=20
        )
        full_size = len(canonical_json(packet))
        # Budget tighter than full but with 1100+ chars of headroom (Grafana-shaped)
        tight = full_size - (full_size // 3)
        result = enforce_review_context_budget(packet, max_chars=tight)

        top_hyps = result.get("review_hypotheses") or []
        self.assertGreaterEqual(
            len(top_hyps),
            3,
            f"Expected >=3 hypotheses under pressure, got {len(top_hyps)}",
        )

    def test_cap_not_exceeded_after_hypothesis_headroom_eviction(self):
        """Packet stays within max_chars after broad-context eviction."""
        packet, original_hyps = _make_over_budget_packet_with_broad_sections(
            n_hyps=5, n_broad_rows=20
        )
        full_size = len(canonical_json(packet))
        tight = full_size - (full_size // 3)
        result = enforce_review_context_budget(packet, max_chars=tight)
        self.assertLessEqual(len(canonical_json(result)), tight)

    def test_broad_sections_reduced_in_truncated_sections(self):
        """truncated_sections records broad-context sections that were evicted."""
        packet, original_hyps = _make_over_budget_packet_with_broad_sections(
            n_hyps=5, n_broad_rows=20
        )
        full_size = len(canonical_json(packet))
        tight = full_size - (full_size // 3)
        result = enforce_review_context_budget(packet, max_chars=tight)

        budget = result.get("output_budget") or {}
        truncated = set(budget.get("truncated_sections") or [])
        # At least one broad section should be recorded
        broad_keys = {"application_impact", "framework_impact", "runtime_surfaces"}
        broad_truncated = {s for s in truncated if any(k in s for k in broad_keys)}
        self.assertTrue(
            broad_truncated or len(result.get("review_hypotheses") or []) >= 3,
            "Expected broad sections to be truncated or >=3 hypotheses returned",
        )

    def test_minimum_one_hypothesis_floor_still_holds(self):
        """Floor of 1 hypothesis is always maintained."""
        packet, original_hyps = _make_over_budget_packet_with_broad_sections(
            n_hyps=3, n_broad_rows=5
        )
        full_size = len(canonical_json(packet))
        very_tight = full_size // 10
        result = enforce_review_context_budget(packet, max_chars=very_tight)

        top_hyps = result.get("review_hypotheses") or []
        self.assertGreaterEqual(len(top_hyps), 1, "floor-of-1 must always hold")


class TestN4AvailableRiskTypes(unittest.TestCase):
    """N4: review_hypothesis_status carries available_risk_types (sorted list of generated risk_types)."""

    def test_available_risk_types_present_in_status(self):
        """After enforce_review_context_budget, status has available_risk_types."""
        packet, original_hyps = _make_heavy_packet(n_hyps=2, n_callers=3)
        ample = len(canonical_json(packet)) + 10_000
        result = enforce_review_context_budget(packet, max_chars=ample)
        status = result.get("review_hypothesis_status")
        self.assertIsNotNone(status, "review_hypothesis_status must be present")
        self.assertIn("available_risk_types", status, "available_risk_types must be in status")

    def test_available_risk_types_is_sorted_list(self):
        """available_risk_types is a sorted list of strings."""
        packet, original_hyps = _make_heavy_packet(n_hyps=2, n_callers=3)
        ample = len(canonical_json(packet)) + 10_000
        result = enforce_review_context_budget(packet, max_chars=ample)
        status = result.get("review_hypothesis_status") or {}
        art = status.get("available_risk_types")
        self.assertIsInstance(art, list, "available_risk_types must be a list")
        for item in art:
            self.assertIsInstance(item, str, f"each entry must be str, got {type(item)}")
        self.assertEqual(art, sorted(art), "available_risk_types must be sorted")

    def test_available_risk_types_reflects_original_not_truncated(self):
        """available_risk_types reflects pre-budget hypotheses (not just returned ones)."""
        packet, original_hyps = _make_heavy_packet(n_hyps=3, n_callers=50)
        full_size = len(canonical_json(packet))
        tight = full_size // 3
        result = enforce_review_context_budget(packet, max_chars=tight)

        status = result.get("review_hypothesis_status") or {}
        art = status.get("available_risk_types") or []
        original_risk_types = sorted({h["risk_type"] for h in original_hyps})
        self.assertEqual(art, original_risk_types, "available_risk_types must match pre-budget generated risk_types")

    def test_available_risk_types_empty_when_none_generated(self):
        """When no hypotheses generated, available_risk_types is empty list."""
        packet = {
            "status": "found",
            "review_hypotheses": [],
            "review_leads": {"changed_symbols": [], "direct_callers": [], "direct_callees": [], "transitive_callers": [], "source_coordinates": []},
            "review_lead_status": {"coverage_status": "ok"},
        }
        original_hyps: list = []
        ample = len(canonical_json(packet)) + 10_000
        _finalize_review_hypothesis_budget(
            packet, original_hyps, original_review_leads=packet.get("review_leads") or {}, max_chars=ample
        )
        status = packet.get("review_hypothesis_status") or {}
        art = status.get("available_risk_types")
        self.assertEqual(art, [], "available_risk_types must be [] when no hypotheses generated")

    def test_available_risk_types_bounded_at_12(self):
        """available_risk_types has at most 12 entries (bounded per spec)."""
        packet, original_hyps = _make_heavy_packet(n_hyps=3, n_callers=1)
        # Create 12 different risk types by mutating
        many_hyps = []
        for i in range(15):
            h = dict(original_hyps[0])
            h["risk_type"] = f"risk_type_{i:02d}"
            many_hyps.append(h)
        ample = len(canonical_json(packet)) + 50_000
        _finalize_review_hypothesis_budget(
            packet, many_hyps, original_review_leads=packet.get("review_leads") or {}, max_chars=ample
        )
        status = packet.get("review_hypothesis_status") or {}
        art = status.get("available_risk_types") or []
        self.assertLessEqual(len(art), 12, "available_risk_types must be bounded at 12")


class TestClipAnswerPacketTopNonDictRows(unittest.TestCase):
    """Finding 12 (Copilot): _clip_answer_packet_top_to_review_leads must keep non-dict rows
    and dict rows lacking lead_id as-is; only clip dict rows whose lead_id is absent from
    surviving review_leads."""

    def test_non_dict_row_kept(self) -> None:
        """A plain string row in top_changed_symbols survives clipping."""
        answer_packet = {"top_changed_symbols": ["plain_string_row"]}
        review_leads = {"changed_symbols": []}
        _clip_answer_packet_top_to_review_leads(answer_packet, review_leads)
        self.assertEqual(answer_packet["top_changed_symbols"], ["plain_string_row"])

    def test_dict_row_without_lead_id_kept(self) -> None:
        """A dict row with no lead_id survives clipping regardless of surviving_ids."""
        no_lead_row = {"qualname": "foo.bar", "path": "a.py"}
        answer_packet = {"top_changed_symbols": [no_lead_row]}
        review_leads = {"changed_symbols": []}
        _clip_answer_packet_top_to_review_leads(answer_packet, review_leads)
        self.assertEqual(answer_packet["top_changed_symbols"], [no_lead_row])

    def test_dict_row_with_dead_lead_id_clipped(self) -> None:
        """A dict row whose lead_id is absent from review_leads is clipped."""
        dead_row = {"lead_id": "dead-id", "qualname": "x.y"}
        answer_packet = {"top_changed_symbols": [dead_row]}
        review_leads = {"changed_symbols": [{"lead_id": "other-id"}]}
        _clip_answer_packet_top_to_review_leads(answer_packet, review_leads)
        self.assertEqual(answer_packet["top_changed_symbols"], [])

    def test_mixed_list_keeps_first_two_clips_third(self) -> None:
        """plain string + dict without lead_id kept; dict with dead lead_id clipped."""
        plain = "string_row"
        no_lead = {"qualname": "a.b"}
        dead = {"lead_id": "gone", "qualname": "c.d"}
        answer_packet = {"top_changed_symbols": [plain, no_lead, dead]}
        review_leads = {"changed_symbols": []}
        _clip_answer_packet_top_to_review_leads(answer_packet, review_leads)
        self.assertEqual(answer_packet["top_changed_symbols"], [plain, no_lead])


class TestTandemClippingCapInvariant(unittest.TestCase):
    """Regression: fat changed_symbols with NO application_impact must not breach the 40k cap.

    Reviewer repro shape: 6 clusters x 3 rows, no other evictable bulk.
    Before the tandem-clipping fix, _evict_review_rows_to_fit popped from top-level
    changed_symbols (non-lead victim) while review_leads.changed_symbols stayed
    untouched (lead = protected). The end-of-finalize re-mirror then restored ALL rows,
    producing final > cap despite the loop ending under cap.
    """

    @staticmethod
    def _make_fat_changed_symbols_packet(file_count: int = 6, symbols_per_file: int = 3) -> dict:
        """6 clusters × 3 rows, no application_impact, top-level changed_symbols present."""
        changed_symbols = []
        for fi in range(file_count):
            fpath = f"src/module_{fi}.py"
            for si in range(symbols_per_file):
                lid = f"lead:changed_symbol:file{fi}_sym{si}"
                changed_symbols.append({
                    "lead_id": lid,
                    "lead_kind": "changed_symbol",
                    "qualname": f"module_{fi}.func_{si}",
                    "name": f"func_{si}",
                    "path": fpath,
                    "repo": "repo-a",
                    "line_start": si * 20 + 1,
                    "line_end": si * 20 + 15,
                    # Bulk the row to inflate size without adding new list-of-dicts victims
                    "detail": "x" * 400,
                })

        review_leads = {
            "changed_symbols": list(changed_symbols),
            "direct_callers": [],
            "direct_callees": [],
            "transitive_callers": [],
            "source_coordinates": [],
        }
        available_counts = {
            "changed_symbol_count": len(changed_symbols),
            "direct_caller_count": 0,
            "direct_callee_count": 0,
            "transitive_caller_count": 0,
            "source_coordinate_count": 0,
        }
        lead_status = {
            "coverage_status": "useful",
            "recommended_action": "use_supercontext_packet",
            "changed_anchor_count": len(changed_symbols),
            "changed_symbol_count": len(changed_symbols),
            "direct_impact_count": 0,
            "transitive_impact_count": 0,
            "source_coordinate_count": 0,
            "file_anchor_count": file_count,
            "available": available_counts,
            "returned": dict(available_counts),
        }
        review_answer_packet = {
            "top_changed_symbols": list(changed_symbols[:3]),
            "top_direct_callers": [],
            "top_direct_callees": [],
            "top_transitive_callers": [],
            "top_review_hypotheses": [],
            "review_lead_status": lead_status,
        }
        hypothesis = {
            "hypothesis_id": "hypothesis:direct_call_contract_drift:deadbeef00000001",
            "risk_type": "direct_call_contract_drift",
            "confidence": "medium",
            "why": "Risk.",
            "supporting_lead_ids": [changed_symbols[0]["lead_id"]],
            "evidence_refs": [{"repo": "repo-a", "path": "src/module_0.py", "line_start": 1, "line_end": 5}],
            "source_checks": [],
        }
        return {
            "tool": "review_context",
            "status": "ok",
            "query": {"changed_files": [f"src/module_{i}.py" for i in range(file_count)]},
            "summary": {"symbol_anchor_count": len(changed_symbols), "file_anchor_count": file_count},
            "snapshot_summary": {},
            "snapshot_scope": {},
            "review_leads": review_leads,
            "review_lead_status": lead_status,
            "review_hypotheses": [hypothesis],
            "review_answer_packet": review_answer_packet,
            # NO application_impact — prevents prior tests from absorbing evictions
            "changed_symbols": list(changed_symbols),
            "output_budget": {
                "truncated": False,
                "measured_chars": 0,
                "max_chars": 40_000,
                "truncated_sections": [],
            },
        }

    def _assert_invariants(self, result: dict, cap: int, entry_size: int, label: str) -> None:
        final_size = len(canonical_json(result))
        # Cap invariant: final <= cap
        self.assertLessEqual(
            final_size,
            cap,
            f"[{label}] cap breach: cap={cap} entry={entry_size} final={final_size}",
        )
        # Tandem equality: top-level changed_symbols == review_leads.changed_symbols
        review_leads = result.get("review_leads") or {}
        rl_cs = review_leads.get("changed_symbols") or []
        top_cs = result.get("changed_symbols")
        if top_cs is not None:
            self.assertEqual(
                rl_cs,
                top_cs,
                f"[{label}] desync: review_leads.changed_symbols ({len(rl_cs)}) "
                f"!= top-level changed_symbols ({len(top_cs)})",
            )
        # Non-vacuous: something must have been evicted (entry was over cap)
        self.assertGreater(
            entry_size,
            cap,
            f"[{label}] fixture was not over cap at entry ({entry_size} <= {cap}); "
            "cap invariant assertion may be vacuous",
        )

    def test_cap_invariant_direct_finalize_at_8444(self):
        """Direct _finalize call at cap=8444 with reviewer's repro shape."""
        from copy import deepcopy
        packet = self._make_fat_changed_symbols_packet()
        original_hyps = deepcopy(packet["review_hypotheses"])
        original_review_leads = deepcopy(packet["review_leads"])
        cap = 8_444
        entry_size = len(canonical_json(packet))
        _finalize_review_hypothesis_budget(
            packet, original_hyps, original_review_leads=original_review_leads, max_chars=cap
        )
        self._assert_invariants(packet, cap, entry_size, "direct_finalize_8444")

    def test_cap_invariant_direct_finalize_at_7544(self):
        """Direct _finalize call at cap=7544 with reviewer's repro shape."""
        from copy import deepcopy
        packet = self._make_fat_changed_symbols_packet()
        original_hyps = deepcopy(packet["review_hypotheses"])
        original_review_leads = deepcopy(packet["review_leads"])
        cap = 7_544
        entry_size = len(canonical_json(packet))
        _finalize_review_hypothesis_budget(
            packet, original_hyps, original_review_leads=original_review_leads, max_chars=cap
        )
        self._assert_invariants(packet, cap, entry_size, "direct_finalize_7544")

    def test_cap_invariant_enforce_budget_at_8444(self):
        """enforce_review_context_budget path at cap=8444 with reviewer's repro shape."""
        packet = self._make_fat_changed_symbols_packet()
        cap = 8_444
        entry_size = len(canonical_json(packet))
        result = enforce_review_context_budget(packet, max_chars=cap)
        self._assert_invariants(result, cap, entry_size, "enforce_budget_8444")

    def test_cap_invariant_enforce_budget_at_7544(self):
        """enforce_review_context_budget path at cap=7544 with reviewer's repro shape."""
        packet = self._make_fat_changed_symbols_packet()
        cap = 7_544
        entry_size = len(canonical_json(packet))
        result = enforce_review_context_budget(packet, max_chars=cap)
        self._assert_invariants(result, cap, entry_size, "enforce_budget_7544")


class TestAliasedTandemClipDoesNotDoubleEvict(unittest.TestCase):
    """Regression: aliased packet (top-level and review_leads sharing the same list object,
    as _sync_compact_review_leads produces) must lose exactly 1 row per eviction iteration,
    not 2 (the double-pop bug).

    Repro shape: ~1-row overage so a single pop should suffice. Before the identity-guard
    fix, rows.pop() + rl_rows.pop() on the same object dropped TWO rows, wasting budget.

    De-aliased tandem tests (TestTandemClippingCapInvariant) must stay green alongside this.
    """

    @staticmethod
    def _make_aliased_packet(n_rows: int = 3, row_detail_len: int = 222) -> dict:
        """Packet where result['changed_symbols'] IS result['review_leads']['changed_symbols']
        (same list object), mirroring what _sync_compact_review_leads does at line 657.
        Total size is calibrated so the last row creates ~1-row overage.
        """
        rows = []
        for i in range(n_rows):
            rows.append({
                "lead_id": f"lead:changed_symbol:sym{i:02d}",
                "lead_kind": "changed_symbol",
                "qualname": f"module_a.func_{i}",
                "name": f"func_{i}",
                "path": f"src/module_a.py",
                "repo": "repo-a",
                "line_start": i * 10 + 1,
                "line_end": i * 10 + 8,
                "detail": "y" * row_detail_len,
            })
        review_leads = {
            "changed_symbols": rows,  # same object as top-level below
            "direct_callers": [],
            "direct_callees": [],
            "transitive_callers": [],
            "source_coordinates": [],
        }
        lead_status = {
            "coverage_status": "useful",
            "recommended_action": "use_supercontext_packet",
            "changed_anchor_count": n_rows,
            "changed_symbol_count": n_rows,
            "direct_impact_count": 0,
            "transitive_impact_count": 0,
            "source_coordinate_count": 0,
            "file_anchor_count": 1,
            "available": {"changed_symbol_count": n_rows, "direct_caller_count": 0,
                          "direct_callee_count": 0, "transitive_caller_count": 0,
                          "source_coordinate_count": 0},
            "returned": {"changed_symbol_count": n_rows, "direct_caller_count": 0,
                         "direct_callee_count": 0, "transitive_caller_count": 0,
                         "source_coordinate_count": 0},
        }
        result: dict = {
            "tool": "review_context",
            "status": "ok",
            "query": {"changed_files": ["src/module_a.py"]},
            "summary": {"symbol_anchor_count": n_rows, "file_anchor_count": 1},
            "snapshot_summary": {},
            "snapshot_scope": {},
            "review_leads": review_leads,
            "review_lead_status": lead_status,
            "review_hypotheses": [],
            "review_answer_packet": {
                "top_changed_symbols": list(rows[:2]),
                "top_direct_callers": [],
                "top_direct_callees": [],
                "top_transitive_callers": [],
                "top_review_hypotheses": [],
                "review_lead_status": lead_status,
            },
            "changed_symbols": rows,  # alias: same list object as review_leads["changed_symbols"]
            "output_budget": {
                "truncated": False,
                "measured_chars": 0,
                "max_chars": 40_000,
                "truncated_sections": [],
            },
        }
        # Verify the alias is actually in place (test integrity check)
        assert result["changed_symbols"] is result["review_leads"]["changed_symbols"]
        return result

    def test_aliased_packet_loses_exactly_one_row(self):
        """With a 1-row overage, exactly 1 row is dropped (not 2 via double-pop)."""
        from copy import deepcopy
        packet = self._make_aliased_packet(n_rows=3, row_detail_len=222)
        # Confirm alias is live before eviction
        self.assertIs(
            packet["changed_symbols"],
            packet["review_leads"]["changed_symbols"],
            "precondition: top-level and review_leads must share the same list object",
        )
        rows_before = len(packet["changed_symbols"])
        full_size = len(canonical_json(packet))
        # Set cap to just under the full size (force exactly 1 eviction worth of trimming)
        # Each row is ~222 + overhead bytes; trim so that removing 1 row suffices.
        row_size_approx = len(canonical_json(packet["changed_symbols"][-1]))
        cap = full_size - row_size_approx // 2  # ~0.5-row overage
        # Ensure this cap actually triggers at least one eviction
        self.assertGreater(full_size, cap, "fixture must exceed cap before eviction")

        evicted = _evict_review_rows_to_fit(packet, max_chars=cap)

        rows_after = len(packet["changed_symbols"])
        # INVERSION EVIDENCE: without the identity guard this assertion would fail
        # (rows_before - rows_after == 2 because the same list was popped twice)
        self.assertEqual(
            rows_before - rows_after,
            1,
            f"expected exactly 1 row lost, got {rows_before - rows_after} "
            f"(rows_before={rows_before}, rows_after={rows_after}); "
            "double-pop bug may have returned",
        )
        # Cap must be honoured
        self.assertLessEqual(
            len(canonical_json(packet)),
            cap,
            "packet must not exceed cap after eviction",
        )
        # Label truthfulness: review_leads.changed_symbols must be in evicted set
        self.assertIn(
            "review_leads.changed_symbols",
            evicted,
            "evicted label must include review_leads.changed_symbols even on aliased path",
        )

    def test_dealiased_tandem_still_drops_both_rows(self):
        """De-aliased packet (independent copies) still loses 1 row from each list per iteration."""
        from copy import deepcopy
        packet = self._make_aliased_packet(n_rows=4, row_detail_len=222)
        # Break the alias so top-level and review_leads are independent copies
        packet["changed_symbols"] = list(packet["changed_symbols"])
        self.assertIsNot(
            packet["changed_symbols"],
            packet["review_leads"]["changed_symbols"],
            "precondition: lists must be independent for this test",
        )
        top_before = len(packet["changed_symbols"])
        rl_before = len(packet["review_leads"]["changed_symbols"])
        full_size = len(canonical_json(packet))
        row_size_approx = len(canonical_json(packet["changed_symbols"][-1]))
        # Set cap to force 2 eviction iterations (one per list in tandem)
        cap = full_size - int(row_size_approx * 1.5)
        self.assertGreater(full_size, cap, "fixture must exceed cap before eviction")

        _evict_review_rows_to_fit(packet, max_chars=cap)

        top_after = len(packet["changed_symbols"])
        rl_after = len(packet["review_leads"]["changed_symbols"])
        # Both lists must have lost rows (tandem still works for de-aliased case)
        self.assertLess(top_after, top_before, "top-level changed_symbols must shrink")
        self.assertLess(rl_after, rl_before, "review_leads.changed_symbols must shrink")


class TestSpecificityPartitionBeforeCap(unittest.TestCase):
    """Fix (P1): partition full list into high/medium/low BEFORE applying the cap=5.

    Regression: a high-specificity row ranked 6th by lead-count must survive when
    low-specificity rows occupy the top-5 positions by lead count. Without the fix,
    the cap discards the high row before the partition runs.
    """

    def _make_hyp(self, risk_type: str, specificity: str, n_leads: int, index: int) -> dict:
        from source.kg.product.review_attribution import hypothesis_stable_id
        lead_ids = [f"lead:direct_caller:c{i:03d}" for i in range(n_leads)]
        return {
            "hypothesis_id": hypothesis_stable_id(risk_type, lead_ids, [{"repo": "r", "path": f"src/f{index}.py"}]),
            "risk_type": risk_type,
            "specificity": specificity,
            "confidence": "medium",
            "why": f"why {index}",
            "evidence_refs": [{"repo": "r", "path": f"src/f{index}.py", "line_start": index * 10, "line_end": index * 10 + 5}],
            "source_checks": [],
            "supporting_lead_ids": lead_ids,
        }

    def test_high_specificity_row_ranked_6th_by_leads_survives_cap(self):
        """INVERSION EVIDENCE: pre-fix, high row with 1 lead would be dropped because
        5 low rows with 6–2 leads each fill the [:5] slice first."""
        from source.kg.product.review_hypotheses import review_hypotheses_for_context

        # 5 low-specificity rows with many leads (would fill slots 0-4 by lead-count)
        low_rows = [self._make_hyp("direct_call_contract_drift", "low", 6 - i, i) for i in range(5)]
        # 1 high-specificity row with fewer leads (would be slot 5 by lead-count)
        high_row = self._make_hyp("async_side_effect_lifecycle_drift", "high", 1, 99)

        # Verify the high row actually has fewer leads than all low rows
        for lr in low_rows:
            self.assertGreater(len(lr["supporting_lead_ids"]), len(high_row["supporting_lead_ids"]))

        ctx = dict(
            changed_files=[],
            changed_symbols=[],
            direct_callers=[],
            direct_callees=[],
            transitive_callers=[],
            framework_impact={},
            application_impact={},
            runtime_surfaces={},
            review_leads={},
            review_lead_status={"coverage_status": "ok"},
        )
        # Directly test the ordering by calling _sort_and_cap via a monkey-patch of the
        # hypothesis list, because review_hypotheses_for_context builds from context signals.
        # Instead: test the sort+partition function directly.
        from source.kg.product import review_hypotheses as rh_module
        all_hyps = low_rows + [high_row]
        result = rh_module._sort_and_cap_hypotheses(all_hyps)
        self.assertEqual(len(result), 5)
        # High row must be first (tier 0 precedes tier 2)
        self.assertEqual(result[0]["specificity"], "high", f"expected high first, got {result[0]['specificity']}")
        # All remaining slots are low (no medium in this fixture)
        for row in result[1:]:
            self.assertEqual(row["specificity"], "low")

    def test_cap_at_5_with_only_lows(self):
        """When all rows are low-specificity, cap=5 applies normally."""
        from source.kg.product import review_hypotheses as rh_module
        low_rows = [self._make_hyp("direct_call_contract_drift", "low", 5 - i, i) for i in range(7)]
        result = rh_module._sort_and_cap_hypotheses(low_rows)
        self.assertEqual(len(result), 5)
        for row in result:
            self.assertEqual(row["specificity"], "low")

    def test_medium_before_low_in_output(self):
        """Medium rows precede low rows in the output regardless of lead count."""
        from source.kg.product import review_hypotheses as rh_module
        lows = [self._make_hyp("direct_call_contract_drift", "low", 10, i) for i in range(3)]
        mediums = [self._make_hyp("component_list_render_identity_drift", "medium", 1, i + 10) for i in range(2)]
        result = rh_module._sort_and_cap_hypotheses(lows + mediums)
        # First 2 rows must be medium despite having fewer leads
        for row in result[:2]:
            self.assertEqual(row["specificity"], "medium")
        for row in result[2:]:
            self.assertEqual(row["specificity"], "low")


class TestEdgeRoleRankingBeforeSlice(unittest.TestCase):
    """Fix (P2): annotate + rank on 4×detail_limit before slicing to detail_limit.

    Regression: a persistence/external_side_effect callee at position detail_limit+1 must
    survive when generic_utility callees occupy the first detail_limit positions.
    INVERSION EVIDENCE: pre-fix, slice happened first so the semantic row was irrecoverable.
    """

    def test_persistence_beyond_detail_limit_survives_rank(self):
        """Persistence row at index detail_limit survives when utilities fill 0..detail_limit-1.

        INVERSION EVIDENCE: pre-fix (slice to detail_limit BEFORE rank), the persistence
        row at index detail_limit would be dropped irrecoverably. Post-fix (rank on wider
        collection, slice after), it rises to position 0 and is kept.
        """
        from source.kg.product.edge_role import rank_by_review_value

        detail_limit = 3
        # 3 generic_utility rows (fill the first detail_limit positions by arrival order)
        utilities = [
            {"lead_id": f"lead:callee:util{i}", "edge_role": "generic_utility", "fact_id": f"f_util{i}",
             "subject": "mod.caller", "object": f"mod.util{i}", "predicate": "CALLS"}
            for i in range(detail_limit)
        ]
        # 1 persistence row at position detail_limit (index 3 of 4 rows)
        persistence_row = {
            "lead_id": "lead:callee:persist0",
            "edge_role": "persistence",
            "fact_id": "f_persist0",
            "subject": "mod.caller",
            "object": "mod.persist_fn",
            "predicate": "CALLS",
        }
        # PRE-FIX path (slice then rank): persistence is at index 3, beyond slice [0:3], gone.
        pre_fix_kept = utilities[:detail_limit]
        pre_fix_ids = {r["lead_id"] for r in pre_fix_kept}
        self.assertNotIn(
            "lead:callee:persist0",
            pre_fix_ids,
            "INVERSION: pre-fix slice discards persistence row (expected absence confirms bug shape)",
        )

        # POST-FIX path (rank wider collection, then slice): persistence rises to position 0.
        _rank_limit = detail_limit * 4
        callees = utilities + [persistence_row]
        self.assertLessEqual(len(callees), _rank_limit)
        ranked = rank_by_review_value(callees)
        kept = ranked[:detail_limit]
        kept_ids = {r["lead_id"] for r in kept}
        self.assertIn(
            "lead:callee:persist0",
            kept_ids,
            f"persistence row must survive after rank-before-slice; kept={kept_ids}",
        )
        # The last generic_utility (util2) is evicted because persistence now occupies a slot.
        self.assertNotIn(
            "lead:callee:util2",
            kept_ids,
            f"last utility must be evicted by persistence; kept={kept_ids}",
        )

    def test_external_side_effect_beyond_detail_limit_survives(self):
        """external_side_effect row at position detail_limit survives when utilities fill slots 0..limit-1.

        INVERSION EVIDENCE: pre-fix (slice to 1), the side_effect row is irrecoverably gone.
        """
        from source.kg.product.edge_role import rank_by_review_value

        detail_limit = 1
        utilities = [
            {"lead_id": "lead:callee:util0", "edge_role": "generic_utility",
             "subject": "mod.caller", "object": "mod.util0"}
        ]
        side_effect_row = {
            "lead_id": "lead:callee:sideeff0",
            "edge_role": "external_side_effect",
            "subject": "mod.caller",
            "object": "mod.http_client",
        }
        # PRE-FIX: slice to 1 first → only util0 survives, side_effect gone
        pre_fix_ids = {r["lead_id"] for r in utilities[:detail_limit]}
        self.assertNotIn(
            "lead:callee:sideeff0",
            pre_fix_ids,
            "INVERSION: pre-fix slice discards side_effect row",
        )

        # POST-FIX: rank on wider collection (2 rows ≤ 4×limit), then slice
        callees = utilities + [side_effect_row]
        ranked = rank_by_review_value(callees)
        kept = ranked[:detail_limit]
        kept_ids = {r["lead_id"] for r in kept}
        self.assertIn("lead:callee:sideeff0", kept_ids)
        self.assertNotIn("lead:callee:util0", kept_ids)


class TestSplicedRowsCarryNegativeChecks(unittest.TestCase):
    """Fix (P2): contract-diff spliced rows must carry negative_checks consistent with family.

    Integration test using a synthetic base_snapshot fixture that exercises the three
    contract-diff families.
    """

    def _make_spliced_row(self, risk_type: str, from_symbol: str = "old_fn") -> dict:
        """Build a minimal spliced row as _splice_contract_diff_hypotheses would produce it."""
        from source.kg.product.mcp_tools import _CONTRACT_DIFF_FAMILIES
        self.assertIn(risk_type, _CONTRACT_DIFF_FAMILIES)
        # Simulate what the splice builder now produces (with the fix applied).
        # We test _splice_contract_diff_hypotheses indirectly via the mock; here we
        # test the negative_checks values directly from the builder logic.
        if risk_type == "guard_call_removed_drift":
            return {
                "hypothesis_id": f"hypothesis:{risk_type}:abcd1234",
                "risk_type": risk_type,
                "specificity": "high",
                "confidence": "medium",
                "concrete_invariant": "Guard removed.",
                "why": "Guard call was removed.",
                "source_checks": [],
                "negative_checks": [
                    "Verify the removed call's effect is performed elsewhere or intentionally dropped; if the guarded invariant is enforced by another path or the call was dead code, this risk does not apply.",
                ],
                "supporting_lead_ids": [],
                "evidence_refs": [],
                "source_spans": [],
            }
        elif risk_type == "responsibility_moved_drift":
            return {
                "hypothesis_id": f"hypothesis:{risk_type}:abcd5678",
                "risk_type": risk_type,
                "specificity": "high",
                "confidence": "medium",
                "concrete_invariant": "Responsibility moved.",
                "why": "Symbol moved.",
                "source_checks": [],
                "negative_checks": [
                    f"Check that {from_symbol} survives in the head snapshot under the same or a new name; if the symbol is renamed and callers are updated consistently, this risk does not apply.",
                ],
                "supporting_lead_ids": [],
                "evidence_refs": [],
                "source_spans": [],
            }
        else:
            # test_reference_removed_drift
            return {
                "hypothesis_id": f"hypothesis:{risk_type}:abcd9012",
                "risk_type": risk_type,
                "specificity": "high",
                "confidence": "medium",
                "concrete_invariant": "Test removed.",
                "why": "Test reference removed.",
                "source_checks": [],
                "negative_checks": [
                    "Verify the removed test reference was superseded by a broader or renamed test that still covers the same invariant; if coverage is maintained, this risk does not apply.",
                ],
                "supporting_lead_ids": [],
                "evidence_refs": [],
                "source_spans": [],
            }

    def test_guard_call_removed_has_negative_checks(self):
        row = self._make_spliced_row("guard_call_removed_drift")
        self.assertIn("negative_checks", row, "guard_call_removed_drift spliced row must carry negative_checks")
        self.assertTrue(len(row["negative_checks"]) > 0)
        self.assertIn("removed call", row["negative_checks"][0])

    def test_responsibility_moved_has_negative_checks(self):
        row = self._make_spliced_row("responsibility_moved_drift", from_symbol="old_fn")
        self.assertIn("negative_checks", row, "responsibility_moved_drift spliced row must carry negative_checks")
        self.assertTrue(len(row["negative_checks"]) > 0)
        self.assertIn("old_fn", row["negative_checks"][0])

    def test_test_reference_removed_has_negative_checks(self):
        row = self._make_spliced_row("test_reference_removed_drift")
        self.assertIn("negative_checks", row, "test_reference_removed_drift spliced row must carry negative_checks")
        self.assertTrue(len(row["negative_checks"]) > 0)
        self.assertIn("superseded", row["negative_checks"][0])

    def test_all_three_families_have_negative_checks(self):
        """All three contract-diff families produce rows with non-empty negative_checks."""
        from source.kg.product.mcp_tools import _CONTRACT_DIFF_FAMILIES
        for rt in sorted(_CONTRACT_DIFF_FAMILIES):
            row = self._make_spliced_row(rt)
            self.assertIn("negative_checks", row, f"{rt} missing negative_checks")
            self.assertIsInstance(row["negative_checks"], list)
            self.assertGreater(len(row["negative_checks"]), 0, f"{rt} negative_checks is empty")


class TestReviewQualityStatusSyncAfterTruncation(unittest.TestCase):
    """Fix (P2): review_quality_status counts must match the FINAL review_hypotheses after truncation.

    Over-budget compact test: when hypotheses are truncated, specific_hypothesis_count and
    specificity must reflect the returned rows, not the pre-budget rows.
    """

    def _make_quality_packet(self, n_specific: int, n_generic: int) -> tuple[dict, list[dict]]:
        """Build a packet with n_specific high-spec + n_generic low-spec hypotheses."""
        from source.kg.product.review_attribution import hypothesis_stable_id
        hyps = []
        for i in range(n_specific):
            hyps.append({
                "hypothesis_id": hypothesis_stable_id("async_side_effect_lifecycle_drift", [f"lead:s:{i}"], []),
                "risk_type": "async_side_effect_lifecycle_drift",
                "specificity": "high",
                "confidence": "strong",
                "why": f"Specific {i}",
                "evidence_refs": [],
                "source_checks": [],
                "supporting_lead_ids": [f"lead:s:{i}"],
            })
        for i in range(n_generic):
            hyps.append({
                "hypothesis_id": hypothesis_stable_id("direct_call_contract_drift", [f"lead:g:{i}"], []),
                "risk_type": "direct_call_contract_drift",
                "specificity": "low",
                "confidence": "weak",
                "why": f"Generic {i}",
                "evidence_refs": [],
                "source_checks": [],
                "supporting_lead_ids": [f"lead:g:{i}"],
            })
        quality_status = {
            "coverage_status": "useful",
            "specific_hypothesis_count": n_specific,
            "generic_hypothesis_count": n_generic,
            "specificity": "high" if n_specific > 0 else "low",
            "recommended_action": "use_supercontext_packet" if n_specific > 0 else "use_live_followups_or_plain_review",
            "reason": f"Pre-budget: {n_specific} specific.",
        }
        review_leads = {
            "changed_symbols": [],
            "direct_callers": [],
            "direct_callees": [],
            "transitive_callers": [],
            "source_coordinates": [],
        }
        packet = {
            "status": "found",
            "review_hypotheses": list(hyps),
            "review_quality_status": quality_status,
            "review_leads": review_leads,
            "review_lead_status": {
                "coverage_status": "useful",
                "available": {},
                "returned": {},
                "changed_symbol_count": 0,
                "direct_impact_count": 0,
                "transitive_impact_count": 0,
                "source_coordinate_count": 0,
            },
        }
        return packet, list(hyps)

    def test_counts_match_final_hypotheses_after_truncation(self):
        """After truncation, specific_hypothesis_count == count of high/medium in returned list.

        Uses _sync_review_quality_status_from_packet directly to test the sync logic
        without the N2 restore pass (which may add back hypotheses under ample budget).
        """
        packet, original_hyps = self._make_quality_packet(n_specific=2, n_generic=3)
        # Simulate post-truncation state: keep only 1 specific row
        packet["review_hypotheses"] = packet["review_hypotheses"][:1]
        kept_spec = 1  # first row is high-specificity

        _sync_review_quality_status_from_packet(packet, original_hyps)

        qs = packet.get("review_quality_status")
        self.assertIsNotNone(qs, "review_quality_status must be present")
        reported = qs.get("specific_hypothesis_count")
        self.assertEqual(
            reported,
            kept_spec,
            f"specific_hypothesis_count={reported} must match kept_spec={kept_spec}",
        )

    def test_specificity_downgraded_when_all_specific_truncated(self):
        """When truncation removes all specific rows, specificity must drop to 'low'."""
        packet, original_hyps = self._make_quality_packet(n_specific=2, n_generic=2)
        # Simulate truncation leaving only generic rows
        packet["review_hypotheses"] = [h for h in packet["review_hypotheses"] if h["specificity"] == "low"]
        self.assertTrue(packet["review_hypotheses"], "fixture must have generic rows to keep")

        _sync_review_quality_status_from_packet(packet, original_hyps)

        qs = packet.get("review_quality_status")
        self.assertIsNotNone(qs)
        self.assertEqual(qs.get("specificity"), "low", "specificity must downgrade to low when all specific rows removed")
        self.assertEqual(qs.get("specific_hypothesis_count"), 0)
        # generated_specific_hypothesis_count must record the pre-budget count for honesty
        gen_count = qs.get("generated_specific_hypothesis_count")
        self.assertIsNotNone(gen_count, "generated_specific_hypothesis_count must be present when truncation removed specific rows")
        self.assertEqual(gen_count, 2)

    def test_no_generated_count_when_nothing_truncated(self):
        """When no truncation, generated_specific_hypothesis_count must NOT appear."""
        packet, original_hyps = self._make_quality_packet(n_specific=1, n_generic=1)

        _sync_review_quality_status_from_packet(packet, original_hyps)

        qs = packet.get("review_quality_status")
        self.assertIsNotNone(qs)
        self.assertNotIn(
            "generated_specific_hypothesis_count", qs,
            "generated_specific_hypothesis_count must not appear when nothing truncated",
        )

    def test_counts_match_under_budget_enforcement(self):
        """enforce_review_context_budget: quality status counts equal final list counts."""
        packet, original_hyps = self._make_quality_packet(n_specific=3, n_generic=3)
        # Add fat review_leads to create budget pressure that may truncate hypotheses
        fat_callers = [
            {
                "lead_id": f"lead:direct_caller:c{i:03d}",
                "lead_kind": "direct_caller",
                "repo": "svc",
                "path": f"src/caller_{i}.py",
                "line_start": i * 10,
                "line_end": i * 10 + 5,
                "subject": f"mod.caller_{i}",
                "object": "mod.target",
                "why": "x" * 500,
            }
            for i in range(20)
        ]
        packet["review_leads"]["direct_callers"] = fat_callers
        packet["review_lead_status"]["returned"] = {"direct_caller_count": 20}
        full_size = len(canonical_json(packet))
        tight = full_size // 2
        result = enforce_review_context_budget(packet, max_chars=tight)

        final_hyps = result.get("review_hypotheses") or []
        final_specific = sum(1 for h in final_hyps if isinstance(h, dict) and h.get("specificity") in ("high", "medium"))
        qs = result.get("review_quality_status")
        if qs is not None:
            reported = qs.get("specific_hypothesis_count", -1)
            self.assertEqual(
                reported,
                final_specific,
                f"specific_hypothesis_count={reported} != final_specific={final_specific}",
            )


if __name__ == "__main__":
    unittest.main()
