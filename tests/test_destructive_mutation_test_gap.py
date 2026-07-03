"""Tests for destructive_mutation_test_gap hypothesis family (Task A1).

Tests:
1. Changed symbol has direct_callee with call ending in ".deleteMany", no test file → fires
2. Changed symbol has direct_callee with call ending in ".delete", test file in changed_files → no hypothesis
3. Changed symbol has NO delete-suffix callee → no hypothesis
4. Changed symbol has direct_callee with call "prisma.calendarCache.deleteMany" → fires (concrete suffix)
5. Callee subject doesn't match changed symbol → no hypothesis
"""
from __future__ import annotations

import unittest

from source.kg.product.review_hypotheses import review_hypotheses_for_context


def _base_context(**overrides):
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
        risk_signals=[],
    )
    ctx.update(overrides)
    return ctx


def _sym(qualname: str, path: str, lead_id: str | None = None) -> dict:
    row = {
        "qualname": qualname,
        "display_name": f"mod.{qualname}",
        "qualified_name": f"mod.{qualname}",
        "kind": "function",
        "path": path,
    }
    if lead_id:
        row["lead_id"] = lead_id
    return row


def _callee_row(subject: str, call: str, path: str = "src/handler.ts", line: int = 10) -> dict:
    """Build a direct_callees row with qualifier.call and evidence bytes_ref."""
    return {
        "subject": subject,
        "object": call.rsplit(".", 1)[-1] if "." in call else call,
        "qualifier": {"call": call},
        "evidence": [
            {
                "bytes_ref": {
                    "repo": "myrepo",
                    "path": path,
                    "line_start": line,
                    "line_end": line,
                }
            }
        ],
    }


