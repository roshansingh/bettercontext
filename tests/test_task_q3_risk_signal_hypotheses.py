"""Task Q3: Concrete-invariant hypothesis families driven by code_risk_signal support facts.

Tests:
- review_hypotheses_for_context: async_side_effect_lifecycle_drift (positive, negative, low-coverage)
- review_hypotheses_for_context: swallowed_exception_state_drift (positive, negative, low-coverage)
- _compact_review_hypothesis passes through concrete_invariant
- call_tool(review_context): families fire when KG has matching support facts
- call_tool(review_context): families absent when no support facts
- call_tool(review_context): families absent for low_coverage packet
- signal on unchanged file only (no changed_symbol match) → absent
"""
from __future__ import annotations

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
# Helpers shared across test cases
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


def _sym(qualname: str, path: str, entity_id: str | None = None, lead_id: str | None = None) -> dict:
    row = {
        "qualname": qualname,
        "display_name": f"mod.{qualname}",
        "qualified_name": f"mod.{qualname}",
        "kind": "function",
        "path": path,
    }
    if entity_id:
        row["entity_id"] = entity_id
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
    fact_id: str | None = None,
) -> dict:
    """Build a synthetic risk signal row as returned by _review_context_risk_signals."""
    fid = fact_id or f"fact_test_{family}_{qualname}"
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


# ---------------------------------------------------------------------------
# Unit tests: review_hypotheses_for_context (no KG, direct call)
# ---------------------------------------------------------------------------

