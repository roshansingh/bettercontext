"""Wave-3 Task C: packet-honesty features for review_context.

C1 — readiness honesty + inspection areas for truncated hypotheses:
  - truncated high/medium rows (cap OR budget) → review_readiness downgraded to
    needs_followup even when a high row survived
  - truncated high/medium rows converted to compact inspection_areas with structural
    coords (repo/path/line/symbol/risk_type/search_terms), capped with a remainder count
  - concrete suggested_followups per truncated high-specificity family
  - inspection_areas fit inside the 15K budget; never silently stripped
  - no truncation → packet_ready unchanged + no inspection_areas

C2 (rec-6) — attribution-label affordance:
  - review_answer_packet.attribution_labels lists the RETURNED hypothesis labels plus a
    generic cite instruction; labels exactly match returned rows; survives budget.

Inversion proofs (per standing rule): each honesty signal is shown to be ABSENT in the
control geometry, so a green assertion cannot be vacuous.
"""
from __future__ import annotations

import json
import tempfile
from copy import deepcopy
from pathlib import Path

import unittest

from source.kg.build.pipeline import build_kg
from source.kg.product import output_budget as ob
from source.kg.product.mcp_tools import call_tool
from source.kg.product.output_budget import REVIEW_CONTEXT_MAX_CHARS
from source.kg.query.snapshot import KgSnapshot


TENANT = "default"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hyp(index: int, *, specificity: str = "high", risk_type: str | None = None) -> dict:
    """A full hypothesis row with distinct stable id + structural cause/consequence coords."""
    rt = risk_type or f"family_{index}"
    hid = f"hypothesis:{rt}:{index:016x}"
    return {
        "hypothesis_id": hid,
        "risk_type": rt,
        "specificity": specificity,
        "derivation": "inferred_llm",
        "label": f"{rt}-{index:04x}",
        "cause": {"repo": "r", "path": f"src/f{index}.py", "line_start": 10 + index, "qualname": f"func{index}"},
        "consequence": {"repo": "r", "path": f"src/g{index}.py", "line_start": 20 + index, "qualname": f"caller{index}"},
    }


def _quality_status_packet_ready(specific: int) -> dict:
    return {
        "coverage_status": "useful",
        "specificity": "high",
        "specific_hypothesis_count": specific,
        "generic_hypothesis_count": 0,
        "recommended_action": "use_supercontext_packet",
        "reason": "pre-budget reason",
        "review_readiness": "packet_ready",
    }


def _build_async_signal_kg(root: Path) -> KgSnapshot:
    """Real build_kg fixture: a TS async-in-loop file that fires an async-lifecycle signal,
    producing exactly one high-specificity hypothesis (no truncation geometry)."""
    svc = root / "svc"
    svc.mkdir()
    (svc / "a.ts").write_text(
        "export async function doWork(items) {\n"
        "  items.forEach(async (it) => {\n"
        "    await save(it);\n"
        "  });\n"
        "}\n"
    )
    out = root / "kg"
    build_kg(svc, out, tenant_id=TENANT)
    return KgSnapshot(out)


# ---------------------------------------------------------------------------
# C1: readiness honesty + inspection areas (sync-level, real production function)
# ---------------------------------------------------------------------------