class TestDestructiveMutationTestGap(unittest.TestCase):
    """Family: destructive_mutation_test_gap"""

    def _call(self, **ctx_overrides):
        return review_hypotheses_for_context(**_base_context(**ctx_overrides))

    def test_positive_delete_many_no_test_file_fires(self):
        """Case 1: changed symbol calls .deleteMany, no test file → fires."""
        sym = _sym("deleteCalendarCache", "src/deleteCache.handler.ts", lead_id="lead-dm-1")
        callee = _callee_row("deleteCalendarCache", "prisma.calendarCache.deleteMany", path="src/deleteCache.handler.ts")
        hypotheses = self._call(
            changed_files=["src/deleteCache.handler.ts"],
            changed_symbols=[sym],
            direct_callees=[callee],
            review_leads={
                "changed_symbols": [{"lead_id": "lead-dm-1", "path": "src/deleteCache.handler.ts"}],
            },
        )
        risk_types = [h["risk_type"] for h in hypotheses]
        self.assertIn("destructive_mutation_test_gap", risk_types)
        h = next(h for h in hypotheses if h["risk_type"] == "destructive_mutation_test_gap")
        self.assertIn("concrete_invariant", h)
        self.assertIn("hypothesis_id", h)
        self.assertTrue(h["hypothesis_id"].startswith("hypothesis:destructive_mutation_test_gap:"))

    def test_negative_delete_with_test_file_absent(self):
        """Case 2: changed symbol calls .delete, test file in changed_files → no hypothesis."""
        sym = _sym("deleteEntry", "src/entry.ts", lead_id="lead-dm-2")
        callee = _callee_row("deleteEntry", "db.record.delete", path="src/entry.ts")
        hypotheses = self._call(
            changed_files=["src/entry.ts", "tests/entry.test.ts"],  # test file present
            changed_symbols=[sym],
            direct_callees=[callee],
            review_leads={
                "changed_symbols": [{"lead_id": "lead-dm-2", "path": "src/entry.ts"}],
            },
        )
        risk_types = [h["risk_type"] for h in hypotheses]
        self.assertNotIn("destructive_mutation_test_gap", risk_types)

    def test_negative_no_delete_suffix_callee_absent(self):
        """Case 3: changed symbol has NO delete-suffix callee → no hypothesis."""
        sym = _sym("updateRecord", "src/update.ts", lead_id="lead-dm-3")
        callee = _callee_row("updateRecord", "prisma.record.update", path="src/update.ts")
        hypotheses = self._call(
            changed_files=["src/update.ts"],
            changed_symbols=[sym],
            direct_callees=[callee],
            review_leads={
                "changed_symbols": [{"lead_id": "lead-dm-3", "path": "src/update.ts"}],
            },
        )
        risk_types = [h["risk_type"] for h in hypotheses]
        self.assertNotIn("destructive_mutation_test_gap", risk_types)

    def test_positive_prisma_calendar_cache_delete_many_fires(self):
        """Case 4: callee call="prisma.calendarCache.deleteMany" → fires (concrete suffix)."""
        sym = _sym("deleteCache", "packages/trpc/viewer/calendars/deleteCache.handler.ts", lead_id="lead-dm-4")
        callee = _callee_row(
            "deleteCache",
            "prisma.calendarCache.deleteMany",
            path="packages/trpc/viewer/calendars/deleteCache.handler.ts",
            line=25,
        )
        hypotheses = self._call(
            changed_files=["packages/trpc/viewer/calendars/deleteCache.handler.ts"],
            changed_symbols=[sym],
            direct_callees=[callee],
            review_leads={
                "changed_symbols": [
                    {
                        "lead_id": "lead-dm-4",
                        "path": "packages/trpc/viewer/calendars/deleteCache.handler.ts",
                    }
                ],
            },
        )
        risk_types = [h["risk_type"] for h in hypotheses]
        self.assertIn("destructive_mutation_test_gap", risk_types)
        h = next(h for h in hypotheses if h["risk_type"] == "destructive_mutation_test_gap")
        # evidence_refs should carry call info
        self.assertTrue(h.get("evidence_refs"), "evidence_refs must be non-empty")
        ref = h["evidence_refs"][0]
        self.assertEqual(ref.get("call"), "prisma.calendarCache.deleteMany")

    def test_negative_callee_subject_mismatch_absent(self):
        """Case 5: callee subject doesn't match changed symbol → no hypothesis."""
        sym = _sym("deleteEntry", "src/entry.ts", lead_id="lead-dm-5")
        # subject is a completely different symbol name
        callee = _callee_row("UNRELATED_FUNCTION", "prisma.record.deleteMany", path="src/other.ts")
        hypotheses = self._call(
            changed_files=["src/entry.ts"],
            changed_symbols=[sym],
            direct_callees=[callee],
            review_leads={
                "changed_symbols": [{"lead_id": "lead-dm-5", "path": "src/entry.ts"}],
            },
        )
        risk_types = [h["risk_type"] for h in hypotheses]
        self.assertNotIn("destructive_mutation_test_gap", risk_types)

    def test_positive_destroy_suffix_fires(self):
        """.destroy suffix also triggers."""
        sym = _sym("removeAccount", "src/account.ts", lead_id="lead-dm-6")
        callee = _callee_row("removeAccount", "account.destroy", path="src/account.ts")
        hypotheses = self._call(
            changed_files=["src/account.ts"],
            changed_symbols=[sym],
            direct_callees=[callee],
        )
        risk_types = [h["risk_type"] for h in hypotheses]
        self.assertIn("destructive_mutation_test_gap", risk_types)

    def test_positive_delete_one_suffix_fires(self):
        """.deleteOne suffix also triggers."""
        sym = _sym("removeSingle", "src/single.ts")
        callee = _callee_row("removeSingle", "db.records.deleteOne", path="src/single.ts")
        hypotheses = self._call(
            changed_files=["src/single.ts"],
            changed_symbols=[sym],
            direct_callees=[callee],
        )
        risk_types = [h["risk_type"] for h in hypotheses]
        self.assertIn("destructive_mutation_test_gap", risk_types)

    def test_positive_bare_delete_fires(self):
        """Bare 'delete' call (no dot prefix) also triggers."""
        sym = _sym("dropItem", "src/item.ts")
        callee = _callee_row("dropItem", "delete", path="src/item.ts")
        hypotheses = self._call(
            changed_files=["src/item.ts"],
            changed_symbols=[sym],
            direct_callees=[callee],
        )
        risk_types = [h["risk_type"] for h in hypotheses]
        self.assertIn("destructive_mutation_test_gap", risk_types)

    def test_lead_matching_does_not_overmatch_shared_short_names(self):
        """Regression: pr-7232 rank flip.

        The callee subject's short segment ("handler") must only claim leads
        anchored to the call-site path.  Changed symbols named "handler" in
        unrelated files must not contribute supporting_lead_ids — bare
        short-name matching inflated leads 1 -> 3 on pr-7232 and pushed this
        family above async_side_effect_lifecycle_drift.
        """
        cancel_sym = {
            "qualname": "handler",
            "qualified_name": "features.bookings.handleCancelBooking.handler",
            "kind": "function",
            "path": "src/handleCancelBooking.ts",
            "lead_id": "lead-cancel",
        }
        bookings_sym = {
            "qualname": "handler",
            "qualified_name": "trpc.viewer.bookings.handler",
            "kind": "function",
            "path": "src/bookings.tsx",
            "lead_id": "lead-bookings",
        }
        workflows_sym = {
            "qualname": "handler",
            "qualified_name": "trpc.viewer.workflows.handler",
            "kind": "function",
            "path": "src/workflows.tsx",
            "lead_id": "lead-workflows",
        }
        callee = _callee_row(
            "features.bookings.handleCancelBooking.handler",
            "prisma.attendee.deleteMany",
            path="src/handleCancelBooking.ts",
        )
        hypotheses = self._call(
            changed_files=["src/handleCancelBooking.ts", "src/bookings.tsx", "src/workflows.tsx"],
            changed_symbols=[cancel_sym, bookings_sym, workflows_sym],
            direct_callees=[callee],
            review_leads={
                "changed_symbols": [
                    {"lead_id": "lead-cancel", "qualname": "handler", "path": "src/handleCancelBooking.ts"},
                    {"lead_id": "lead-bookings", "qualname": "handler", "path": "src/bookings.tsx"},
                    {"lead_id": "lead-workflows", "qualname": "handler", "path": "src/workflows.tsx"},
                ],
            },
        )
        h = next(h for h in hypotheses if h["risk_type"] == "destructive_mutation_test_gap")
        self.assertEqual(h["supporting_lead_ids"], ["lead-cancel"])


if __name__ == "__main__":
    unittest.main()