class TestAsyncSideEffectLifecycleDrift(unittest.TestCase):
    """Family: async_side_effect_lifecycle_drift"""

    def _call(self, **ctx_overrides):
        return review_hypotheses_for_context(**_base_context(**ctx_overrides))

    def test_positive_async_callback_in_iteration_fires(self):
        sym = _sym("handleList", "src/handler.ts", entity_id="eid-1", lead_id="lead-1")
        sig = _risk_signal("async_callback_in_iteration", "eid-1", path="src/handler.ts", callee="processBatch")
        hypotheses = self._call(
            changed_symbols=[sym],
            direct_callers=[_edge("PageHandler", "handleList", "lead-edge-1")],
            risk_signals=[sig],
            review_leads={
                "changed_symbols": [{"lead_id": "lead-1", "path": "src/handler.ts", "entity_id": "eid-1"}],
                "direct_callers": [{"lead_id": "lead-edge-1"}],
            },
        )
        risk_types = [h["risk_type"] for h in hypotheses]
        self.assertIn("async_side_effect_lifecycle_drift", risk_types)
        h = next(h for h in hypotheses if h["risk_type"] == "async_side_effect_lifecycle_drift")
        # Non-vacuous: must have evidence_refs with coordinates
        self.assertTrue(h.get("evidence_refs"), "evidence_refs must be non-empty")
        self.assertIn("path", h["evidence_refs"][0], "evidence_ref must carry path")
        # Must carry concrete_invariant
        self.assertIn("concrete_invariant", h)
        self.assertIn("coupled", h["concrete_invariant"])
        # Must have hypothesis_id
        self.assertIn("hypothesis_id", h)
        self.assertTrue(h["hypothesis_id"].startswith("hypothesis:async_side_effect_lifecycle_drift:"))

    def test_positive_unawaited_async_call_fires(self):
        sym = _sym("mutate", "src/mutate.ts", entity_id="eid-2", lead_id="lead-2")
        sig = _risk_signal("unawaited_async_call", "eid-2", path="src/mutate.ts", callee="saveRow")
        hypotheses = self._call(
            changed_symbols=[sym],
            risk_signals=[sig],
            review_leads={"changed_symbols": [{"lead_id": "lead-2", "path": "src/mutate.ts", "entity_id": "eid-2"}]},
        )
        risk_types = [h["risk_type"] for h in hypotheses]
        self.assertIn("async_side_effect_lifecycle_drift", risk_types)

    def test_positive_confidence_medium_when_lead_and_direct_edge(self):
        sym = _sym("processItem", "src/proc.ts", entity_id="eid-3", lead_id="lead-3")
        sig = _risk_signal("async_callback_in_iteration", "eid-3", path="src/proc.ts")
        hypotheses = self._call(
            changed_symbols=[sym],
            direct_callers=[_edge("Caller", "processItem", "lead-edge-3")],
            risk_signals=[sig],
            review_leads={
                "changed_symbols": [{"lead_id": "lead-3", "path": "src/proc.ts", "entity_id": "eid-3"}],
            },
        )
        h = next((h for h in hypotheses if h["risk_type"] == "async_side_effect_lifecycle_drift"), None)
        self.assertIsNotNone(h)
        self.assertEqual(h["confidence"], "medium")

    def test_positive_confidence_weak_when_no_direct_edge(self):
        sym = _sym("doWork", "src/work.ts", entity_id="eid-4", lead_id="lead-4")
        sig = _risk_signal("unawaited_async_call", "eid-4", path="src/work.ts")
        hypotheses = self._call(
            changed_symbols=[sym],
            risk_signals=[sig],
            review_leads={"changed_symbols": [{"lead_id": "lead-4", "path": "src/work.ts", "entity_id": "eid-4"}]},
        )
        h = next((h for h in hypotheses if h["risk_type"] == "async_side_effect_lifecycle_drift"), None)
        self.assertIsNotNone(h)
        self.assertEqual(h["confidence"], "weak")

    def test_negative_no_signals_family_absent(self):
        sym = _sym("handleList", "src/handler.ts", entity_id="eid-5", lead_id="lead-5")
        hypotheses = self._call(
            changed_symbols=[sym],
            direct_callers=[_edge("Caller", "handleList", "lead-edge-5")],
            risk_signals=[],
        )
        risk_types = [h["risk_type"] for h in hypotheses]
        self.assertNotIn("async_side_effect_lifecycle_drift", risk_types)

    def test_negative_signal_on_unchanged_entity_absent(self):
        sym = _sym("handleList", "src/handler.ts", entity_id="eid-6")
        # Signal subject_id is different entity — NOT in changed symbols
        sig = _risk_signal("async_callback_in_iteration", "eid-UNRELATED", path="src/other.ts")
        hypotheses = self._call(
            changed_symbols=[sym],
            direct_callers=[_edge("Caller", "handleList", "lead-edge-6")],
            risk_signals=[sig],
        )
        risk_types = [h["risk_type"] for h in hypotheses]
        self.assertNotIn("async_side_effect_lifecycle_drift", risk_types)

    def test_negative_signal_on_unchanged_file_absent(self):
        # changed_symbols has no entity_id (so entity match won't fire);
        # signal evidence path is NOT in changed_symbols paths
        sym = _sym("handleList", "src/changed.ts")
        sig = _risk_signal("unawaited_async_call", "eid-X", path="src/unchanged.ts")
        hypotheses = self._call(
            changed_symbols=[sym],
            risk_signals=[sig],
        )
        risk_types = [h["risk_type"] for h in hypotheses]
        self.assertNotIn("async_side_effect_lifecycle_drift", risk_types)

    def test_negative_low_coverage_gate_respected(self):
        sym = _sym("handleList", "src/handler.ts", entity_id="eid-lc")
        sig = _risk_signal("async_callback_in_iteration", "eid-lc", path="src/handler.ts")
        hypotheses = self._call(
            changed_files=["src/handler.css"],  # stylesheet for low_coverage_stylesheet_gap
            changed_symbols=[sym],
            risk_signals=[sig],
            review_lead_status={"coverage_status": "low_coverage"},
        )
        risk_types = [h["risk_type"] for h in hypotheses]
        self.assertNotIn("async_side_effect_lifecycle_drift", risk_types)

    def test_positive_why_contains_callee_name(self):
        sym = _sym("fn", "src/a.ts", entity_id="eid-why")
        sig = _risk_signal("unawaited_async_call", "eid-why", path="src/a.ts", callee="persistRecord")
        hypotheses = self._call(changed_symbols=[sym], risk_signals=[sig], review_leads={})
        h = next((h for h in hypotheses if h["risk_type"] == "async_side_effect_lifecycle_drift"), None)
        self.assertIsNotNone(h)
        self.assertIn("persistRecord", h["why"])

    def test_positive_path_fallback_match_via_evidence_path(self):
        # Symbol has no entity_id; match via evidence path in changed_symbols paths
        sym = _sym("processItems", "src/list.ts")  # no entity_id
        sig = _risk_signal("async_callback_in_iteration", "eid-path", path="src/list.ts")
        hypotheses = self._call(
            changed_symbols=[sym],
            risk_signals=[sig],
        )
        risk_types = [h["risk_type"] for h in hypotheses]
        self.assertIn("async_side_effect_lifecycle_drift", risk_types)


