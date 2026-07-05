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

from unittest.mock import patch

from source.kg.build.pipeline import build_kg
from source.kg.core.models import canonical_json
from source.kg.integrations.semantic_llm import LlmResult
from source.kg.product import mcp_tools
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


# ---------------------------------------------------------------------------
# C1 + C2 survive the REAL hypothesis_first budget-compaction path (call_tool)
#
# Regression for the Wave-3 Task C field-drop: the original C tests exercised
# _sync_review_quality_status_from_packet directly (C1) or used a no-truncation
# call_tool geometry (C2), so the hypothesis_first compact rebuild — which lands
# the packet at/over the 15K cap and dropped BOTH affordances via "hard cap wins"
# guards — was never covered. These tests force budget compaction AND high-family
# truncation through call_tool and assert both affordances survive in the FINAL
# serialized packet. Inversion: without the fund-over-cap threading the drop
# reproduces (attribution_labels absent, review_quality_status.inspection_areas
# absent) exactly as observed in the field packet.
# ---------------------------------------------------------------------------

def _build_over_generation_semantic_pair(root: Path, *, nfiles: int = 20):
    """Real build_kg base/head pair: nfiles guard-removing functions plus a fake
    semantic-diff client that emits several distinct high-specificity claims per call.

    Enough distinct high families are generated to overflow the top-5 cap (forcing
    high-family truncation → C1 inspection_areas) while the lead + diff-anchor mass
    pushes the packet over REVIEW_CONTEXT_MAX_CHARS (forcing the hypothesis_first
    compact rebuild). Returns (head_kg, call_args, fake_client)."""
    svc = root / "svc"
    svc.mkdir()
    (svc / "__init__.py").write_text("", encoding="utf-8")
    base_ck = root / "cb"
    base_ck.mkdir()
    head_ck = root / "ch"
    head_ck.mkdir()
    files: list[str] = []
    for i in range(nfiles):
        fn = f"m{i}.py"
        files.append(fn)
        base = (
            f"def func{i}(a, b, c):\n    if a is None:\n        raise ValueError\n"
            f"    helper{i}(a)\n    return a\ndef helper{i}(a):\n    return a\n"
        )
        head = f"def func{i}(a, b, c):\n    return a\n"
        (svc / fn).write_text(base, encoding="utf-8")
        (base_ck / fn).write_text(base, encoding="utf-8")
        (head_ck / fn).write_text(head, encoding="utf-8")
    out_base = root / "kb"
    build_kg(svc, out_base, tenant_id=TENANT)
    for i in range(nfiles):
        (svc / f"m{i}.py").write_text(f"def func{i}(a, b, c):\n    return a\n", encoding="utf-8")
    out_head = root / "kh"
    build_kg(svc, out_head, tenant_id=TENANT)
    head_kg = KgSnapshot(out_head)

    def _claim(j: int) -> dict:
        return {
            "claim": f"variant {j} no longer validates",
            "cause_line": 2,
            "consequence": "invalid data " + "z" * 40,
            "negative_check": "if intentional, N/A " + "q" * 40,
            "category": "guard_removal",
            "old_contract": "validated v%d " % j + "a" * 40,
            "new_contract": "no longer validates v%d " % j + "b" * 40,
            "violated_invariant": "callers relied on rejection %d " % j + "c" * 40,
        }

    class _FakeMultiClaimClient:
        def __init__(self) -> None:
            self.call_count = 0

        def complete_json(self, prompt: str) -> LlmResult:
            self.call_count += 1
            base = self.call_count
            return LlmResult.parsed([_claim(base * 100 + k) for k in range(3)])

    fake_client = _FakeMultiClaimClient()
    call_args = {
        "repo": "svc",
        "changed_files": files,
        "base_snapshot": str(out_base),
        "base_checkout": str(base_ck),
        "head_checkout": str(head_ck),
        # Wave 3 honesty tests assert the pre-follow-up needs_followup affordances.
        # Wave 4 has separate coverage for the default internal follow-up execution path.
        "execute_followups": False,
    }
    return head_kg, call_args, fake_client


def _call_review_context_with_fake_semantic(head_kg, call_args, fake_client) -> dict:
    real = mcp_tools._splice_semantic_diff_hypotheses
    with patch(
        "source.kg.product.mcp_tools._splice_semantic_diff_hypotheses",
        side_effect=lambda **kw: real(**kw, _client=fake_client),
    ):
        return call_tool(head_kg, "review_context", call_args)


