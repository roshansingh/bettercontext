"""Tests for call_result_identity_comparison_semantics hypothesis family (Task A1).

Tests:
1. risk_signal with risk_family="call_result_identity_comparison" + changed symbol matching → fires
2. risk_signal with risk_family="call_result_identity_comparison" + NO matching symbol → no hypothesis
3. risk_signal with different risk_family + changed symbol → no hypothesis
4. why string derives from callee_left/right in qualifier (non-fabricated)
5. Empty risk_signals → no hypothesis
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from source.kg.build.pipeline import build_kg
from source.kg.core.store import read_jsonl
from source.kg.product.review_hypotheses import review_hypotheses_for_context


NODE_AVAILABLE = shutil.which("node") is not None


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


def _sym(qualname: str, path: str, symbol_id: str | None = None, lead_id: str | None = None) -> dict:
    row = {
        "qualname": qualname,
        "display_name": f"mod.{qualname}",
        "qualified_name": f"mod.{qualname}",
        "kind": "function",
        "path": path,
    }
    if symbol_id:
        row["symbol_id"] = symbol_id
    if lead_id:
        row["lead_id"] = lead_id
    return row


def _edge(subject: str, object_: str, lead_id: str = "lead-edge") -> dict:
    return {"subject": f"mod.{subject}", "object": f"mod.{object_}", "lead_id": lead_id}


def _risk_signal_cri(
    subject_id: str,
    path: str = "src/slots.ts",
    line: int = 110,
    callee_left: str = "dayjs",
    callee_right: str = "dayjs",
    qualname: str = "handleSlots",
    fact_id: str | None = None,
) -> dict:
    fid = fact_id or f"fact_test_cri_{qualname}"
    return {
        "fact_id": fid,
        "predicate": "code_risk_signal",
        "subject_id": subject_id,
        "object_id": subject_id,
        "qualifier": {
            "risk_family": "call_result_identity_comparison",
            "callee_left": callee_left,
            "callee_right": callee_right,
            "qualname": qualname,
            "line": line,
        },
        "_evidence": [
            {
                "target_id": fid,
                "bytes_ref": {
                    "repo": "myrepo",
                    "path": path,
                    "line_start": line,
                    "line_end": line,
                },
            }
        ],
    }


# ---------------------------------------------------------------------------
# Extractor-level fixtures (ts_parser.mjs collector, end-to-end via build_kg)
# ---------------------------------------------------------------------------

# Positive: two call-expression results compared with === inside a function.
# Callees are identifier-rooted property chains so callName resolves them.
_TS_CALL_VS_CALL = """\
import dayjs from "dayjs";

export function overlaps(a: object, b: object): boolean {
  return dayjs.utc(a) === dayjs.utc(b);
}
"""

# Positive: chained-call bases (dayjs(a).add(...)) still fire; callName cannot
# resolve a CallExpression base, so callee strings degrade to "" by design.
_TS_CHAINED_CALL_VS_CALL = """\
import dayjs from "dayjs";

export function sameStart(a: string, b: string): boolean {
  return dayjs(a).startOf("day") === dayjs(b).startOf("day");
}
"""

# Positive: !== between call expressions also fires
_TS_CALL_VS_CALL_NOT_EQUAL = """\
export function drifted(a: string, b: string): boolean {
  return normalize(a) !== normalize(b);
}

function normalize(v: string): object {
  return { v };
}
"""

# Negative: literal on one side — not a call-vs-call comparison
_TS_CALL_VS_LITERAL = """\
export function isFive(a: string): boolean {
  return parseCount(a) === 5;
}

function parseCount(v: string): number {
  return v.length;
}
"""

# Negative: identifier-vs-identifier comparison — no call expressions
_TS_IDENT_VS_IDENT = """\
export function same(a: string, b: string): boolean {
  return a === b;
}
"""


def _build_support_facts(files: dict[str, str]) -> tuple[list[dict], list[dict]]:
    """Write files to a tempdir, run build_kg, return (support_facts, evidence)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        pkg = root / "pkg"
        pkg.mkdir()
        (pkg / "package.json").write_text(
            json.dumps({"name": "test-pkg", "version": "1.0.0"}), encoding="utf-8"
        )
        for name, text in files.items():
            (pkg / name).write_text(text, encoding="utf-8")
        out = root / "kg"
        build_kg(pkg, out)
        sf_path = out / "support_facts.jsonl"
        support_facts = read_jsonl(sf_path) if sf_path.exists() else []
        evidence = read_jsonl(out / "evidence.jsonl")
    return support_facts, evidence


def _identity_signals(support_facts: list[dict]) -> list[dict]:
    return [
        f
        for f in support_facts
        if f.get("predicate") == "code_risk_signal"
        and (f.get("qualifier") or {}).get("risk_family") == "call_result_identity_comparison"
    ]