class TestSwallowedExceptionStateDrift(unittest.TestCase):
    """Family: swallowed_exception_state_drift"""

    def _call(self, **ctx_overrides):
        return review_hypotheses_for_context(**_base_context(**ctx_overrides))

    def test_positive_swallowed_exception_fires(self):
        sym = _sym("savePayment", "payments/processor.py", entity_id="eid-se-1", lead_id="lead-se-1")
        sig = _risk_signal("swallowed_exception", "eid-se-1", path="payments/processor.py",
                           callee="", qualname="savePayment")
        hypotheses = self._call(
            changed_symbols=[sym],
            direct_callers=[_edge("Caller", "savePayment", "lead-edge-se-1")],
            risk_signals=[sig],
            review_leads={
                "changed_symbols": [{"lead_id": "lead-se-1", "path": "payments/processor.py", "entity_id": "eid-se-1"}],
            },
        )
        risk_types = [h["risk_type"] for h in hypotheses]
        self.assertIn("swallowed_exception_state_drift", risk_types)
        h = next(h for h in hypotheses if h["risk_type"] == "swallowed_exception_state_drift")
        # Non-vacuous: evidence_refs with coordinates
        self.assertTrue(h.get("evidence_refs"), "evidence_refs must be non-empty")
        self.assertIn("path", h["evidence_refs"][0])
        # concrete_invariant present
        self.assertIn("concrete_invariant", h)
        self.assertIn("silent state", h["concrete_invariant"])
        # hypothesis_id stable
        self.assertIn("hypothesis_id", h)
        self.assertTrue(h["hypothesis_id"].startswith("hypothesis:swallowed_exception_state_drift:"))

    def test_positive_confidence_medium_lead_plus_edge(self):
        sym = _sym("processOrder", "orders/handler.py", entity_id="eid-se-2", lead_id="lead-se-2")
        sig = _risk_signal("swallowed_exception", "eid-se-2", path="orders/handler.py")
        hypotheses = self._call(
            changed_symbols=[sym],
            direct_callees=[_edge("processOrder", "db_write", "lead-edge-se-2")],
            risk_signals=[sig],
            review_leads={
                "changed_symbols": [{"lead_id": "lead-se-2", "path": "orders/handler.py", "entity_id": "eid-se-2"}],
            },
        )
        h = next((h for h in hypotheses if h["risk_type"] == "swallowed_exception_state_drift"), None)
        self.assertIsNotNone(h)
        self.assertEqual(h["confidence"], "medium")

    def test_positive_confidence_weak_no_direct_edge(self):
        sym = _sym("cleanupJob", "jobs/cleanup.py", entity_id="eid-se-3", lead_id="lead-se-3")
        sig = _risk_signal("swallowed_exception", "eid-se-3", path="jobs/cleanup.py")
        hypotheses = self._call(
            changed_symbols=[sym],
            risk_signals=[sig],
            review_leads={"changed_symbols": [{"lead_id": "lead-se-3", "path": "jobs/cleanup.py", "entity_id": "eid-se-3"}]},
        )
        h = next((h for h in hypotheses if h["risk_type"] == "swallowed_exception_state_drift"), None)
        self.assertIsNotNone(h)
        self.assertEqual(h["confidence"], "weak")

    def test_negative_no_signals_family_absent(self):
        sym = _sym("savePayment", "payments/processor.py", entity_id="eid-se-4")
        hypotheses = self._call(
            changed_symbols=[sym],
            risk_signals=[],
        )
        risk_types = [h["risk_type"] for h in hypotheses]
        self.assertNotIn("swallowed_exception_state_drift", risk_types)

    def test_negative_async_signal_does_not_trigger_swallowed(self):
        sym = _sym("fn", "src/f.ts", entity_id="eid-se-5")
        sig = _risk_signal("async_callback_in_iteration", "eid-se-5", path="src/f.ts")
        hypotheses = self._call(changed_symbols=[sym], risk_signals=[sig])
        risk_types = [h["risk_type"] for h in hypotheses]
        self.assertNotIn("swallowed_exception_state_drift", risk_types)

    def test_negative_signal_on_unchanged_entity_absent(self):
        sym = _sym("fn", "src/f.py", entity_id="eid-se-6")
        sig = _risk_signal("swallowed_exception", "eid-UNRELATED", path="src/other.py")
        hypotheses = self._call(changed_symbols=[sym], risk_signals=[sig])
        risk_types = [h["risk_type"] for h in hypotheses]
        self.assertNotIn("swallowed_exception_state_drift", risk_types)

    def test_negative_low_coverage_gate_respected(self):
        sym = _sym("fn", "src/f.py", entity_id="eid-se-lc")
        sig = _risk_signal("swallowed_exception", "eid-se-lc", path="src/f.py")
        hypotheses = self._call(
            changed_files=["src/style.css"],
            changed_symbols=[sym],
            risk_signals=[sig],
            review_lead_status={"coverage_status": "low_coverage"},
        )
        risk_types = [h["risk_type"] for h in hypotheses]
        self.assertNotIn("swallowed_exception_state_drift", risk_types)

    def test_positive_why_contains_qualname(self):
        sym = _sym("retryOp", "src/retry.py", entity_id="eid-se-why")
        sig = _risk_signal("swallowed_exception", "eid-se-why", path="src/retry.py", qualname="retryOp")
        hypotheses = self._call(changed_symbols=[sym], risk_signals=[sig], review_leads={})
        h = next((h for h in hypotheses if h["risk_type"] == "swallowed_exception_state_drift"), None)
        self.assertIsNotNone(h)
        self.assertIn("retryOp", h["why"])


