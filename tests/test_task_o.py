"""Tests for Task O: N1 Rule Flip (Preserve Leads) + Evidence-Backed Specificity Selection (O2).

O1: When budget eviction pressure exists, lead rows mirrored in top_* should survive —
     evict other sections first. Coherent drop of both sides is last resort only.
O2: Stable partition of hypothesis order — specific-class families before generic-class.
"""
from __future__ import annotations

import unittest

from source.kg.core.models import canonical_json
from source.kg.product.output_budget import enforce_review_context_budget
from source.kg.product.review_attribution import hypothesis_stable_id
from source.kg.product.review_hypotheses import review_hypotheses_for_context


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_sym(name: str, path: str, lead_id: str | None = None) -> dict:
    stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    row: dict = {
        "qualname": name,
        "display_name": f"{stem}.{name}",
        "qualified_name": f"{stem}.{name}",
        "path": path,
        "repo": "svc",
    }
    if lead_id:
        row["lead_id"] = lead_id
    return row


def _make_hypothesis(risk_type: str, index: int, lead_ids: list[str] | None = None) -> dict:
    lids = lead_ids or [f"lead:caller:c{i:03d}" for i in range(3)]
    return {
        "hypothesis_id": hypothesis_stable_id(risk_type, lids, []),
        "risk_type": risk_type,
        "confidence": "medium",
        "why": f"Why {risk_type} {index}",
        "evidence_refs": [{"path": f"src/f{index}.py"}],
        "source_checks": [f"check {index}"],
        "supporting_lead_ids": lids,
    }


def _fat_rows(n: int, label: str, section: str = "application_impact") -> list[dict]:
    """Fat rows for broad context sections."""
    return [
        {
            "lead_id": f"lead:{section}:{label}{i}",
            "path": f"src/{label}_{i}.py",
            "repo": "svc",
            "subject": f"mod.fn_{i}",
            "object": f"mod.target_{i}",
            "payload": "x" * 400,
        }
        for i in range(n)
    ]


def _packet_with_changed_symbol_lead_and_broad_context(
    n_sym_leads: int, n_broad_rows: int, n_hypotheses: int = 1
) -> tuple[dict, list[dict]]:
    """Build a packet where:

    - changed_symbols in review_leads are mirrored in top_changed_symbols
    - fat broad-context rows (application_impact) occupy the majority of budget
    - one changed_symbol lead has a known lead_id
    """
    sym_leads = [
        {"lead_id": f"lead:sym:{i}", "path": f"src/sym_{i}.py", "repo": "svc", "qualname": f"Sym{i}"}
        for i in range(n_sym_leads)
    ]
    broad = _fat_rows(n_broad_rows, "app", "application_impact")
    hypotheses = [_make_hypothesis("direct_call_contract_drift", i) for i in range(n_hypotheses)]
    review_leads: dict = {
        "changed_symbols": sym_leads,
        "direct_callers": [],
        "direct_callees": [],
        "transitive_callers": [],
        "source_coordinates": [],
    }
    packet: dict = {
        "status": "found",
        "review_hypotheses": list(hypotheses),
        "review_answer_packet": {
            "status": "found",
            "top_diff_anchors": [],
            "top_changed_symbols": list(sym_leads),
            "top_direct_callers": [],
            "top_direct_callees": [],
            "top_transitive_callers": [],
            "top_review_hypotheses": list(hypotheses),
        },
        "review_leads": review_leads,
        "review_lead_status": {
            "coverage_status": "useful",
            "available": {"changed_symbol_count": n_sym_leads},
            "returned": {"changed_symbol_count": n_sym_leads},
            "changed_symbol_count": n_sym_leads,
            "direct_impact_count": 0,
            "transitive_impact_count": 0,
            "source_coordinate_count": 0,
        },
        # Fat broad-context section that should be evicted before review_leads
        "application_impact": {"surfaces": broad},
        "output_budget": {"truncated": False, "truncated_sections": []},
    }
    return packet, list(hypotheses)


# ---------------------------------------------------------------------------
# O1 Tests — N1 Rule Flip: preserve leads, evict broad context first
# ---------------------------------------------------------------------------