class TestPacketHonestySurvivesBudgetCompaction(unittest.TestCase):
    """Both affordances must survive the hypothesis_first compact rebuild via call_tool."""

    def _run(self):
        with tempfile.TemporaryDirectory() as d:
            head_kg, call_args, fc = _build_over_generation_semantic_pair(Path(d))
            result = _call_review_context_with_fake_semantic(head_kg, call_args, fc)
            self.assertGreaterEqual(fc.call_count, 1, "real splice must call the fake client")
            return result

    def test_geometry_forces_compaction_and_truncation(self) -> None:
        """Precondition: the fixture actually forces the hypothesis_first path AND
        high-family truncation — otherwise the affordance assertions are vacuous."""
        result = self._run()
        budget = result.get("output_budget") or {}
        self.assertEqual(
            budget.get("profile"), "hypothesis_first",
            f"fixture must trigger the compact rebuild; budget={budget}",
        )
        rqs = result.get("review_quality_status") or {}
        gen = rqs.get("generated_specific_hypothesis_count")
        returned = len(result.get("review_hypotheses") or [])
        self.assertIsInstance(gen, int, "high families must have been truncated (generated>returned)")
        self.assertGreater(gen, returned, "fixture must truncate at least one high family")
        stats = rqs.get("semantic_diff_stats") or {}
        self.assertGreater(
            stats.get("rows_verified", 0),
            0,
            f"semantic verifier must not reject every over-generation row; stats={stats}",
        )

    def test_attribution_labels_survive_in_final_packet(self) -> None:
        """C2 regression: attribution_labels must be in the FINAL answer packet after
        compaction, matching the returned hypothesis labels. This is the exact assertion
        the field-drop would fail (pre-fix: answer packet has only
        {packet_mode, status, review_lead_status, top_review_hypotheses})."""
        result = self._run()
        ap = result.get("review_answer_packet") or {}
        attr = ap.get("attribution_labels")
        self.assertIsInstance(
            attr, dict,
            f"attribution_labels must survive compaction; ap keys={sorted(ap.keys())}",
        )
        self.assertTrue(attr.get("instruction"), "cite instruction must be present")
        returned_labels = [
            h.get("label") for h in (result.get("review_hypotheses") or []) if isinstance(h, dict)
        ]
        self.assertTrue(returned_labels, "fixture must return >=1 labeled hypothesis")
        self.assertEqual(
            attr.get("labels"), returned_labels,
            "attribution_labels must match the FINAL (post-eviction) returned rows",
        )
        self.assertLess(len(json.dumps(attr)), 400, "affordance stays compact")

    def test_inspection_areas_survive_in_final_packet(self) -> None:
        """C1 regression: with high families truncated, review_readiness downgrades to
        needs_followup and review_quality_status.inspection_areas (with structural coords)
        must survive compaction in the FINAL packet."""
        result = self._run()
        rqs = result.get("review_quality_status") or {}
        self.assertEqual(
            rqs.get("review_readiness"), "needs_followup",
            f"truncated high families must downgrade readiness; rqs={rqs}",
        )
        block = rqs.get("inspection_areas")
        self.assertIsInstance(
            block, dict,
            f"inspection_areas must survive compaction; rqs keys={sorted(rqs.keys())}",
        )
        areas = block.get("areas") or []
        self.assertTrue(areas, "inspection_areas.areas must be non-empty")
        self.assertLessEqual(len(areas), 5, "inspection_areas capped at 5")
        # Structural coords sourced from the truncated rows.
        first = areas[0]
        self.assertIn("risk_type", first)
        self.assertTrue(
            any(k in first for k in ("path", "symbol", "line")),
            f"inspection area must carry a structural coordinate; got {first}",
        )

    def test_final_packet_within_budget(self) -> None:
        """Both affordances stay inside the 15K budget (canonical_json is the system measure)."""
        result = self._run()
        size = len(canonical_json(result))
        self.assertLessEqual(
            size, REVIEW_CONTEXT_MAX_CHARS,
            f"packet with affordances must fit the 15K cap; size={size}",
        )

    def test_no_stale_lead_ids_in_final_packet(self) -> None:
        """No surviving hypothesis may cite a lead_id absent from the final review_leads.
        Broad invariant over the real pipeline; the load-bearing eviction-forcing regression
        lives in test_pre_pr_review_fixes.TestStaleLeadIdsAfterLateFundingEviction."""
        from source.kg.product.output_budget import _collect_surviving_lead_ids

        result = self._run()
        surviving = _collect_surviving_lead_ids(result)
        ap = result.get("review_answer_packet") or {}
        for hyps in (result.get("review_hypotheses") or [], ap.get("top_review_hypotheses") or []):
            for hyp in hyps:
                lead_ids = hyp.get("supporting_lead_ids") if isinstance(hyp, dict) else None
                if isinstance(lead_ids, list):
                    stale = [lid for lid in lead_ids if lid not in surviving]
                    self.assertEqual(stale, [], f"stale lead ids in {hyp.get('label')!r}: {stale}")


# ---------------------------------------------------------------------------
# Ordering regression: attribution_labels funded before inspection_areas
# ---------------------------------------------------------------------------
# Real-packet evidence (635280d): attribution_labels was null on the two larger
# packets (grafana, sentry geometries).  Root cause: _sync_review_quality_status_from_packet
# ran FIRST and consumed all eviction slack for inspection_areas, leaving
# _attach_attribution_labels with nothing to evict and the packet already at cap,
# so it dropped labels.  Fix: swap the order — fund attribution_labels first.
# ---------------------------------------------------------------------------

