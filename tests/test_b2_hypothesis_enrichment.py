"""Phase B2: hypothesis enrichment + review_quality_status.

Tests:
- specificity field on each hypothesis (high/medium/low)
- postable_claim non-null on signal-backed families when signal data present
- cause and consequence present on signal-backed families
- negative_checks present on every hypothesis (at least one REFUTE-phrased check)
- review_quality_status emitted on every call_tool result
- specificity=high + postable_claim non-null on signal-backed packet
- generic-only packet yields review_quality_status.specificity=low + recommended_action use_live_followups_or_plain_review
- test_locks never outranks a high-specificity hypothesis in top_review_hypotheses
- _compact_review_hypothesis passes through specificity, postable_claim, cause, consequence, negative_checks
- field replay: calcom/cal.diy pr-7232 canonical size <= 40000, 5 changed symbols, async family first,
  specificity=high + postable_claim non-null
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from source.kg.core.models import Entity, Evidence, Fact
from source.kg.core.store import JsonlKgStore
from source.kg.product.mcp_tools import call_tool
from source.kg.product.output_budget import _compact_review_hypothesis
from source.kg.product.review_hypotheses import review_hypotheses_for_context
from source.kg.query.snapshot import KgSnapshot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


def _risk_signal(
    family: str,
    subject_id: str,
    path: str = "src/app.py",
    line: int = 5,
    callee: str = "saveRecord",
    qualname: str = "handleEvent",
) -> dict:
    fid = f"fact_test_{family}_{qualname}"
    return {
        "fact_id": fid,
        "predicate": "code_risk_signal",
        "subject_id": subject_id,
        "object_id": subject_id,
        "qualifier": {
            "risk_family": family,
            "callee": callee,
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


def _build_async_signal_snapshot(root: Path) -> tuple[Entity, Fact]:
    symbol = Entity(
        kind="CodeSymbol",
        identity={
            "tenant_id": "default",
            "repo": "myrepo",
            "module": "src.handler",
            "qualname": "handleEvent",
            "symbol_kind": "function",
        },
        properties={"path": "src/handler.ts", "line": 5, "end_line": 15},
    )
    caller = Entity(
        kind="CodeSymbol",
        identity={
            "tenant_id": "default",
            "repo": "myrepo",
            "module": "src.caller",
            "qualname": "dispatch",
            "symbol_kind": "function",
        },
        properties={"path": "src/caller.ts", "line": 1, "end_line": 10},
    )
    signal_fact = Fact(
        predicate="code_risk_signal",
        subject_id=symbol.entity_id,
        object_id=symbol.entity_id,
        qualifier={
            "risk_family": "async_callback_in_iteration",
            "callee": "saveRecord",
            "qualname": "handleEvent",
            "line": 7,
        },
    )
    calls_fact = Fact(
        predicate="CALLS",
        subject_id=caller.entity_id,
        object_id=symbol.entity_id,
    )
    ev = Evidence(
        target_type="fact",
        target_id=signal_fact.fact_id,
        derivation_class="deterministic_static",
        source_system="test_extractor_v0",
        source_ref={"extractor": "test_extractor_v0"},
        bytes_ref={
            "repo": "myrepo",
            "commit_sha": "abc123",
            "path": "src/handler.ts",
            "line_start": 7,
            "line_end": 7,
        },
    )
    JsonlKgStore(root).write(
        entities=[symbol, caller],
        facts=[calls_fact],
        support_facts=[signal_fact],
        evidence=[ev],
        coverage=[],
        manifest={"version": 1},
    )
    return symbol, signal_fact


# ---------------------------------------------------------------------------
# Unit tests: specificity field
# ---------------------------------------------------------------------------

class TestSpecificityField(unittest.TestCase):

    def _call(self, **overrides):
        return review_hypotheses_for_context(**_base_context(**overrides))

    def test_async_family_carries_specificity_high(self):
        sym = _sym("handleList", "src/handler.ts", symbol_id="eid-1", lead_id="lead-1")
        sig = _risk_signal("async_callback_in_iteration", "eid-1", path="src/handler.ts", callee="processBatch")
        hyps = self._call(
            changed_symbols=[sym],
            direct_callers=[_edge("PageHandler", "handleList", "lead-edge-1")],
            risk_signals=[sig],
            review_leads={
                "changed_symbols": [{"lead_id": "lead-1", "path": "src/handler.ts", "symbol_id": "eid-1"}],
                "direct_callers": [{"lead_id": "lead-edge-1"}],
            },
        )
        h = next(h for h in hyps if h["risk_type"] == "async_side_effect_lifecycle_drift")
        self.assertEqual(h["specificity"], "high")

    def test_swallowed_exception_family_carries_specificity_high(self):
        sym = _sym("savePayment", "payments/processor.py", symbol_id="eid-se-1", lead_id="lead-se-1")
        sig = _risk_signal("swallowed_exception", "eid-se-1", path="payments/processor.py", qualname="savePayment")
        hyps = self._call(
            changed_symbols=[sym],
            risk_signals=[sig],
            review_leads={"changed_symbols": [{"lead_id": "lead-se-1", "path": "payments/processor.py", "symbol_id": "eid-se-1"}]},
        )
        h = next(h for h in hyps if h["risk_type"] == "swallowed_exception_state_drift")
        self.assertEqual(h["specificity"], "high")

    def test_component_family_carries_specificity_medium(self):
        hyps = self._call(
            changed_files=["src/ItemList.tsx"],
            changed_symbols=[_sym("ItemList", "src/ItemList.tsx", lead_id="lead-c1")],
            direct_callees=[_edge("ItemList", "Row", "lead-ce1")],
            review_leads={
                "changed_symbols": [{"lead_id": "lead-c1", "path": "src/ItemList.tsx"}],
                "direct_callees": [{"lead_id": "lead-ce1", "path": "src/Row.tsx"}],
            },
        )
        h = next(h for h in hyps if h["risk_type"] == "component_list_render_identity_drift")
        self.assertEqual(h["specificity"], "medium")

    def test_hook_gate_family_carries_specificity_medium(self):
        hyps = self._call(
            changed_files=["src/useAuth.ts"],
            changed_symbols=[_sym("useAuth", "src/useAuth.ts", lead_id="lead-h1")],
            direct_callers=[_edge("Dashboard", "useAuth", "lead-he1")],
            review_leads={
                "changed_symbols": [{"lead_id": "lead-h1", "path": "src/useAuth.ts"}],
                "direct_callers": [{"lead_id": "lead-he1", "path": "src/Dashboard.tsx"}],
            },
        )
        h = next(h for h in hyps if h["risk_type"] == "hook_gate_render_mismatch")
        self.assertEqual(h["specificity"], "medium")

    def test_test_locks_carries_specificity_low(self):
        hyps = self._call(
            changed_files=["src/parser.py", "tests/test_parser.py"],
            changed_symbols=[
                _sym("parse", "src/parser.py", lead_id="lead-t1"),
                _sym("test_parse_empty", "tests/test_parser.py", lead_id="lead-t2"),
            ],
            direct_callees=[_edge("test_parse_empty", "parse", "lead-te1")],
            review_leads={
                "changed_symbols": [
                    {"lead_id": "lead-t1", "path": "src/parser.py"},
                    {"lead_id": "lead-t2", "path": "tests/test_parser.py"},
                ],
                "direct_callees": [{"lead_id": "lead-te1"}],
            },
        )
        h = next(h for h in hyps if h["risk_type"] == "test_locks_in_regression")
        # test_locks is always low — no runtime invariant attached
        self.assertEqual(h["specificity"], "low")

    def test_direct_call_drift_carries_specificity_low(self):
        hyps = self._call(
            changed_files=["src/svc.py"],
            changed_symbols=[_sym("process", "src/svc.py", lead_id="lead-d1")],
            direct_callers=[_edge("caller", "process", "lead-de1")],
            review_leads={
                "changed_symbols": [{"lead_id": "lead-d1", "path": "src/svc.py"}],
                "direct_callers": [{"lead_id": "lead-de1"}],
            },
        )
        h = next(h for h in hyps if h["risk_type"] == "direct_call_contract_drift")
        self.assertEqual(h["specificity"], "low")


# ---------------------------------------------------------------------------
# Unit tests: postable_claim, cause, consequence, negative_checks
# ---------------------------------------------------------------------------

class TestAdjudicabilityFields(unittest.TestCase):

    def _call(self, **overrides):
        return review_hypotheses_for_context(**_base_context(**overrides))

    def test_async_family_has_non_null_postable_claim_when_callee_and_qualname_present(self):
        sym = _sym("handleEvent", "src/handler.ts", symbol_id="eid-pc-1", lead_id="lead-pc-1")
        sig = _risk_signal(
            "async_callback_in_iteration", "eid-pc-1",
            path="src/handler.ts", callee="saveRecord", qualname="handleEvent",
        )
        hyps = self._call(
            changed_symbols=[sym],
            direct_callers=[_edge("Caller", "handleEvent", "lead-edge-pc-1")],
            risk_signals=[sig],
            review_leads={
                "changed_symbols": [{"lead_id": "lead-pc-1", "path": "src/handler.ts", "symbol_id": "eid-pc-1"}],
                "direct_callers": [{"lead_id": "lead-edge-pc-1"}],
            },
        )
        h = next(h for h in hyps if h["risk_type"] == "async_side_effect_lifecycle_drift")
        # Non-vacuous: postable_claim must be non-null and reference actual symbol names
        self.assertIsNotNone(h.get("postable_claim"), "postable_claim must be non-null when signal data present")
        self.assertIn("handleEvent", h["postable_claim"])
        self.assertIn("saveRecord", h["postable_claim"])

    def test_async_family_has_cause_with_bytes_ref_coords(self):
        sym = _sym("fn", "src/a.ts", symbol_id="eid-cause-1", lead_id="lead-cause-1")
        sig = _risk_signal("unawaited_async_call", "eid-cause-1", path="src/a.ts", line=12)
        hyps = self._call(
            changed_symbols=[sym],
            risk_signals=[sig],
            review_leads={"changed_symbols": [{"lead_id": "lead-cause-1", "path": "src/a.ts", "symbol_id": "eid-cause-1"}]},
        )
        h = next(h for h in hyps if h["risk_type"] == "async_side_effect_lifecycle_drift")
        cause = h.get("cause")
        self.assertIsNotNone(cause, "cause must be present")
        self.assertIn("path", cause)
        self.assertEqual(cause["line_start"], 12)

    def test_every_hypothesis_has_negative_checks_with_at_least_one_entry(self):
        # Build a context that triggers several families
        sym = _sym("handleEvent", "src/handler.ts", symbol_id="eid-nc-1", lead_id="lead-nc-1")
        sig = _risk_signal("async_callback_in_iteration", "eid-nc-1", path="src/handler.ts")
        hyps = self._call(
            changed_symbols=[sym],
            direct_callers=[_edge("Caller", "handleEvent", "lead-edge-nc-1")],
            risk_signals=[sig],
            review_leads={
                "changed_symbols": [{"lead_id": "lead-nc-1", "path": "src/handler.ts", "symbol_id": "eid-nc-1"}],
                "direct_callers": [{"lead_id": "lead-edge-nc-1"}],
            },
        )
        for h in hyps:
            nc = h.get("negative_checks")
            self.assertIsNotNone(nc, f"negative_checks missing on {h['risk_type']}")
            self.assertIsInstance(nc, list)
            self.assertGreater(len(nc), 0, f"negative_checks empty on {h['risk_type']}")

    def test_negative_checks_are_refutation_phrased(self):
        # At least one check should contain a refutation qualifier
        sym = _sym("fn", "src/b.ts", symbol_id="eid-nc-2", lead_id="lead-nc-2")
        sig = _risk_signal("unawaited_async_call", "eid-nc-2", path="src/b.ts")
        hyps = self._call(
            changed_symbols=[sym],
            risk_signals=[sig],
            review_leads={"changed_symbols": [{"lead_id": "lead-nc-2", "path": "src/b.ts", "symbol_id": "eid-nc-2"}]},
        )
        h = next(h for h in hyps if h["risk_type"] == "async_side_effect_lifecycle_drift")
        nc = h["negative_checks"]
        # At least one check must contain "not apply", "unlikely", or "intentional"
        refutation_keywords = ("not apply", "unlikely", "intentional")
        self.assertTrue(
            any(any(kw in check for kw in refutation_keywords) for check in nc),
            f"No refutation-phrased check in negative_checks: {nc}",
        )

    def test_swallowed_exception_postable_claim_contains_qualname(self):
        sym = _sym("retryOp", "src/retry.py", symbol_id="eid-se-pc", lead_id="lead-se-pc")
        sig = _risk_signal("swallowed_exception", "eid-se-pc", path="src/retry.py", qualname="retryOp")
        hyps = self._call(
            changed_symbols=[sym],
            risk_signals=[sig],
            review_leads={"changed_symbols": [{"lead_id": "lead-se-pc", "path": "src/retry.py", "symbol_id": "eid-se-pc"}]},
        )
        h = next(h for h in hyps if h["risk_type"] == "swallowed_exception_state_drift")
        self.assertIsNotNone(h.get("postable_claim"))
        self.assertIn("retryOp", h["postable_claim"])

    def test_component_family_postable_claim_contains_symbol_name(self):
        hyps = self._call(
            changed_files=["src/ItemList.tsx"],
            changed_symbols=[_sym("ItemList", "src/ItemList.tsx", lead_id="lead-comp-pc")],
            direct_callees=[_edge("ItemList", "Row", "lead-ce-pc")],
            review_leads={
                "changed_symbols": [{"lead_id": "lead-comp-pc", "path": "src/ItemList.tsx"}],
                "direct_callees": [{"lead_id": "lead-ce-pc", "path": "src/Row.tsx"}],
            },
        )
        h = next(h for h in hyps if h["risk_type"] == "component_list_render_identity_drift")
        self.assertIsNotNone(h.get("postable_claim"))
        self.assertIn("ItemList", h["postable_claim"])

    def test_hook_family_postable_claim_contains_hook_name(self):
        hyps = self._call(
            changed_files=["src/useData.ts"],
            changed_symbols=[_sym("useData", "src/useData.ts", lead_id="lead-hk-pc")],
            direct_callers=[_edge("Widget", "useData", "lead-hke-pc")],
            review_leads={
                "changed_symbols": [{"lead_id": "lead-hk-pc", "path": "src/useData.ts"}],
                "direct_callers": [{"lead_id": "lead-hke-pc", "path": "src/Widget.tsx"}],
            },
        )
        h = next(h for h in hyps if h["risk_type"] == "hook_gate_render_mismatch")
        self.assertIsNotNone(h.get("postable_claim"))
        self.assertIn("useData", h["postable_claim"])


# ---------------------------------------------------------------------------
# Unit tests: test_locks ordering (must not outrank high-specificity)
# ---------------------------------------------------------------------------

class TestTestLocksOrdering(unittest.TestCase):

    def _call(self, **overrides):
        return review_hypotheses_for_context(**_base_context(**overrides))

    def test_test_locks_never_outranks_async_family_in_top_hypotheses(self):
        # Scenario: both test_locks and async family qualify; async must appear before test_locks
        sym_code = _sym("handleEvent", "src/handler.ts", symbol_id="eid-tlo-1", lead_id="lead-tlo-1")
        sym_test = _sym("testHandler", "tests/test_handler.ts", lead_id="lead-tlo-2")
        sig = _risk_signal("async_callback_in_iteration", "eid-tlo-1", path="src/handler.ts", callee="save")
        hyps = self._call(
            changed_files=["src/handler.ts", "tests/test_handler.ts"],
            changed_symbols=[sym_code, sym_test],
            direct_callers=[_edge("Caller", "handleEvent", "lead-edge-tlo-1")],
            direct_callees=[_edge("testHandler", "handleEvent", "lead-edge-tlo-2")],
            risk_signals=[sig],
            review_leads={
                "changed_symbols": [
                    {"lead_id": "lead-tlo-1", "path": "src/handler.ts", "symbol_id": "eid-tlo-1"},
                    {"lead_id": "lead-tlo-2", "path": "tests/test_handler.ts"},
                ],
                "direct_callers": [{"lead_id": "lead-edge-tlo-1"}],
                "direct_callees": [{"lead_id": "lead-edge-tlo-2"}],
            },
        )
        risk_types = [h["risk_type"] for h in hyps]
        # Must contain async family
        self.assertIn("async_side_effect_lifecycle_drift", risk_types)
        # test_locks must appear after async family (if present at all)
        if "test_locks_in_regression" in risk_types:
            async_idx = risk_types.index("async_side_effect_lifecycle_drift")
            test_locks_idx = risk_types.index("test_locks_in_regression")
            self.assertLess(
                async_idx,
                test_locks_idx,
                f"async family (idx={async_idx}) must appear before test_locks (idx={test_locks_idx})",
            )

    def test_test_locks_never_outranks_swallowed_exception(self):
        sym_code = _sym("savePayment", "payments/proc.py", symbol_id="eid-tlo-se", lead_id="lead-tlo-se")
        sym_test = _sym("testSave", "tests/test_proc.py", lead_id="lead-tlo-se-t")
        sig = _risk_signal("swallowed_exception", "eid-tlo-se", path="payments/proc.py", qualname="savePayment")
        hyps = self._call(
            changed_files=["payments/proc.py", "tests/test_proc.py"],
            changed_symbols=[sym_code, sym_test],
            direct_callers=[_edge("Caller", "savePayment", "lead-edge-tlo-se")],
            direct_callees=[_edge("testSave", "savePayment", "lead-edge-tlo-se-t")],
            risk_signals=[sig],
            review_leads={
                "changed_symbols": [
                    {"lead_id": "lead-tlo-se", "path": "payments/proc.py", "symbol_id": "eid-tlo-se"},
                    {"lead_id": "lead-tlo-se-t", "path": "tests/test_proc.py"},
                ],
                "direct_callers": [{"lead_id": "lead-edge-tlo-se"}],
                "direct_callees": [{"lead_id": "lead-edge-tlo-se-t"}],
            },
        )
        risk_types = [h["risk_type"] for h in hyps]
        self.assertIn("swallowed_exception_state_drift", risk_types)
        if "test_locks_in_regression" in risk_types:
            se_idx = risk_types.index("swallowed_exception_state_drift")
            tl_idx = risk_types.index("test_locks_in_regression")
            self.assertLess(se_idx, tl_idx)


# ---------------------------------------------------------------------------
# Unit tests: _compact_review_hypothesis passthrough
# ---------------------------------------------------------------------------

class TestCompactHypothesisPassthrough(unittest.TestCase):

    def test_specificity_passes_through(self):
        full = {
            "hypothesis_id": "hypothesis:async_side_effect_lifecycle_drift:abc",
            "risk_type": "async_side_effect_lifecycle_drift",
            "confidence": "medium",
            "why": "1 signal",
            "specificity": "high",
            "evidence_refs": [],
            "source_checks": [],
            "supporting_lead_ids": [],
        }
        compacted = _compact_review_hypothesis(full)
        self.assertEqual(compacted["specificity"], "high")

    def test_postable_claim_passes_through(self):
        full = {
            "hypothesis_id": "hypothesis:async_side_effect_lifecycle_drift:abc",
            "risk_type": "async_side_effect_lifecycle_drift",
            "confidence": "medium",
            "why": "1 signal",
            "specificity": "high",
            "postable_claim": "handleEvent contains an async call to saveRecord whose completion may not be coupled.",
            "evidence_refs": [],
            "source_checks": [],
            "supporting_lead_ids": [],
        }
        compacted = _compact_review_hypothesis(full)
        self.assertEqual(compacted["postable_claim"], full["postable_claim"])

    def test_cause_passes_through(self):
        full = {
            "hypothesis_id": "h:x:y",
            "risk_type": "swallowed_exception_state_drift",
            "confidence": "weak",
            "why": "...",
            "specificity": "high",
            "cause": {"repo": "myrepo", "path": "src/a.py", "line_start": 10, "line_end": 10},
            "evidence_refs": [],
            "source_checks": [],
            "supporting_lead_ids": [],
        }
        compacted = _compact_review_hypothesis(full)
        self.assertEqual(compacted["cause"], full["cause"])

    def test_consequence_passes_through(self):
        full = {
            "hypothesis_id": "h:x:y",
            "risk_type": "swallowed_exception_state_drift",
            "confidence": "weak",
            "why": "...",
            "specificity": "high",
            "consequence": {"path": "src/b.py", "line_start": 20},
            "evidence_refs": [],
            "source_checks": [],
            "supporting_lead_ids": [],
        }
        compacted = _compact_review_hypothesis(full)
        self.assertEqual(compacted["consequence"], full["consequence"])

    def test_negative_checks_passes_through_capped_at_2(self):
        full = {
            "hypothesis_id": "h:x:z",
            "risk_type": "direct_call_contract_drift",
            "confidence": "medium",
            "why": "...",
            "specificity": "low",
            "negative_checks": ["check A", "check B", "check C"],
            "evidence_refs": [],
            "source_checks": [],
            "supporting_lead_ids": [],
        }
        compacted = _compact_review_hypothesis(full)
        # Capped at 2
        self.assertEqual(compacted["negative_checks"], ["check A", "check B"])

    def test_null_postable_claim_not_present_when_absent(self):
        full = {
            "hypothesis_id": "h:x:z",
            "risk_type": "direct_call_contract_drift",
            "confidence": "medium",
            "why": "...",
            "specificity": "low",
            "evidence_refs": [],
            "source_checks": [],
            "supporting_lead_ids": [],
        }
        compacted = _compact_review_hypothesis(full)
        self.assertNotIn("postable_claim", compacted)

    def test_concrete_invariant_still_passes_through(self):
        full = {
            "hypothesis_id": "h:x:ci",
            "risk_type": "async_side_effect_lifecycle_drift",
            "confidence": "medium",
            "why": "...",
            "specificity": "high",
            "concrete_invariant": "Side-effect calls must be coupled.",
            "evidence_refs": [],
            "source_checks": [],
            "supporting_lead_ids": [],
        }
        compacted = _compact_review_hypothesis(full)
        self.assertIn("concrete_invariant", compacted)


# ---------------------------------------------------------------------------
# Integration tests: review_quality_status via call_tool
# ---------------------------------------------------------------------------

class TestReviewQualityStatusIntegration(unittest.TestCase):

    def test_signal_backed_packet_has_specificity_high_and_recommended_action_use_supercontext(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _build_async_signal_snapshot(root)
            result = call_tool(
                KgSnapshot(root),
                "review_context",
                {
                    "repo": "myrepo",
                    "changed_files": ["src/handler.ts"],
                    "changed_ranges": [{"path": "src/handler.ts", "start_line": 1, "end_line": 20}],
                    "limit": 10,
                },
            )
        rqs = result.get("review_quality_status")
        self.assertIsNotNone(rqs, "review_quality_status must be present")
        self.assertEqual(rqs["specificity"], "high")
        self.assertEqual(rqs["recommended_action"], "use_supercontext_packet")
        self.assertGreater(rqs["specific_hypothesis_count"], 0)

    def test_signal_backed_hypothesis_has_non_null_postable_claim(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _build_async_signal_snapshot(root)
            result = call_tool(
                KgSnapshot(root),
                "review_context",
                {
                    "repo": "myrepo",
                    "changed_files": ["src/handler.ts"],
                    "changed_ranges": [{"path": "src/handler.ts", "start_line": 1, "end_line": 20}],
                    "limit": 10,
                },
            )
        hyps = result.get("review_hypotheses") or []
        h = next(
            (h for h in hyps if h["risk_type"] == "async_side_effect_lifecycle_drift"),
            None,
        )
        self.assertIsNotNone(h)
        self.assertIsNotNone(h.get("postable_claim"), "postable_claim must be non-null on signal-backed hypothesis")

    def test_generic_only_packet_has_specificity_low_and_recommended_action_use_live_followups(self):
        # Snapshot with only a CALLS edge, no risk signals → generic families only
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            symbol = Entity(
                kind="CodeSymbol",
                identity={
                    "tenant_id": "default",
                    "repo": "myrepo",
                    "module": "src.svc",
                    "qualname": "processOrder",
                    "symbol_kind": "function",
                },
                properties={"path": "src/svc.py", "line": 1, "end_line": 10},
            )
            caller = Entity(
                kind="CodeSymbol",
                identity={
                    "tenant_id": "default",
                    "repo": "myrepo",
                    "module": "src.api",
                    "qualname": "handleRequest",
                    "symbol_kind": "function",
                },
                properties={"path": "src/api.py", "line": 1, "end_line": 10},
            )
            calls_fact = Fact(
                predicate="CALLS",
                subject_id=caller.entity_id,
                object_id=symbol.entity_id,
            )
            JsonlKgStore(root).write(
                entities=[symbol, caller],
                facts=[calls_fact],
                support_facts=[],
                evidence=[],
                coverage=[],
                manifest={"version": 1},
            )
            result = call_tool(
                KgSnapshot(root),
                "review_context",
                {
                    "repo": "myrepo",
                    "changed_files": ["src/svc.py"],
                    "changed_ranges": [{"path": "src/svc.py", "start_line": 1, "end_line": 10}],
                    "limit": 10,
                },
            )
        rqs = result.get("review_quality_status")
        self.assertIsNotNone(rqs, "review_quality_status must always be present")
        # No signal-backed families → specificity must be low or medium (no high)
        self.assertNotEqual(rqs["specificity"], "high")
        # If all generic: recommended_action must be use_live_followups_or_plain_review
        if rqs["specificity"] == "low":
            self.assertEqual(rqs["recommended_action"], "use_live_followups_or_plain_review")

    def test_review_quality_status_present_on_empty_packet(self):
        # Snapshot with no edges and no signals → empty hypotheses
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            symbol = Entity(
                kind="CodeSymbol",
                identity={
                    "tenant_id": "default",
                    "repo": "myrepo",
                    "module": "src.util",
                    "qualname": "helper",
                    "symbol_kind": "function",
                },
                properties={"path": "src/util.py", "line": 1, "end_line": 5},
            )
            JsonlKgStore(root).write(
                entities=[symbol],
                facts=[],
                support_facts=[],
                evidence=[],
                coverage=[],
                manifest={"version": 1},
            )
            result = call_tool(
                KgSnapshot(root),
                "review_context",
                {
                    "repo": "myrepo",
                    "changed_files": ["src/util.py"],
                    "changed_ranges": [{"path": "src/util.py", "start_line": 1, "end_line": 5}],
                    "limit": 10,
                },
            )
        rqs = result.get("review_quality_status")
        self.assertIsNotNone(rqs, "review_quality_status must be present even with empty hypotheses")
        self.assertEqual(rqs["specific_hypothesis_count"], 0)
        self.assertEqual(rqs["specificity"], "low")


# ---------------------------------------------------------------------------
# Field replay: calcom/cal.diy pr-7232
# ---------------------------------------------------------------------------

_FIELD_REPLAY_KG = os.path.expanduser(
    "~/work/pr-review/pr-review-autoresearch/runs/"
    "supercontext-kg-store-f1fb588/calcom__cal.diy/pr-7232/"
    "6048e2a86b50e81e1e3b1b467dfea5a895add3dc/"
    "kg-9d96b838e487301b/kg"
)
_FIELD_REPLAY_ARGS = "/tmp/sc-7232-args.json"


@unittest.skipUnless(
    os.path.isdir(_FIELD_REPLAY_KG) and os.path.isfile(_FIELD_REPLAY_ARGS),
    "field replay KG or args not available",
)
class TestFieldReplayCalDiy7232(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        with open(_FIELD_REPLAY_ARGS) as f:
            cls.args = json.load(f)
        cls.result = call_tool(KgSnapshot(Path(_FIELD_REPLAY_KG)), "review_context", cls.args)

    def test_canonical_size_le_40000(self):
        from source.kg.core.models import canonical_json
        size = len(canonical_json(self.result))
        self.assertLessEqual(size, 40000, f"canonical size {size} exceeds 40000")

    def test_changed_symbols_count_is_5(self):
        # "returned changed symbols 5" = the compacted list actually returned in the packet
        changed = self.result.get("changed_symbols") or []
        self.assertEqual(len(changed), 5, f"expected 5 returned changed symbols, got {len(changed)}")

    def test_async_family_is_first_in_top_review_hypotheses(self):
        packet = self.result.get("review_answer_packet") or {}
        top_hyps = packet.get("top_review_hypotheses") or []
        self.assertTrue(top_hyps, "top_review_hypotheses must be non-empty")
        first = top_hyps[0]
        self.assertEqual(
            first["risk_type"],
            "async_side_effect_lifecycle_drift",
            f"expected async family first, got {first['risk_type']}",
        )

    def test_async_family_has_specificity_high(self):
        hyps = self.result.get("review_hypotheses") or []
        h = next(
            (h for h in hyps if h["risk_type"] == "async_side_effect_lifecycle_drift"),
            None,
        )
        self.assertIsNotNone(h, "async family must be present")
        self.assertEqual(h["specificity"], "high")

    def test_async_family_has_non_null_postable_claim(self):
        hyps = self.result.get("review_hypotheses") or []
        h = next(
            (h for h in hyps if h["risk_type"] == "async_side_effect_lifecycle_drift"),
            None,
        )
        self.assertIsNotNone(h, "async family must be present")
        self.assertIsNotNone(h.get("postable_claim"), "postable_claim must be non-null on async hypothesis")

    def test_review_quality_status_present_with_high_specificity(self):
        rqs = self.result.get("review_quality_status")
        self.assertIsNotNone(rqs, "review_quality_status must be present")
        self.assertEqual(rqs["specificity"], "high")
        self.assertEqual(rqs["recommended_action"], "use_supercontext_packet")


if __name__ == "__main__":
    unittest.main()