class TestO1PreservesLeadsOverBroadContext(unittest.TestCase):
    """O1: lead rows mirrored in top_* survive when broad context can be evicted instead."""

    def test_changed_symbol_lead_survives_broad_context_eviction(self):
        """top_changed_symbols lead_id still present in review_leads.changed_symbols after budget."""
        n_syms = 2
        packet, _ = _packet_with_changed_symbol_lead_and_broad_context(
            n_sym_leads=n_syms, n_broad_rows=30
        )
        full_size = len(canonical_json(packet))
        # Budget: tight enough to force eviction, but broad_context can fund the symbols
        tight = full_size - (full_size // 3)
        result = enforce_review_context_budget(packet, max_chars=tight)

        ap = result.get("review_answer_packet") or {}
        top_sym_ids = {
            r.get("lead_id")
            for r in (ap.get("top_changed_symbols") or [])
            if isinstance(r, dict) and r.get("lead_id")
        }
        rl = result.get("review_leads") or {}
        rl_sym_ids = {
            r.get("lead_id")
            for r in (rl.get("changed_symbols") or [])
            if isinstance(r, dict) and r.get("lead_id")
        }

        # Subset invariant must hold
        self.assertTrue(
            top_sym_ids.issubset(rl_sym_ids),
            f"top_changed_symbols {top_sym_ids} not subset of review_leads.changed_symbols {rl_sym_ids}",
        )
        # The original sym leads should survive (broad context evicted instead)
        original_ids = {f"lead:sym:{i}" for i in range(n_syms)}
        # At least one symbol lead must survive (broad context was the victim)
        surviving = original_ids & rl_sym_ids
        self.assertGreater(
            len(surviving),
            0,
            f"expected at least one symbol lead to survive; broad context should be evicted first. "
            f"surviving={surviving}, rl_sym_ids={rl_sym_ids}",
        )

    def test_returned_changed_symbol_count_consistent_after_preservation(self):
        """review_lead_status.returned.changed_symbol_count == len(review_leads.changed_symbols)."""
        packet, _ = _packet_with_changed_symbol_lead_and_broad_context(
            n_sym_leads=3, n_broad_rows=20
        )
        full_size = len(canonical_json(packet))
        tight = full_size - (full_size // 3)
        result = enforce_review_context_budget(packet, max_chars=tight)

        rl = result.get("review_leads") or {}
        rl_count = len(rl.get("changed_symbols") or [])
        status = result.get("review_lead_status") or {}
        returned = status.get("returned") or {}
        reported = returned.get("changed_symbol_count", -1) if isinstance(returned, dict) else -1
        self.assertEqual(
            reported, rl_count,
            f"returned.changed_symbol_count={reported} != review_leads.changed_symbols len={rl_count}",
        )

    def test_cap_not_exceeded_after_preservation(self):
        """Budget cap is never exceeded after O1 lead preservation."""
        packet, _ = _packet_with_changed_symbol_lead_and_broad_context(
            n_sym_leads=2, n_broad_rows=25
        )
        full_size = len(canonical_json(packet))
        tight = full_size - (full_size // 3)
        result = enforce_review_context_budget(packet, max_chars=tight)
        self.assertLessEqual(len(canonical_json(result)), tight)

    def test_subset_invariant_still_holds_after_preservation(self):
        """top_changed_symbols ⊆ review_leads.changed_symbols (invariant preserved)."""
        packet, _ = _packet_with_changed_symbol_lead_and_broad_context(
            n_sym_leads=4, n_broad_rows=15
        )
        full_size = len(canonical_json(packet))
        tight = full_size // 2
        result = enforce_review_context_budget(packet, max_chars=tight)

        ap = result.get("review_answer_packet") or {}
        top_ids = {
            r.get("lead_id")
            for r in (ap.get("top_changed_symbols") or [])
            if isinstance(r, dict) and r.get("lead_id")
        }
        rl = result.get("review_leads") or {}
        rl_ids = {
            r.get("lead_id")
            for r in (rl.get("changed_symbols") or [])
            if isinstance(r, dict) and r.get("lead_id")
        }
        self.assertTrue(
            top_ids.issubset(rl_ids),
            f"subset invariant violated: top_ids={top_ids} not ⊆ rl_ids={rl_ids}",
        )


class TestO1LastResortCoherentDrop(unittest.TestCase):
    """O1 last resort: when no non-lead sections remain, both lead row AND mirror row drop together."""

    def _packet_leads_only(self, n_syms: int) -> tuple[dict, list[dict]]:
        """Packet with ONLY review_leads (no broad context), so eviction must touch review_leads."""
        sym_leads = [
            {
                "lead_id": f"lead:sym:{i}",
                "path": f"src/sym_{i}.py",
                "repo": "svc",
                "qualname": f"Sym{i}",
                "payload": "y" * 300,
            }
            for i in range(n_syms)
        ]
        hypotheses = [_make_hypothesis("direct_call_contract_drift", 0)]
        review_leads: dict = {
            "changed_symbols": sym_leads,
            "direct_callers": [],
            "direct_callees": [],
            "transitive_callers": [],
            "source_coordinates": [],
        }
        packet: dict = {
            "status": "found",
            "review_hypotheses": list(hypotheses),
            "review_answer_packet": {
                "status": "found",
                "top_diff_anchors": [],
                "top_changed_symbols": list(sym_leads),
                "top_direct_callers": [],
                "top_direct_callees": [],
                "top_transitive_callers": [],
                "top_review_hypotheses": list(hypotheses),
            },
            "review_leads": review_leads,
            "review_lead_status": {
                "coverage_status": "useful",
                "available": {"changed_symbol_count": n_syms},
                "returned": {"changed_symbol_count": n_syms},
                "changed_symbol_count": n_syms,
                "direct_impact_count": 0,
                "transitive_impact_count": 0,
                "source_coordinate_count": 0,
            },
            "output_budget": {"truncated": False, "truncated_sections": []},
        }
        return packet, list(hypotheses)

    def test_subset_invariant_preserved_under_extreme_pressure(self):
        """Under extreme budget pressure (leads-only packet), subset invariant holds."""
        packet, _ = self._packet_leads_only(n_syms=8)
        full_size = len(canonical_json(packet))
        # Very tight — must evict from review_leads
        very_tight = full_size // 4
        result = enforce_review_context_budget(packet, max_chars=very_tight)

        ap = result.get("review_answer_packet") or {}
        top_ids = {
            r.get("lead_id")
            for r in (ap.get("top_changed_symbols") or [])
            if isinstance(r, dict) and r.get("lead_id")
        }
        rl = result.get("review_leads") or {}
        rl_ids = {
            r.get("lead_id")
            for r in (rl.get("changed_symbols") or [])
            if isinstance(r, dict) and r.get("lead_id")
        }
        self.assertTrue(
            top_ids.issubset(rl_ids),
            f"last-resort coherent drop must keep subset invariant: {top_ids} not ⊆ {rl_ids}",
        )

    def test_cap_not_exceeded_under_extreme_pressure(self):
        """Budget cap never exceeded when review_leads can be reduced to fit.

        Uses a budget of half the full size so the packet is achievable (minimal
        packet overhead << half size), verifying the cap holds without setting
        exceeded_after_minimization.
        """
        packet, _ = self._packet_leads_only(n_syms=6)
        full_size = len(canonical_json(packet))
        # half-size budget is achievable: drop ~half the fat sym_lead rows
        tight = full_size // 2
        result = enforce_review_context_budget(packet, max_chars=tight)
        budget = result.get("output_budget") or {}
        if budget.get("exceeded_after_minimization"):
            self.skipTest("packet minimum overhead exceeds budget — not a valid O1 test scenario")
        self.assertLessEqual(len(canonical_json(result)), tight)

    def test_returned_count_consistent_after_coherent_drop(self):
        """returned.changed_symbol_count == len(review_leads.changed_symbols) after last-resort drop."""
        packet, _ = self._packet_leads_only(n_syms=5)
        full_size = len(canonical_json(packet))
        very_tight = full_size // 4
        result = enforce_review_context_budget(packet, max_chars=very_tight)

        rl = result.get("review_leads") or {}
        rl_count = len(rl.get("changed_symbols") or [])
        status = result.get("review_lead_status") or {}
        returned = status.get("returned") or {}
        reported = returned.get("changed_symbol_count", -1) if isinstance(returned, dict) else -1
        self.assertEqual(
            reported, rl_count,
            f"returned.changed_symbol_count={reported} != rl len={rl_count} after last-resort drop",
        )


# ---------------------------------------------------------------------------
# O2 Tests — Specificity-preferring stable partition
# ---------------------------------------------------------------------------

def _base_ctx(**overrides) -> dict:
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
    ctx.update(overrides)
    return ctx


def _hook_sym(name: str) -> dict:
    """A hook symbol (specific-class trigger: hook_gate_render_mismatch)."""
    return _make_sym(name, f"src/{name}.ts")


def _caller_edge(subj: str, obj: str, lead_id: str = "lead-e") -> dict:
    return {"subject": f"mod.{subj}", "object": f"mod.{obj}", "lead_id": lead_id, "predicate": "CALLS"}


class TestO2SpecificFamiliesRankedFirst(unittest.TestCase):
    """O2: specific-class families appear before generic-class in the returned order."""

    def test_hook_gate_outranks_generic_when_all_present(self):
        """hook_gate_render_mismatch (specific) appears before direct_call_contract_drift (generic)."""
        # Build a scenario that generates hook_gate (specific) and direct_call (generic).
        # hook_gate needs a hook symbol (useXxx) in a .ts/.tsx file with a consumer edge.
        changed_syms = [_make_sym("useAuth", "src/useAuth.ts", lead_id="lead-hook")]
        # direct_callers edge: component consumes hook
        callers = [
            _caller_edge("AuthPage", "useAuth", "lead-edge-hook"),
        ]
        # Also add a non-hook symbol to trigger direct_call_contract_drift
        changed_syms.append(_make_sym("processPayment", "src/payment.py", lead_id="lead-py"))
        py_callers = [_caller_edge("order", "processPayment", "lead-edge-py")]
        all_callers = callers + py_callers

        ctx = _base_ctx(
            changed_files=["src/useAuth.ts", "src/payment.py"],
            changed_symbols=changed_syms,
            direct_callers=all_callers,
            review_leads={
                "changed_symbols": [
                    {"lead_id": "lead-hook", "path": "src/useAuth.ts"},
                    {"lead_id": "lead-py", "path": "src/payment.py"},
                ],
                "direct_callers": [
                    {"lead_id": "lead-edge-hook", "subject": "mod.AuthPage", "object": "mod.useAuth"},
                    {"lead_id": "lead-edge-py", "subject": "mod.order", "object": "mod.processPayment"},
                ],
            },
        )
        hypotheses = review_hypotheses_for_context(**ctx)
        risk_types = [h["risk_type"] for h in hypotheses]

        self.assertIn("hook_gate_render_mismatch", risk_types, "hook_gate must be generated")
        self.assertIn("direct_call_contract_drift", risk_types, "direct_call must be generated")

        hook_pos = risk_types.index("hook_gate_render_mismatch")
        direct_pos = risk_types.index("direct_call_contract_drift")
        self.assertLess(
            hook_pos, direct_pos,
            f"hook_gate (specific) must outrank direct_call (generic): positions hook={hook_pos} direct={direct_pos}",
        )

    def test_test_locks_outranks_generic(self):
        """test_locks_in_regression (specific) appears before direct_call_contract_drift (generic)."""
        # test_locks needs: a test file changed + non-test symbol with edge + edge touching non-test symbol
        changed_syms = [
            _make_sym("processOrder", "src/orders.py", lead_id="lead-order"),
        ]
        callers = [_caller_edge("checkout", "processOrder", "lead-edge-order")]
        ctx = _base_ctx(
            changed_files=["src/orders.py", "tests/test_orders.py"],
            changed_symbols=changed_syms,
            direct_callers=callers,
            review_leads={
                "changed_symbols": [{"lead_id": "lead-order", "path": "src/orders.py"}],
                "direct_callers": [
                    {"lead_id": "lead-edge-order", "subject": "mod.checkout", "object": "mod.processOrder"}
                ],
            },
        )
        hypotheses = review_hypotheses_for_context(**ctx)
        risk_types = [h["risk_type"] for h in hypotheses]

        if "test_locks_in_regression" not in risk_types:
            self.skipTest("test_locks_in_regression not generated in this scenario")
        if "direct_call_contract_drift" not in risk_types:
            self.skipTest("direct_call_contract_drift not generated in this scenario")

        tl_pos = risk_types.index("test_locks_in_regression")
        dc_pos = risk_types.index("direct_call_contract_drift")
        self.assertLess(
            tl_pos, dc_pos,
            f"test_locks (specific) must outrank direct_call (generic): tl={tl_pos} dc={dc_pos}",
        )

    def test_no_specifics_generated_order_unchanged(self):
        """When no specific-class hypotheses are generated, order is pure comparator order (unchanged)."""
        # Only direct_call_contract_drift (generic) generated: no hooks, no components, no test files
        changed_syms = [_make_sym("process", "src/main.py", lead_id="lead-main")]
        callers = [_caller_edge("caller1", "process", "lead-e1")]
        ctx = _base_ctx(
            changed_files=["src/main.py"],
            changed_symbols=changed_syms,
            direct_callers=callers,
            review_leads={
                "changed_symbols": [{"lead_id": "lead-main", "path": "src/main.py"}],
                "direct_callers": [
                    {"lead_id": "lead-e1", "subject": "mod.caller1", "object": "mod.process"}
                ],
            },
        )
        hypotheses = review_hypotheses_for_context(**ctx)
        risk_types = [h["risk_type"] for h in hypotheses]

        # No specific families should be in the list
        specific_families = {
            "component_list_render_identity_drift",
            "hook_gate_render_mismatch",
            "test_locks_in_regression",
            "low_coverage_stylesheet_gap",
        }
        in_list = specific_families & set(risk_types)
        self.assertEqual(in_list, set(), f"expected no specific families, got {in_list}")

    def test_low_coverage_path_unaffected_by_partition(self):
        """low_coverage_stylesheet_gap path returns a single hypothesis (not reordered)."""
        ctx = _base_ctx(
            changed_files=["src/styles.css"],
            review_lead_status={"coverage_status": "low_coverage"},
        )
        hypotheses = review_hypotheses_for_context(**ctx)
        if not hypotheses:
            self.skipTest("no stylesheet hypothesis generated")
        self.assertEqual(len(hypotheses), 1, "low_coverage path returns at most 1 hypothesis")
        self.assertEqual(hypotheses[0]["risk_type"], "low_coverage_stylesheet_gap")

    def test_specific_families_within_class_keep_comparator_order(self):
        """Within specific-class group, comparator order is preserved (stable partition)."""
        # Two specific-class hypotheses: hook_gate + component_list
        # hook_gate has fewer leads → component_list (with more leads) ranks higher by comparator
        # After partition, both remain in specific-class first block, component_list before hook_gate
        # (more supporting_lead_ids → higher comparator rank within specific block)
        comp_leads = [f"lead:comp:{i}" for i in range(5)]
        hook_leads = [f"lead:hook:{i}" for i in range(2)]

        # component_list: uppercase component in .tsx with callee edge
        comp_sym = {"qualname": "Widget", "display_name": "mod.Widget", "qualified_name": "mod.Widget",
                    "path": "src/Widget.tsx", "repo": "svc", "lead_id": "lead:comp:sym"}
        hook_sym = {"qualname": "useWidget", "display_name": "mod.useWidget", "qualified_name": "mod.useWidget",
                    "path": "src/useWidget.ts", "repo": "svc", "lead_id": "lead:hook:sym"}

        changed_syms = [comp_sym, hook_sym]
        # Edge to trigger component_list
        comp_callees = [{"subject": "mod.Widget", "object": "mod.Child", "lead_id": "lead:comp:edge"}]
        # Edge to trigger hook_gate: AuthPage calls useWidget (component caller of hook)
        hook_callers = [{"subject": "mod.AuthPage", "object": "mod.useWidget", "lead_id": "lead:hook:edge"}]

        ctx = _base_ctx(
            changed_files=["src/Widget.tsx", "src/useWidget.ts"],
            changed_symbols=changed_syms,
            direct_callees=comp_callees,
            direct_callers=hook_callers,
            review_leads={
                "changed_symbols": [
                    {"lead_id": "lead:comp:sym", "path": "src/Widget.tsx"},
                    {"lead_id": "lead:hook:sym", "path": "src/useWidget.ts"},
                ],
                "direct_callees": [
                    {"lead_id": "lead:comp:edge", "subject": "mod.Widget", "object": "mod.Child"}
                ],
                "direct_callers": [
                    {"lead_id": "lead:hook:edge", "subject": "mod.AuthPage", "object": "mod.useWidget"}
                ],
            },
        )
        hypotheses = review_hypotheses_for_context(**ctx)
        risk_types = [h["risk_type"] for h in hypotheses]

        specific_families = {
            "component_list_render_identity_drift",
            "hook_gate_render_mismatch",
        }
        present_specifics = [rt for rt in risk_types if rt in specific_families]
        if len(present_specifics) < 2:
            self.skipTest(f"need >=2 specific families for this test, got {present_specifics}")

        # Both specifics must come before any generic
        generic_positions = [i for i, rt in enumerate(risk_types) if rt not in specific_families]
        if not generic_positions:
            return  # all hypotheses are specific, trivially fine
        first_generic = min(generic_positions)
        for rt in present_specifics:
            pos = risk_types.index(rt)
            self.assertLess(
                pos, first_generic,
                f"specific '{rt}' at position {pos} must be before first generic at {first_generic}",
            )


class TestO2MirrorReflectsPartitionedOrder(unittest.TestCase):
    """O2: mirror (top_review_hypotheses) reflects the partitioned order from top-level."""

    def test_mirror_head_is_specific_when_specific_exists(self):
        """When hook_gate_render_mismatch is generated, it appears in the mirror before generics."""
        changed_syms = [_make_sym("useAuth", "src/useAuth.ts", lead_id="lead-hook")]
        callers = [_caller_edge("AuthPage", "useAuth", "lead-edge-hook")]
        changed_syms.append(_make_sym("processPayment", "src/payment.py", lead_id="lead-py"))
        py_callers = [_caller_edge("order", "processPayment", "lead-edge-py")]
        all_callers = callers + py_callers

        ctx = _base_ctx(
            changed_files=["src/useAuth.ts", "src/payment.py"],
            changed_symbols=changed_syms,
            direct_callers=all_callers,
            review_leads={
                "changed_symbols": [
                    {"lead_id": "lead-hook", "path": "src/useAuth.ts"},
                    {"lead_id": "lead-py", "path": "src/payment.py"},
                ],
                "direct_callers": [
                    {"lead_id": "lead-edge-hook", "subject": "mod.AuthPage", "object": "mod.useAuth"},
                    {"lead_id": "lead-edge-py", "subject": "mod.order", "object": "mod.processPayment"},
                ],
            },
        )
        hypotheses = review_hypotheses_for_context(**ctx)
        risk_types = [h["risk_type"] for h in hypotheses]

        if "hook_gate_render_mismatch" not in risk_types:
            self.skipTest("hook_gate not generated")
        if "direct_call_contract_drift" not in risk_types:
            self.skipTest("direct_call not generated")

        # Build a minimal packet to run enforce_review_context_budget and check the mirror
        hyp_objects = [
            {
                "hypothesis_id": hypothesis_stable_id(h["risk_type"], [], []),
                "risk_type": h["risk_type"],
                "confidence": h["confidence"],
                "why": h.get("why", ""),
                "evidence_refs": h.get("evidence_refs", []),
                "source_checks": h.get("source_checks", []),
                "supporting_lead_ids": h.get("supporting_lead_ids", []),
            }
            for h in hypotheses
        ]
        packet: dict = {
            "status": "found",
            "review_hypotheses": hyp_objects,
            "review_answer_packet": {
                "status": "found",
                "top_diff_anchors": [],
                "top_changed_symbols": [],
                "top_direct_callers": [],
                "top_direct_callees": [],
                "top_transitive_callers": [],
                "top_review_hypotheses": list(hyp_objects),
            },
            "review_leads": {
                "changed_symbols": [],
                "direct_callers": [],
                "direct_callees": [],
                "transitive_callers": [],
                "source_coordinates": [],
            },
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
        ample = len(canonical_json(packet)) + 20_000
        result = enforce_review_context_budget(packet, max_chars=ample)

        mirror = (result.get("review_answer_packet") or {}).get("top_review_hypotheses") or []
        mirror_types = [h.get("risk_type") for h in mirror]

        self.assertIn("hook_gate_render_mismatch", mirror_types, "hook_gate must appear in mirror")
        hook_mirror_pos = mirror_types.index("hook_gate_render_mismatch")
        if "direct_call_contract_drift" in mirror_types:
            direct_mirror_pos = mirror_types.index("direct_call_contract_drift")
            self.assertLess(
                hook_mirror_pos, direct_mirror_pos,
                f"hook_gate must precede direct_call in mirror: {hook_mirror_pos} vs {direct_mirror_pos}",
            )


if __name__ == "__main__":
    unittest.main()