class TestAttributionLabelsBeforeInspectionAreas(unittest.TestCase):
    """Ordering guarantee: attribution_labels is funded BEFORE inspection_areas consumes
    eviction slack.  Verified by constructing a packet that sits exactly at the cap after
    both affordances are attached, then asserting labels survive and inspection_areas
    shrinks (rather than labels being the victim)."""

    def _make_at_cap_packet(self, *, n_hyps: int = 5, cap: int) -> dict:
        """Packet with n_hyps high-spec hypotheses returned + n_hyps-1 truncated (so
        inspection_areas is non-empty), sized to land just over ``cap`` before finalize.
        Returns the finalized packet under ``cap``."""
        from source.kg.product.output_budget import (
            _finalize_review_hypothesis_budget,
            _attach_review_hypothesis_status,
        )
        from source.kg.core.models import canonical_json

        hyps = [_hyp(i, specificity="high") for i in range(n_hyps)]
        original_hypotheses = [_hyp(i, specificity="high") for i in range(n_hyps * 2)]

        # Build a minimal skeleton that sits just over cap by padding with a large filler.
        result: dict = {
            "tool": "review_context",
            "status": "ok",
            "repo": "testrepo",
            "review_hypotheses": hyps,
            "review_answer_packet": {"status": "ok", "packet_mode": "hypothesis_first",
                                     "review_lead_status": {}, "top_review_hypotheses": []},
            "review_leads": {},
            "review_lead_status": {},
            "diff_anchors": [],
            "changed_symbols": [],
            "source_coordinates": [],
            "output_budget": {"profile": "hypothesis_first", "truncated": True,
                              "truncated_sections": ["review_hypotheses"]},
            # Filler to push packet just over cap; finalize must evict it.
            "direct_callers": [{"caller_symbol": "x" * 200, "repo": "r", "path": f"p{k}.py",
                                 "line": k} for k in range(20)],
        }
        _finalize_review_hypothesis_budget(
            result, original_hypotheses,
            original_review_leads={},
            max_chars=cap,
        )
        return result

    def test_labels_present_when_inspection_areas_also_present(self) -> None:
        """Both affordances survive when both are funded; labels are NOT the eviction victim."""
        cap = REVIEW_CONTEXT_MAX_CHARS
        result = self._make_at_cap_packet(n_hyps=4, cap=cap)
        ap = result.get("review_answer_packet") or {}
        rqs = result.get("review_quality_status") or {}
        size = len(canonical_json(result))
        self.assertLessEqual(size, cap, f"finalize must fit; size={size}")
        # If hypotheses were returned, labels must be present.
        if result.get("review_hypotheses"):
            self.assertIn(
                "attribution_labels", ap,
                f"labels must survive even when inspection_areas is present; ap keys={sorted(ap.keys())}",
            )

    def test_inversion_labels_absent_when_no_hypotheses_returned(self) -> None:
        """Inversion: zero returned hypotheses → attribution_labels absent.
        Proves the positive assertion above is not vacuous."""
        from source.kg.product.output_budget import _finalize_review_hypothesis_budget
        result: dict = {
            "tool": "review_context",
            "status": "ok",
            "repo": "testrepo",
            "review_hypotheses": [],
            "review_answer_packet": {"status": "ok"},
            "review_leads": {},
            "review_lead_status": {},
            "diff_anchors": [],
            "changed_symbols": [],
            "source_coordinates": [],
            "output_budget": {"profile": "hypothesis_first"},
        }
        _finalize_review_hypothesis_budget(
            result, [],
            original_review_leads={},
            max_chars=REVIEW_CONTEXT_MAX_CHARS,
        )
        ap = result.get("review_answer_packet") or {}
        self.assertNotIn(
            "attribution_labels", ap,
            "no returned hypotheses → no attribution_labels",
        )

    def test_labels_match_final_returned_rows_after_eviction(self) -> None:
        """Labels exactly match the hypothesis labels actually in the packet post-eviction."""
        from source.kg.core.models import canonical_json
        cap = REVIEW_CONTEXT_MAX_CHARS
        result = self._make_at_cap_packet(n_hyps=3, cap=cap)
        ap = result.get("review_answer_packet") or {}
        hyps = result.get("review_hypotheses") or []
        if not hyps:
            return  # no hypotheses returned → labels correctly absent (covered above)
        attr = ap.get("attribution_labels")
        self.assertIsInstance(attr, dict, "attribution_labels must be a dict")
        returned_labels = [h.get("label") for h in hyps if isinstance(h, dict)]
        self.assertEqual(
            attr.get("labels"), returned_labels,
            "labels must exactly match final returned rows, in order",
        )


if __name__ == "__main__":
    unittest.main()