class TestConcreteInvariantCompaction(unittest.TestCase):
    """_compact_review_hypothesis must pass through concrete_invariant."""

    def test_compact_preserves_concrete_invariant(self):
        full = {
            "hypothesis_id": "hypothesis:async_side_effect_lifecycle_drift:abc123",
            "risk_type": "async_side_effect_lifecycle_drift",
            "confidence": "medium",
            "why": "1 signal in changed symbols",
            "concrete_invariant": "Side-effect calls and their completion must stay coupled.",
            "evidence_refs": [{"path": "src/a.ts", "line_start": 5}],
            "source_checks": ["Check A", "Check B"],
            "supporting_lead_ids": ["lead-1"],
        }
        compacted = _compact_review_hypothesis(full)
        self.assertIn("concrete_invariant", compacted)
        self.assertEqual(compacted["concrete_invariant"], full["concrete_invariant"])

    def test_compact_without_concrete_invariant_still_works(self):
        full = {
            "hypothesis_id": "hypothesis:direct_call_contract_drift:xyz",
            "risk_type": "direct_call_contract_drift",
            "confidence": "strong",
            "why": "Changed symbols have callers.",
            "evidence_refs": [],
            "source_checks": [],
            "supporting_lead_ids": [],
        }
        compacted = _compact_review_hypothesis(full)
        self.assertNotIn("concrete_invariant", compacted)
        self.assertIn("risk_type", compacted)


# ---------------------------------------------------------------------------
# Integration tests: call_tool("review_context") end-to-end with KG snapshot
# ---------------------------------------------------------------------------

def _build_risk_signal_snapshot(
    root: Path,
    *,
    risk_family: str,
    changed_file: str = "src/handler.ts",
) -> tuple[Entity, Fact]:
    """Build a minimal KG snapshot with one CodeSymbol and one code_risk_signal support fact."""
    symbol = Entity(
        kind="CodeSymbol",
        identity={
            "tenant_id": "default",
            "repo": "myrepo",
            "module": "src.handler",
            "qualname": "handleEvent",
            "symbol_kind": "function",
        },
        properties={"path": changed_file, "line": 5, "end_line": 15},
    )
    caller_symbol = Entity(
        kind="CodeSymbol",
        identity={
            "tenant_id": "default",
            "repo": "myrepo",
            "module": "src.caller",
            "qualname": "dispatchEvent",
            "symbol_kind": "function",
        },
        properties={"path": "src/caller.ts", "line": 1, "end_line": 10},
    )
    signal_fact = Fact(
        predicate="code_risk_signal",
        subject_id=symbol.entity_id,
        object_id=symbol.entity_id,
        qualifier={
            "risk_family": risk_family,
            "callee": "saveRecord",
            "qualname": "handleEvent",
            "line": 7,
        },
    )
    calls_fact = Fact(
        predicate="CALLS",
        subject_id=caller_symbol.entity_id,
        object_id=symbol.entity_id,
    )
    signal_evidence = Evidence(
        target_type="fact",
        target_id=signal_fact.fact_id,
        derivation_class="deterministic_static",
        source_system="test_extractor_v0",
        source_ref={"extractor": "test_extractor_v0"},
        bytes_ref={
            "repo": "myrepo",
            "commit_sha": "abc123",
            "path": changed_file,
            "line_start": 7,
            "line_end": 7,
        },
    )
    JsonlKgStore(root).write(
        entities=[symbol, caller_symbol],
        facts=[calls_fact],
        support_facts=[signal_fact],
        evidence=[signal_evidence],
        coverage=[],
        manifest={"version": 1},
    )
    return symbol, signal_fact


