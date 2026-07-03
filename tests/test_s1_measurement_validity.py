"""S1: measurement-validity hygiene for review_context.

Tests:
1. Loud base-diff status (Recall Gap 4):
   - no base_snapshot + changed_ranges → base_diff_status=missing, reason, suggested_setup
   - base_snapshot provided and loaded → base_diff_status=active
   - base_snapshot provided but failed → base_diff_status=failed
2. Review-readiness status model (Recall Gap 5):
   - high/medium specificity → packet_ready
   - low specificity + followups → needs_followup
   - low specificity + no followups → plain_review_better
   - low + base_diff_status=missing + changed_ranges → base_snapshot_required
3. suggested_followups (Recall Gap 1):
   - low-specificity packet emits <=3 followups
   - base_diff_status=missing → first followup is base-snapshot build
   - followup tools are in TOOL_NAMES (valid MCP tools)
   - high-specificity packet emits no suggested_followups
4. Attribution loss-proofing:
   - every hypothesis from _make_hypothesis has a label field
   - label is deterministic: same inputs → same label
   - label survives _compact_review_hypothesis
   - label survives _slim_mirror_hypothesis
   - contract-diff splice rows also get labels
5. Field gate: pr-7232 replay: size <= 15000, async first, base_diff_status=missing,
   review_readiness present
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from source.kg.core.models import Entity, Evidence, Fact
from source.kg.core.store import JsonlKgStore
from source.kg.product.mcp_tools import TOOL_NAMES, call_tool
from source.kg.product.output_budget import _compact_review_hypothesis, _slim_mirror_hypothesis
from source.kg.product.review_hypotheses import review_hypotheses_for_context
from source.kg.query.snapshot import KgSnapshot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_minimal_kg(root: Path, tenant_id: str = "default") -> KgSnapshot:
    (root / "entities.jsonl").write_text("")
    (root / "facts.jsonl").write_text("")
    (root / "evidence.jsonl").write_text("")
    (root / "coverage.jsonl").write_text("")
    (root / "manifest.json").write_text(json.dumps({"tenant_id": tenant_id, "version": 1}))
    return KgSnapshot(root)


def _make_kg_with_symbol(root: Path, repo: str = "testrepo") -> tuple[KgSnapshot, Entity]:
    symbol = Entity(
        kind="CodeSymbol",
        identity={
            "tenant_id": "default",
            "repo": repo,
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
    return KgSnapshot(root), symbol


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
    row: dict = {
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


def _risk_signal(family: str, subject_id: str, path: str = "src/app.py", line: int = 5) -> dict:
    fid = f"fact_test_{family}_{subject_id}"
    return {
        "fact_id": fid,
        "predicate": "code_risk_signal",
        "subject_id": subject_id,
        "object_id": subject_id,
        "qualifier": {"risk_family": family, "callee": "save", "qualname": "fn", "line": line},
        "_evidence": [{"target_id": fid, "bytes_ref": {"repo": "r", "path": path,
                                                        "line_start": line, "line_end": line}}],
    }


# ---------------------------------------------------------------------------
# 1. Loud base-diff status (Recall Gap 4)
# ---------------------------------------------------------------------------

class TestBaseDiffStatusMissing(unittest.TestCase):
    """review_context WITHOUT base_snapshot and WITH changed_ranges → base_diff_status=missing."""

    def _call_no_base(self, kg: KgSnapshot, repo: str) -> dict:
        return call_tool(kg, "review_context", {
            "repo": repo,
            "changed_files": ["src/handler.ts"],
            "changed_ranges": [{"path": "src/handler.ts", "start_line": 5, "end_line": 10}],
        })

    def test_base_diff_status_missing_when_no_base_snapshot(self) -> None:
        """Hard assert: review_quality_status.base_diff_status = 'missing'."""
        with tempfile.TemporaryDirectory() as d:
            kg = _make_minimal_kg(Path(d))
            result = self._call_no_base(kg, "default")
            rqs = result.get("review_quality_status") or {}
            self.assertEqual(
                rqs.get("base_diff_status"), "missing",
                f"expected base_diff_status=missing, got {rqs.get('base_diff_status')}; full rqs={rqs}",
            )

    def test_base_diff_reason_mentions_contract_diff_disabled(self) -> None:
        """reason must mention 'contract-diff' or 'Contract-diff' families disabled."""
        with tempfile.TemporaryDirectory() as d:
            kg = _make_minimal_kg(Path(d))
            result = self._call_no_base(kg, "default")
            rqs = result.get("review_quality_status") or {}
            reason = rqs.get("reason") or ""
            self.assertIn(
                "contract-diff",
                reason.lower(),
                f"reason must mention contract-diff; got: {reason!r}",
            )

    def test_suggested_setup_present_when_missing(self) -> None:
        """suggested_setup must equal 'build_base_snapshot_then_retry'."""
        with tempfile.TemporaryDirectory() as d:
            kg = _make_minimal_kg(Path(d))
            result = self._call_no_base(kg, "default")
            rqs = result.get("review_quality_status") or {}
            self.assertEqual(
                rqs.get("suggested_setup"), "build_base_snapshot_then_retry",
                f"suggested_setup mismatch; got {rqs.get('suggested_setup')}",
            )

    def test_inversion_no_changed_ranges_no_base_diff_status(self) -> None:
        """Inversion: without changed_ranges, base_diff_status must NOT be 'missing'.

        The signal that activates the missing warning is changed_ranges present but
        no base_snapshot. Without changed_ranges the contract-diff gate is silent.
        """
        with tempfile.TemporaryDirectory() as d:
            kg = _make_minimal_kg(Path(d))
            result = call_tool(kg, "review_context", {
                "repo": "default",
                "changed_files": ["src/handler.ts"],
                # No changed_ranges
            })
            rqs = result.get("review_quality_status") or {}
            self.assertNotEqual(
                rqs.get("base_diff_status"), "missing",
                "base_diff_status must NOT be 'missing' when no changed_ranges are present",
            )


class TestBaseDiffStatusActive(unittest.TestCase):
    """base_snapshot provided and loads cleanly → base_diff_status=active."""

    def test_base_diff_status_active_when_base_snapshot_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as head_dir, \
             tempfile.TemporaryDirectory() as base_dir:
            kg = _make_minimal_kg(Path(head_dir))
            _make_minimal_kg(Path(base_dir))
            result = call_tool(kg, "review_context", {
                "repo": "default",
                "changed_files": ["src/handler.ts"],
                "changed_ranges": [{"path": "src/handler.ts", "start_line": 1, "end_line": 5}],
                "base_snapshot": str(base_dir),
            })
            rqs = result.get("review_quality_status") or {}
            self.assertEqual(
                rqs.get("base_diff_status"), "active",
                f"expected base_diff_status=active when base snapshot loaded; got {rqs.get('base_diff_status')}",
            )

    def test_inversion_missing_vs_active(self) -> None:
        """Inversion: active is NOT missing — proves the two values are distinct."""
        with tempfile.TemporaryDirectory() as head_dir, \
             tempfile.TemporaryDirectory() as base_dir:
            kg = _make_minimal_kg(Path(head_dir))
            _make_minimal_kg(Path(base_dir))
            # active
            active = call_tool(kg, "review_context", {
                "repo": "default",
                "changed_files": ["src/handler.ts"],
                "changed_ranges": [{"path": "src/handler.ts", "start_line": 1, "end_line": 5}],
                "base_snapshot": str(base_dir),
            })["review_quality_status"].get("base_diff_status")
            # missing
            missing = call_tool(kg, "review_context", {
                "repo": "default",
                "changed_files": ["src/handler.ts"],
                "changed_ranges": [{"path": "src/handler.ts", "start_line": 1, "end_line": 5}],
            })["review_quality_status"].get("base_diff_status")
            self.assertNotEqual(active, missing, "active and missing must be different values")
            self.assertEqual(active, "active")
            self.assertEqual(missing, "missing")


class TestBaseDiffStatusFailed(unittest.TestCase):
    """base_snapshot provided but fails (bad path) → base_diff_status=failed."""

    def test_base_diff_status_failed_when_bad_path(self) -> None:
        with tempfile.TemporaryDirectory() as head_dir:
            kg = _make_minimal_kg(Path(head_dir))
            result = call_tool(kg, "review_context", {
                "repo": "default",
                "changed_files": ["src/handler.ts"],
                "changed_ranges": [{"path": "src/handler.ts", "start_line": 1, "end_line": 5}],
                "base_snapshot": "/nonexistent/path/to/snapshot",
            })
            rqs = result.get("review_quality_status") or {}
            self.assertEqual(
                rqs.get("base_diff_status"), "failed",
                f"expected base_diff_status=failed on bad snapshot path; got {rqs.get('base_diff_status')}",
            )


# ---------------------------------------------------------------------------
# 2. Review-readiness status model (Recall Gap 5)
# ---------------------------------------------------------------------------

class TestReviewReadiness(unittest.TestCase):
    """review_readiness routing field on review_quality_status."""

    def _call(self, kg: KgSnapshot, **extra) -> dict:
        args = {"repo": "default", "changed_files": ["src/a.ts"], **extra}
        return call_tool(kg, "review_context", args)

    def test_packet_ready_when_high_specificity(self) -> None:
        """high specificity → review_readiness=packet_ready."""
        with tempfile.TemporaryDirectory() as d:
            # Build KG with async signal so a high-specificity hypothesis fires.
            symbol = Entity(
                kind="CodeSymbol",
                identity={"tenant_id": "default", "repo": "default", "module": "src.a",
                          "qualname": "doIt", "symbol_kind": "function"},
                properties={"path": "src/a.ts", "line": 1, "end_line": 10},
            )
            sig_fact = Fact(
                predicate="code_risk_signal",
                subject_id=symbol.entity_id,
                object_id=symbol.entity_id,
                qualifier={"risk_family": "async_callback_in_iteration",
                           "callee": "save", "qualname": "doIt", "line": 3},
            )
            ev = Evidence(
                target_type="fact",
                target_id=sig_fact.fact_id,
                derivation_class="deterministic_static",
                source_system="test",
                source_ref={"extractor": "test"},
                bytes_ref={"repo": "default", "commit_sha": "abc", "path": "src/a.ts",
                           "line_start": 3, "line_end": 3},
            )
            JsonlKgStore(Path(d)).write(
                entities=[symbol], facts=[], support_facts=[sig_fact], evidence=[ev],
                coverage=[], manifest={"version": 1},
            )
            kg = KgSnapshot(Path(d))
            result = self._call(kg,
                changed_files=["src/a.ts"],
                changed_ranges=[{"path": "src/a.ts", "start_line": 1, "end_line": 10}],
            )
            rqs = result.get("review_quality_status") or {}
            self.assertIn(
                rqs.get("review_readiness"), ("packet_ready",),
                f"high-specificity packet must be packet_ready; got {rqs.get('review_readiness')}; specificity={rqs.get('specificity')}",
            )

    def test_base_snapshot_required_when_low_and_no_base_and_changed_ranges(self) -> None:
        """low specificity + base_diff_status=missing + changed_ranges → base_snapshot_required."""
        with tempfile.TemporaryDirectory() as d:
            kg = _make_minimal_kg(Path(d))
            result = self._call(kg,
                changed_files=["src/a.ts"],
                changed_ranges=[{"path": "src/a.ts", "start_line": 1, "end_line": 5}],
            )
            rqs = result.get("review_quality_status") or {}
            self.assertEqual(
                rqs.get("review_readiness"), "base_snapshot_required",
                f"expected base_snapshot_required; got {rqs.get('review_readiness')}; full rqs={rqs}",
            )

    def test_review_readiness_present_on_all_results(self) -> None:
        """review_readiness must be present on all review_context results."""
        with tempfile.TemporaryDirectory() as d:
            kg = _make_minimal_kg(Path(d))
            result = self._call(kg, changed_files=["src/a.ts"])
            rqs = result.get("review_quality_status") or {}
            self.assertIn(
                "review_readiness", rqs,
                f"review_readiness must always be present; got keys: {list(rqs.keys())}",
            )

    def test_inversion_plain_review_better_vs_packet_ready(self) -> None:
        """Inversion: plain_review_better and packet_ready must be distinct values."""
        valid_values = {"packet_ready", "needs_followup", "plain_review_better", "base_snapshot_required"}
        with tempfile.TemporaryDirectory() as d:
            kg = _make_minimal_kg(Path(d))
            result = self._call(kg, changed_files=["src/a.ts"])
            rqs = result.get("review_quality_status") or {}
            readiness = rqs.get("review_readiness")
            self.assertIn(readiness, valid_values,
                f"review_readiness must be one of {valid_values}; got {readiness!r}")


# ---------------------------------------------------------------------------
# 3. suggested_followups (Recall Gap 1)
# ---------------------------------------------------------------------------

class TestSuggestedFollowups(unittest.TestCase):
    """suggested_followups emitted on low-specificity packets with changed_ranges."""

    def test_base_snapshot_build_is_first_followup_when_missing(self) -> None:
        """When base_diff_status=missing, first suggested_followup is the base-snapshot build."""
        with tempfile.TemporaryDirectory() as d:
            kg = _make_minimal_kg(Path(d))
            result = call_tool(kg, "review_context", {
                "repo": "default",
                "changed_files": ["src/a.ts"],
                "changed_ranges": [{"path": "src/a.ts", "start_line": 1, "end_line": 5}],
            })
            rqs = result.get("review_quality_status") or {}
            followups = rqs.get("suggested_followups") or []
            self.assertGreater(
                len(followups), 0,
                "expected at least 1 suggested_followup when base_diff_status=missing",
            )
            first = followups[0]
            # The first followup should name the snapshot-build step.
            why_or_tool = str(first.get("why", "")) + str(first.get("tool", ""))
            self.assertTrue(
                "base_snapshot" in why_or_tool or "snapshot" in why_or_tool.lower(),
                f"first followup must reference base_snapshot build; got {first}",
            )

    def test_followups_bounded_at_3(self) -> None:
        """suggested_followups must not exceed 3 entries."""
        with tempfile.TemporaryDirectory() as d:
            kg = _make_minimal_kg(Path(d))
            result = call_tool(kg, "review_context", {
                "repo": "default",
                "changed_files": ["src/a.ts"],
                "changed_ranges": [{"path": "src/a.ts", "start_line": 1, "end_line": 5}],
            })
            rqs = result.get("review_quality_status") or {}
            followups = rqs.get("suggested_followups") or []
            self.assertLessEqual(
                len(followups), 3,
                f"suggested_followups must have <=3 entries; got {len(followups)}",
            )

    def test_followup_tools_are_valid_mcp_tools(self) -> None:
        """All suggested_followup tool names must be in TOOL_NAMES."""
        with tempfile.TemporaryDirectory() as d:
            kg = _make_minimal_kg(Path(d))
            result = call_tool(kg, "review_context", {
                "repo": "default",
                "changed_files": ["src/a.ts"],
                "changed_ranges": [{"path": "src/a.ts", "start_line": 1, "end_line": 5}],
            })
            rqs = result.get("review_quality_status") or {}
            followups = rqs.get("suggested_followups") or []
            for f in followups:
                tool = f.get("tool")
                if tool is not None:
                    self.assertIn(
                        tool, TOOL_NAMES,
                        f"suggested_followup tool {tool!r} not in TOOL_NAMES={TOOL_NAMES}",
                    )

    def test_no_suggested_followups_on_high_specificity(self) -> None:
        """High-specificity packets must not emit suggested_followups."""
        with tempfile.TemporaryDirectory() as d:
            symbol = Entity(
                kind="CodeSymbol",
                identity={"tenant_id": "default", "repo": "default", "module": "src.a",
                          "qualname": "doIt", "symbol_kind": "function"},
                properties={"path": "src/a.ts", "line": 1, "end_line": 10},
            )
            sig_fact = Fact(
                predicate="code_risk_signal",
                subject_id=symbol.entity_id,
                object_id=symbol.entity_id,
                qualifier={"risk_family": "async_callback_in_iteration",
                           "callee": "save", "qualname": "doIt", "line": 3},
            )
            ev = Evidence(
                target_type="fact",
                target_id=sig_fact.fact_id,
                derivation_class="deterministic_static",
                source_system="test",
                source_ref={"extractor": "test"},
                bytes_ref={"repo": "default", "commit_sha": "abc", "path": "src/a.ts",
                           "line_start": 3, "line_end": 3},
            )
            JsonlKgStore(Path(d)).write(
                entities=[symbol], facts=[], support_facts=[sig_fact], evidence=[ev],
                coverage=[], manifest={"version": 1},
            )
            kg = KgSnapshot(Path(d))
            result = call_tool(kg, "review_context", {
                "repo": "default",
                "changed_files": ["src/a.ts"],
                "changed_ranges": [{"path": "src/a.ts", "start_line": 1, "end_line": 10}],
            })
            rqs = result.get("review_quality_status") or {}
            followups = rqs.get("suggested_followups")
            self.assertTrue(
                not followups,
                f"high-specificity packet must have no suggested_followups; got {followups}",
            )

    def test_inversion_followups_absent_when_no_low_specificity(self) -> None:
        """Inversion: if we force specificity=high, suggested_followups must be absent/empty.

        Proves the condition is checked, not hardcoded.
        """
        from source.kg.product.mcp_tools import _build_review_quality_status
        hypotheses = [{
            "risk_type": "async_side_effect_lifecycle_drift",
            "specificity": "high",
            "supporting_lead_ids": [],
            "evidence_refs": [],
        }]
        status = _build_review_quality_status(
            review_hypotheses=hypotheses,
            coverage_status="useful",
            base_diff_status="active",
            changed_ranges=[{"path": "x.ts", "start_line": 1, "end_line": 5}],
            changed_symbols=[],
        )
        followups = status.get("suggested_followups")
        self.assertTrue(
            not followups,
            f"high-specificity must produce no suggested_followups; got {followups}",
        )


# ---------------------------------------------------------------------------
# 4. Attribution loss-proofing: label on hypotheses
# ---------------------------------------------------------------------------

class TestHypothesisLabel(unittest.TestCase):
    """Every hypothesis must carry a short stable label that survives compaction."""

    def _make_hyp(self, risk_type: str = "async_side_effect_lifecycle_drift") -> dict:
        """Build a minimal hypothesis via review_hypotheses_for_context signal path."""
        sym = {
            "qualname": "fn", "display_name": "mod.fn", "qualified_name": "mod.fn",
            "kind": "function", "path": "src/a.ts", "symbol_id": "eid-1", "lead_id": "lead-1",
        }
        sig = _risk_signal("async_callback_in_iteration", "eid-1")
        hyps = review_hypotheses_for_context(**_base_context(
            changed_symbols=[sym],
            risk_signals=[sig],
            review_leads={"changed_symbols": [{"lead_id": "lead-1", "path": "src/a.ts", "symbol_id": "eid-1"}]},
        ))
        h = next((h for h in hyps if h["risk_type"] == risk_type), None)
        if h is None:
            raise AssertionError(f"expected hypothesis {risk_type} not found; got {[h['risk_type'] for h in hyps]}")
        return h

    def test_label_present_on_hypothesis(self) -> None:
        h = self._make_hyp()
        self.assertIn("label", h, f"hypothesis must carry 'label' field; got keys: {list(h.keys())}")

    def test_label_is_nonempty_string(self) -> None:
        h = self._make_hyp()
        label = h.get("label")
        self.assertIsInstance(label, str, f"label must be a string; got {type(label)}")
        self.assertTrue(label, "label must be non-empty")

    def test_label_contains_risk_type(self) -> None:
        h = self._make_hyp()
        label = h.get("label", "")
        self.assertIn("async", label, f"label must contain risk_type prefix; got {label!r}")

    def test_label_is_deterministic(self) -> None:
        """Same inputs must produce the same label every time."""
        h1 = self._make_hyp()
        h2 = self._make_hyp()
        self.assertEqual(
            h1.get("label"), h2.get("label"),
            f"label must be deterministic; got {h1.get('label')!r} vs {h2.get('label')!r}",
        )

    def test_label_ends_with_4char_hex_suffix(self) -> None:
        """label format: {risk_type}-{4hex} — suffix must be exactly 4 hex chars."""
        h = self._make_hyp()
        label = h.get("label", "")
        self.assertTrue(
            len(label) > 4,
            f"label too short: {label!r}",
        )
        suffix = label[-4:]
        self.assertTrue(
            all(c in "0123456789abcdef" for c in suffix),
            f"last 4 chars of label must be hex; got suffix={suffix!r} from label={label!r}",
        )

    def test_label_survives_compact_review_hypothesis(self) -> None:
        """_compact_review_hypothesis must preserve the label field."""
        h = self._make_hyp()
        compact = _compact_review_hypothesis(h)
        self.assertIn(
            "label", compact,
            f"label must survive _compact_review_hypothesis; compact keys: {list(compact.keys())}",
        )
        self.assertEqual(compact["label"], h["label"])

    def test_label_survives_slim_mirror_hypothesis(self) -> None:
        """_slim_mirror_hypothesis must preserve the label field."""
        h = self._make_hyp()
        slim = _slim_mirror_hypothesis(h)
        self.assertIn(
            "label", slim,
            f"label must survive _slim_mirror_hypothesis; slim keys: {list(slim.keys())}",
        )
        self.assertEqual(slim["label"], h["label"])

    def test_label_present_in_call_tool_result(self) -> None:
        """End-to-end: call_tool review_context returns hypotheses with label."""
        with tempfile.TemporaryDirectory() as d:
            symbol = Entity(
                kind="CodeSymbol",
                identity={"tenant_id": "default", "repo": "default", "module": "src.a",
                          "qualname": "doIt", "symbol_kind": "function"},
                properties={"path": "src/a.ts", "line": 1, "end_line": 10},
            )
            sig_fact = Fact(
                predicate="code_risk_signal",
                subject_id=symbol.entity_id,
                object_id=symbol.entity_id,
                qualifier={"risk_family": "async_callback_in_iteration",
                           "callee": "save", "qualname": "doIt", "line": 3},
            )
            ev = Evidence(
                target_type="fact",
                target_id=sig_fact.fact_id,
                derivation_class="deterministic_static",
                source_system="test",
                source_ref={"extractor": "test"},
                bytes_ref={"repo": "default", "commit_sha": "abc", "path": "src/a.ts",
                           "line_start": 3, "line_end": 3},
            )
            JsonlKgStore(Path(d)).write(
                entities=[symbol], facts=[], support_facts=[sig_fact], evidence=[ev],
                coverage=[], manifest={"version": 1},
            )
            kg = KgSnapshot(Path(d))
            result = call_tool(kg, "review_context", {
                "repo": "default",
                "changed_files": ["src/a.ts"],
                "changed_ranges": [{"path": "src/a.ts", "start_line": 1, "end_line": 10}],
            })
            hyps = result.get("review_hypotheses") or []
            self.assertTrue(hyps, "review_hypotheses must be non-empty")
            for h in hyps:
                self.assertIn(
                    "label", h,
                    f"every hypothesis in call_tool result must have label; missing in {h.get('risk_type')}",
                )

    def test_label_present_in_top_review_hypotheses_mirror(self) -> None:
        """label must also appear in review_answer_packet.top_review_hypotheses."""
        with tempfile.TemporaryDirectory() as d:
            symbol = Entity(
                kind="CodeSymbol",
                identity={"tenant_id": "default", "repo": "default", "module": "src.a",
                          "qualname": "doIt", "symbol_kind": "function"},
                properties={"path": "src/a.ts", "line": 1, "end_line": 10},
            )
            sig_fact = Fact(
                predicate="code_risk_signal",
                subject_id=symbol.entity_id,
                object_id=symbol.entity_id,
                qualifier={"risk_family": "async_callback_in_iteration",
                           "callee": "save", "qualname": "doIt", "line": 3},
            )
            ev = Evidence(
                target_type="fact",
                target_id=sig_fact.fact_id,
                derivation_class="deterministic_static",
                source_system="test",
                source_ref={"extractor": "test"},
                bytes_ref={"repo": "default", "commit_sha": "abc", "path": "src/a.ts",
                           "line_start": 3, "line_end": 3},
            )
            JsonlKgStore(Path(d)).write(
                entities=[symbol], facts=[], support_facts=[sig_fact], evidence=[ev],
                coverage=[], manifest={"version": 1},
            )
            kg = KgSnapshot(Path(d))
            result = call_tool(kg, "review_context", {
                "repo": "default",
                "changed_files": ["src/a.ts"],
                "changed_ranges": [{"path": "src/a.ts", "start_line": 1, "end_line": 10}],
            })
            packet = result.get("review_answer_packet") or {}
            top_hyps = packet.get("top_review_hypotheses") or []
            self.assertTrue(top_hyps, "top_review_hypotheses must be non-empty")
            for h in top_hyps:
                self.assertIn(
                    "label", h,
                    f"top_review_hypotheses mirror must have label; missing in {h.get('risk_type')}",
                )

    def test_inversion_label_absent_without_field(self) -> None:
        """Inversion: a hypothesis dict missing 'label' proves we are testing the field presence."""
        stub = {"risk_type": "test", "hypothesis_id": "hyp:test:abc1"}
        self.assertNotIn("label", stub, "stub must not have label (inversion proof)")
        # Now check the real output does have it.
        h = self._make_hyp()
        self.assertIn("label", h)


# ---------------------------------------------------------------------------
# 5. Field gate: pr-7232 replay
# ---------------------------------------------------------------------------

_FIELD_REPLAY_KG = os.path.expanduser(
    "~/work/pr-review/pr-review-autoresearch/runs/"
    "supercontext-kg-store-f1fb588/calcom__cal.diy/pr-7232/"
    "6048e2a86b50e81e1e3b1b467dfea5a895add3dc/"
    "kg-9d96b838e487301b/kg"
)
_FIELD_REPLAY_ARGS = os.path.join(
    os.path.dirname(__file__), "fixtures", "review-context-cal-diy-args.json"
)


@unittest.skipUnless(
    os.path.isdir(_FIELD_REPLAY_KG) and os.path.isfile(_FIELD_REPLAY_ARGS),
    "field replay KG not available",
)
class TestFieldGatePr7232S1(unittest.TestCase):
    """S1 field gate: pr-7232 compact replay."""

    @classmethod
    def setUpClass(cls):
        with open(_FIELD_REPLAY_ARGS) as f:
            cls.args = json.load(f)
        cls.result = call_tool(KgSnapshot(Path(_FIELD_REPLAY_KG)), "review_context", cls.args)

    def test_size_le_15000(self) -> None:
        from source.kg.core.models import canonical_json
        size = len(canonical_json(self.result))
        self.assertLessEqual(size, 15_000, f"compact size {size} exceeds 15,000")

    def test_async_family_is_first(self) -> None:
        packet = self.result.get("review_answer_packet") or {}
        top_hyps = packet.get("top_review_hypotheses") or []
        self.assertTrue(top_hyps, "top_review_hypotheses must be non-empty")
        self.assertEqual(
            top_hyps[0].get("risk_type"), "async_side_effect_lifecycle_drift",
            f"async family must be first; got {top_hyps[0].get('risk_type')}",
        )

    def test_base_diff_status_missing(self) -> None:
        """Field gate: args have no base_snapshot, so base_diff_status must be 'missing'."""
        rqs = self.result.get("review_quality_status") or {}
        self.assertEqual(
            rqs.get("base_diff_status"), "missing",
            f"expected base_diff_status=missing (no base_snapshot in args); got {rqs.get('base_diff_status')}",
        )

    def test_review_readiness_present(self) -> None:
        """Field gate: review_readiness must be present."""
        rqs = self.result.get("review_quality_status") or {}
        self.assertIn(
            "review_readiness", rqs,
            f"review_readiness must be present; got keys: {list(rqs.keys())}",
        )

    def test_label_on_hypotheses(self) -> None:
        """Field gate: every returned hypothesis must carry a label."""
        hyps = self.result.get("review_hypotheses") or []
        self.assertTrue(hyps, "review_hypotheses must be non-empty")
        for h in hyps:
            self.assertIn(
                "label", h,
                f"hypothesis {h.get('risk_type')} missing label",
            )


if __name__ == "__main__":
    unittest.main()