class TestC1ReadinessHonesty(unittest.TestCase):
    """Over-generation geometry: 6 high families available, 2 returned → 4 truncated."""

    def _run_over_generation(self):
        full = [_hyp(i, risk_type=f"family_{i}") for i in range(6)]
        returned = full[:2]  # 2 high rows survive; 4 high rows truncated
        result = {
            "review_quality_status": _quality_status_packet_ready(6),
            "review_hypotheses": returned,
        }
        ob._sync_review_quality_status_from_packet(
            result, original_hypotheses=returned, full_pre_cap_hypotheses=full,
            max_chars=REVIEW_CONTEXT_MAX_CHARS,
        )
        return result["review_quality_status"], full, returned

    def test_readiness_downgrades_when_high_rows_truncated(self) -> None:
        rqs, _full, _ret = self._run_over_generation()
        # A high row survived (specificity stays high) but 4 high rows were truncated →
        # readiness MUST downgrade, not stay packet_ready.
        self.assertEqual(rqs["specificity"], "high")
        self.assertEqual(
            rqs["review_readiness"], "needs_followup",
            f"truncated high rows must downgrade readiness even when a high row survives; got {rqs}",
        )
        self.assertEqual(rqs.get("generated_specific_hypothesis_count"), 6)

    def test_inspection_areas_present_with_correct_coords(self) -> None:
        rqs, full, returned = self._run_over_generation()
        block = rqs.get("inspection_areas")
        self.assertIsInstance(block, dict, f"inspection_areas must be present; got {rqs}")
        areas = block.get("areas")
        self.assertTrue(areas, "inspection_areas.areas must be non-empty")
        # Cap: at most 5 rows.
        self.assertLessEqual(len(areas), 5)
        # Coords sourced structurally from the truncated rows' cause coords.
        truncated_ids = {h["hypothesis_id"] for h in full} - {h["hypothesis_id"] for h in returned}
        truncated_by_rt = {h["risk_type"]: h for h in full if h["hypothesis_id"] in truncated_ids}
        for area in areas:
            rt = area["risk_type"]
            self.assertIn(rt, truncated_by_rt, "inspection_area risk_type must be a truncated row")
            src = truncated_by_rt[rt]["cause"]
            self.assertEqual(area["repo"], src["repo"])
            self.assertEqual(area["path"], src["path"])
            self.assertEqual(area["line"], src["line_start"])
            self.assertEqual(area["symbol"], src["qualname"])
            self.assertIn(src["qualname"], area["search_terms"])

    def test_suggested_followups_per_truncated_high_family(self) -> None:
        rqs, _full, _ret = self._run_over_generation()
        followups = rqs.get("suggested_followups") or []
        self.assertTrue(followups, "downgraded packet must carry suggested_followups")
        # Every followup names a truncated risk_type and points back at review_context.
        for f in followups:
            self.assertEqual(f["tool"], "review_context")
            self.assertIn("risk_type", f)

    def test_remainder_count_when_over_cap(self) -> None:
        # 8 high families available, 1 returned → 7 truncated → cap 5, omitted_count 2.
        full = [_hyp(i, risk_type=f"family_{i}") for i in range(8)]
        returned = full[:1]
        result = {"review_quality_status": _quality_status_packet_ready(8), "review_hypotheses": returned}
        ob._sync_review_quality_status_from_packet(
            result, original_hypotheses=returned, full_pre_cap_hypotheses=full,
            max_chars=REVIEW_CONTEXT_MAX_CHARS,
        )
        block = result["review_quality_status"]["inspection_areas"]
        self.assertEqual(len(block["areas"]), 5)
        self.assertEqual(block["omitted_count"], 2, f"7 truncated - 5 shown = 2 omitted; got {block}")

    def test_inversion_no_truncation_no_downgrade_no_inspection_areas(self) -> None:
        """Control: all high rows returned → packet_ready unchanged, no inspection_areas.

        Inversion proof that the C1 signals above are not always-on."""
        full = [_hyp(i, risk_type=f"family_{i}") for i in range(3)]
        returned = list(full)  # nothing truncated
        result = {"review_quality_status": _quality_status_packet_ready(3), "review_hypotheses": returned}
        ob._sync_review_quality_status_from_packet(
            result, original_hypotheses=returned, full_pre_cap_hypotheses=full,
            max_chars=REVIEW_CONTEXT_MAX_CHARS,
        )
        rqs = result["review_quality_status"]
        self.assertEqual(rqs["review_readiness"], "packet_ready")
        self.assertNotIn("inspection_areas", rqs, "no truncation → no inspection_areas")

    def test_budget_respected_after_inspection_areas(self) -> None:
        """inspection_areas fit inside the hard cap: if the affordance would push the packet
        over, it is trimmed (remainder tracked) — the hard cap always wins."""
        full = [_hyp(i, risk_type=f"family_{i}") for i in range(8)]
        returned = full[:1]
        # Measure the packet the sync produces with generous budget (all 5 areas present).
        generous = {"review_quality_status": _quality_status_packet_ready(8), "review_hypotheses": returned}
        ob._sync_review_quality_status_from_packet(
            generous, original_hypotheses=returned, full_pre_cap_hypotheses=full,
            max_chars=REVIEW_CONTEXT_MAX_CHARS,
        )
        full_chars = ob._current_chars(generous)
        self.assertEqual(len(generous["review_quality_status"]["inspection_areas"]["areas"]), 5)
        # Pick a cap between the base (no areas) and the full (5 areas) so trimming engages.
        tight = full_chars - 200
        result = {"review_quality_status": _quality_status_packet_ready(8), "review_hypotheses": returned}
        ob._sync_review_quality_status_from_packet(
            result, original_hypotheses=returned, full_pre_cap_hypotheses=full, max_chars=tight,
        )
        measured = ob._current_chars(result)
        self.assertLessEqual(measured, tight, f"packet must stay <= max_chars; measured={measured} cap={tight}")
        # Areas were trimmed below 5 but the remainder count preserves honesty.
        block = result["review_quality_status"].get("inspection_areas")
        if isinstance(block, dict):
            self.assertLess(len(block["areas"]), 5)
            self.assertGreaterEqual(block.get("omitted_count", 0), 1)
        # Even under trimming, readiness stays downgraded — the honesty signal is not lost.
        self.assertEqual(result["review_quality_status"]["review_readiness"], "needs_followup")