class TestReviewContextRiskSignalIntegration(unittest.TestCase):
    """End-to-end: call_tool(kg, "review_context") with code_risk_signal support facts."""

    def test_async_lifecycle_family_fires_with_real_kg(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            symbol, _ = _build_risk_signal_snapshot(
                root, risk_family="async_callback_in_iteration", changed_file="src/handler.ts"
            )
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
        hypotheses = result.get("review_hypotheses") or []
        risk_types = [h["risk_type"] for h in hypotheses]
        self.assertIn(
            "async_side_effect_lifecycle_drift",
            risk_types,
            f"expected async_side_effect_lifecycle_drift in {risk_types}",
        )
        h = next(h for h in hypotheses if h["risk_type"] == "async_side_effect_lifecycle_drift")
        self.assertTrue(h.get("evidence_refs"), "evidence_refs must be non-empty")
        self.assertIn("path", h["evidence_refs"][0])
        self.assertIn("concrete_invariant", h)
        self.assertIn("hypothesis_id", h)

    def test_swallowed_exception_family_fires_with_real_kg(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _build_risk_signal_snapshot(
                root, risk_family="swallowed_exception", changed_file="payments/processor.py"
            )
            result = call_tool(
                KgSnapshot(root),
                "review_context",
                {
                    "repo": "myrepo",
                    "changed_files": ["payments/processor.py"],
                    "changed_ranges": [{"path": "payments/processor.py", "start_line": 1, "end_line": 20}],
                    "limit": 10,
                },
            )
        hypotheses = result.get("review_hypotheses") or []
        risk_types = [h["risk_type"] for h in hypotheses]
        self.assertIn(
            "swallowed_exception_state_drift",
            risk_types,
            f"expected swallowed_exception_state_drift in {risk_types}",
        )
        h = next(h for h in hypotheses if h["risk_type"] == "swallowed_exception_state_drift")
        self.assertTrue(h.get("evidence_refs"), "evidence_refs must be non-empty")
        self.assertIn("path", h["evidence_refs"][0])
        self.assertIn("concrete_invariant", h)

    def test_families_absent_when_no_support_facts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
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
                    "changed_files": ["src/handler.ts"],
                    "changed_ranges": [{"path": "src/handler.ts", "start_line": 1, "end_line": 20}],
                    "limit": 10,
                },
            )
        hypotheses = result.get("review_hypotheses") or []
        risk_types = [h["risk_type"] for h in hypotheses]
        self.assertNotIn("async_side_effect_lifecycle_drift", risk_types)
        self.assertNotIn("swallowed_exception_state_drift", risk_types)

    def test_families_absent_for_signal_on_unchanged_file(self):
        """Signal evidence path does not overlap changed_files → families must not fire."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            # Signal is on src/other.ts (not the changed file)
            _build_risk_signal_snapshot(
                root, risk_family="async_callback_in_iteration", changed_file="src/other.ts"
            )
            result = call_tool(
                KgSnapshot(root),
                "review_context",
                {
                    "repo": "myrepo",
                    "changed_files": ["src/unchanged.ts"],  # different from signal path
                    "changed_ranges": [{"path": "src/unchanged.ts", "start_line": 1, "end_line": 5}],
                    "limit": 10,
                },
            )
        hypotheses = result.get("review_hypotheses") or []
        risk_types = [h["risk_type"] for h in hypotheses]
        self.assertNotIn(
            "async_side_effect_lifecycle_drift",
            risk_types,
            "should not fire when signal is on a different (unchanged) file",
        )


if __name__ == "__main__":
    unittest.main()