@unittest.skipIf(not NODE_AVAILABLE, "node not available")
class CallResultIdentityComparisonExtractorTest(unittest.TestCase):
    """Extractor: ts_parser.mjs collector + Python adapter, end-to-end."""

    def test_call_vs_call_strict_equals_emits_signal(self):
        sf, ev = _build_support_facts({"overlap.ts": _TS_CALL_VS_CALL})
        signals = _identity_signals(sf)
        self.assertEqual(len(signals), 1)
        q = signals[0]["qualifier"]
        self.assertEqual(q["qualname"], "overlaps")
        self.assertEqual(q["callee_left"], "dayjs.utc")
        self.assertEqual(q["callee_right"], "dayjs.utc")
        # bytes_ref must be complete on the fact evidence
        fact_ev = [e for e in ev if e.get("target_id") == signals[0]["fact_id"]]
        self.assertTrue(fact_ev)
        br = fact_ev[0].get("bytes_ref") or {}
        for key in ("repo", "commit_sha", "path", "line_start", "line_end"):
            self.assertIn(key, br)

    def test_chained_call_bases_emit_signal_with_empty_callees(self):
        sf, _ = _build_support_facts({"chain.ts": _TS_CHAINED_CALL_VS_CALL})
        signals = _identity_signals(sf)
        self.assertEqual(len(signals), 1)
        q = signals[0]["qualifier"]
        self.assertEqual(q["qualname"], "sameStart")
        self.assertEqual(q["callee_left"], "")
        self.assertEqual(q["callee_right"], "")

    def test_call_vs_call_strict_not_equals_emits_signal(self):
        sf, _ = _build_support_facts({"drift.ts": _TS_CALL_VS_CALL_NOT_EQUAL})
        signals = _identity_signals(sf)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["qualifier"]["qualname"], "drifted")

    def test_call_vs_literal_emits_no_signal(self):
        sf, _ = _build_support_facts({"five.ts": _TS_CALL_VS_LITERAL})
        self.assertEqual(_identity_signals(sf), [])

    def test_ident_vs_ident_emits_no_signal(self):
        sf, _ = _build_support_facts({"same.ts": _TS_IDENT_VS_IDENT})
        self.assertEqual(_identity_signals(sf), [])