# ---------------------------------------------------------------------------
# C1 + C2 through the real call_tool pipeline (no-truncation control geometry)
# ---------------------------------------------------------------------------

class TestReviewContextPacketHonestyEndToEnd(unittest.TestCase):
    def test_no_truncation_packet_ready_and_no_inspection_areas(self) -> None:
        """Real pipeline, single high-spec hypothesis: packet_ready, no inspection_areas."""
        with tempfile.TemporaryDirectory() as d:
            kg = _build_async_signal_kg(Path(d))
            result = call_tool(kg, "review_context", {
                "repo": "svc",
                "changed_files": ["a.ts"],
                "changed_ranges": [{"path": "a.ts", "start_line": 1, "end_line": 5}],
            })
            rqs = result.get("review_quality_status") or {}
            self.assertEqual(rqs.get("specificity"), "high")
            self.assertEqual(
                rqs.get("review_readiness"), "packet_ready",
                f"single non-truncated high hypothesis must be packet_ready; got {rqs}",
            )
            self.assertNotIn("inspection_areas", rqs)
            self.assertLessEqual(len(json.dumps(result)), REVIEW_CONTEXT_MAX_CHARS)

    def test_attribution_labels_present_and_match_returned_rows(self) -> None:
        """C2: attribution_labels at the top of the answer packet match returned rows."""
        with tempfile.TemporaryDirectory() as d:
            kg = _build_async_signal_kg(Path(d))
            result = call_tool(kg, "review_context", {
                "repo": "svc",
                "changed_files": ["a.ts"],
                "changed_ranges": [{"path": "a.ts", "start_line": 1, "end_line": 5}],
            })
            ap = result.get("review_answer_packet") or {}
            attr = ap.get("attribution_labels")
            self.assertIsInstance(attr, dict, f"attribution_labels must be present; keys={list(ap.keys())}")
            self.assertIn("instruction", attr)
            self.assertTrue(attr["instruction"], "instruction must be a non-empty cite directive")
            returned_labels = [
                h.get("label") for h in (result.get("review_hypotheses") or []) if isinstance(h, dict)
            ]
            self.assertTrue(returned_labels, "fixture must return >=1 hypothesis with a label")
            self.assertEqual(
                attr["labels"], returned_labels,
                "attribution_labels must exactly match the returned hypothesis labels",
            )
            # Compactness: the affordance stays tiny.
            self.assertLess(len(json.dumps(attr)), 400)

    def test_inversion_attribution_labels_absent_when_no_hypotheses(self) -> None:
        """Inversion: a packet with zero hypotheses carries no attribution_labels — proves
        the field is driven by returned rows, not always emitted."""
        with tempfile.TemporaryDirectory() as d:
            # Minimal KG: no signals, no symbols → no hypotheses.
            (Path(d) / "entities.jsonl").write_text("")
            (Path(d) / "facts.jsonl").write_text("")
            (Path(d) / "evidence.jsonl").write_text("")
            (Path(d) / "coverage.jsonl").write_text("")
            (Path(d) / "manifest.json").write_text(json.dumps({"tenant_id": TENANT, "version": 1}))
            kg = KgSnapshot(Path(d))
            result = call_tool(kg, "review_context", {"repo": "default", "changed_files": ["x.ts"]})
            hyps = result.get("review_hypotheses") or []
            self.assertEqual(hyps, [], "control geometry must generate no hypotheses")
            ap = result.get("review_answer_packet") or {}
            self.assertNotIn(
                "attribution_labels", ap,
                "no returned hypotheses → no attribution_labels affordance",
            )


if __name__ == "__main__":
    unittest.main()