class TestCallResultIdentityComparisonSemantics(unittest.TestCase):
    """Family: call_result_identity_comparison_semantics"""

    def _call(self, **ctx_overrides):
        return review_hypotheses_for_context(**_base_context(**ctx_overrides))

    def test_positive_matching_symbol_fires(self):
        """Case 1: signal with call_result_identity_comparison + changed symbol matching → fires."""
        sym = _sym("handleSlots", "src/slots.ts", symbol_id="eid-cri-1", lead_id="lead-cri-1")
        sig = _risk_signal_cri("eid-cri-1", path="src/slots.ts", callee_left="dayjs", callee_right="dayjs")
        hypotheses = self._call(
            changed_symbols=[sym],
            direct_callers=[_edge("SlotPicker", "handleSlots", "lead-edge-cri-1")],
            risk_signals=[sig],
            review_leads={
                "changed_symbols": [{"lead_id": "lead-cri-1", "path": "src/slots.ts", "symbol_id": "eid-cri-1"}],
                "direct_callers": [{"lead_id": "lead-edge-cri-1"}],
            },
        )
        risk_types = [h["risk_type"] for h in hypotheses]
        self.assertIn("call_result_identity_comparison_semantics", risk_types)
        h = next(h for h in hypotheses if h["risk_type"] == "call_result_identity_comparison_semantics")
        self.assertTrue(h.get("evidence_refs"), "evidence_refs must be non-empty")
        self.assertIn("path", h["evidence_refs"][0])
        self.assertIn("concrete_invariant", h)
        self.assertIn("reference identity", h["concrete_invariant"])
        self.assertIn("hypothesis_id", h)
        self.assertTrue(h["hypothesis_id"].startswith("hypothesis:call_result_identity_comparison_semantics:"))

    def test_negative_no_matching_symbol_absent(self):
        """Case 2: signal with call_result_identity_comparison + NO matching symbol → no hypothesis."""
        sym = _sym("handleSlots", "src/slots.ts", symbol_id="eid-cri-2")
        # Signal subject_id is a different entity
        sig = _risk_signal_cri("eid-UNRELATED", path="src/other.ts")
        hypotheses = self._call(
            changed_symbols=[sym],
            direct_callers=[_edge("Caller", "handleSlots", "lead-edge-cri-2")],
            risk_signals=[sig],
        )
        risk_types = [h["risk_type"] for h in hypotheses]
        self.assertNotIn("call_result_identity_comparison_semantics", risk_types)

    def test_negative_different_family_absent(self):
        """Case 3: signal with different risk_family + changed symbol → no hypothesis."""
        sym = _sym("handleSlots", "src/slots.ts", symbol_id="eid-cri-3", lead_id="lead-cri-3")
        # Build a signal with a different family
        sig = {
            "fact_id": "fact_other_family",
            "predicate": "code_risk_signal",
            "subject_id": "eid-cri-3",
            "object_id": "eid-cri-3",
            "qualifier": {
                "risk_family": "async_callback_in_iteration",  # different family
                "callee_left": "dayjs",
                "callee_right": "dayjs",
                "qualname": "handleSlots",
                "line": 42,
            },
            "_evidence": [
                {
                    "target_id": "fact_other_family",
                    "bytes_ref": {
                        "repo": "myrepo",
                        "path": "src/slots.ts",
                        "line_start": 42,
                        "line_end": 42,
                    },
                }
            ],
        }
        hypotheses = self._call(
            changed_symbols=[sym],
            risk_signals=[sig],
            review_leads={
                "changed_symbols": [{"lead_id": "lead-cri-3", "path": "src/slots.ts", "symbol_id": "eid-cri-3"}],
            },
        )
        risk_types = [h["risk_type"] for h in hypotheses]
        self.assertNotIn("call_result_identity_comparison_semantics", risk_types)

    def test_positive_why_derives_from_callee_left_right(self):
        """Case 4: why string derives from callee_left/right in qualifier (non-fabricated)."""
        sym = _sym("compareSlots", "src/compare.ts", symbol_id="eid-cri-4", lead_id="lead-cri-4")
        sig = _risk_signal_cri(
            "eid-cri-4",
            path="src/compare.ts",
            callee_left="getStartTime",
            callee_right="getEndTime",
            qualname="compareSlots",
        )
        hypotheses = self._call(
            changed_symbols=[sym],
            risk_signals=[sig],
            review_leads={
                "changed_symbols": [{"lead_id": "lead-cri-4", "path": "src/compare.ts", "symbol_id": "eid-cri-4"}],
            },
        )
        h = next((h for h in hypotheses if h["risk_type"] == "call_result_identity_comparison_semantics"), None)
        self.assertIsNotNone(h)
        # why must contain the actual callee names from the qualifier, not fabricated text
        self.assertIn("getStartTime", h["why"])
        self.assertIn("getEndTime", h["why"])

    def test_negative_empty_risk_signals_absent(self):
        """Case 5: Empty risk_signals → no hypothesis."""
        sym = _sym("handleSlots", "src/slots.ts", symbol_id="eid-cri-5", lead_id="lead-cri-5")
        hypotheses = self._call(
            changed_symbols=[sym],
            direct_callers=[_edge("Caller", "handleSlots", "lead-edge-cri-5")],
            risk_signals=[],
        )
        risk_types = [h["risk_type"] for h in hypotheses]
        self.assertNotIn("call_result_identity_comparison_semantics", risk_types)

    def test_positive_path_fallback_match_via_evidence_path(self):
        """Signal matches via evidence path when symbol has no symbol_id."""
        sym = _sym("handleSlots", "src/slots.ts")  # no symbol_id
        sig = _risk_signal_cri("eid-unknown", path="src/slots.ts")
        hypotheses = self._call(
            changed_symbols=[sym],
            risk_signals=[sig],
        )
        risk_types = [h["risk_type"] for h in hypotheses]
        self.assertIn("call_result_identity_comparison_semantics", risk_types)

    def test_positive_confidence_medium_with_lead_and_edge(self):
        """confidence=medium when lead_ids non-empty and direct edges exist."""
        sym = _sym("fn", "src/fn.ts", symbol_id="eid-cri-m", lead_id="lead-cri-m")
        sig = _risk_signal_cri("eid-cri-m", path="src/fn.ts")
        hypotheses = self._call(
            changed_symbols=[sym],
            direct_callers=[_edge("Caller", "fn", "lead-edge-cri-m")],
            risk_signals=[sig],
            review_leads={
                "changed_symbols": [{"lead_id": "lead-cri-m", "path": "src/fn.ts", "symbol_id": "eid-cri-m"}],
            },
        )
        h = next((h for h in hypotheses if h["risk_type"] == "call_result_identity_comparison_semantics"), None)
        self.assertIsNotNone(h)
        self.assertEqual(h["confidence"], "medium")

    def test_positive_confidence_weak_without_edges(self):
        """confidence=weak when no direct edges."""
        sym = _sym("fn", "src/fn.ts", symbol_id="eid-cri-w", lead_id="lead-cri-w")
        sig = _risk_signal_cri("eid-cri-w", path="src/fn.ts")
        hypotheses = self._call(
            changed_symbols=[sym],
            direct_callers=[],
            direct_callees=[],
            risk_signals=[sig],
            review_leads={
                "changed_symbols": [{"lead_id": "lead-cri-w", "path": "src/fn.ts", "symbol_id": "eid-cri-w"}],
            },
        )
        h = next((h for h in hypotheses if h["risk_type"] == "call_result_identity_comparison_semantics"), None)
        self.assertIsNotNone(h)
        self.assertEqual(h["confidence"], "weak")


if __name__ == "__main__":
    unittest.main()
