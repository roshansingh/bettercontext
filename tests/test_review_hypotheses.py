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
    )
    ctx.update(overrides)
    return ctx


def _sym(name: str, path: str, kind: str = "function", lead_id: str | None = None) -> dict:
    """Build a production-shaped symbol row.

    Uses qualname (short) and display_name (module-qualified), matching what
    _symbol_result returns.  The synthetic module is derived from path for
    display_name so edge suffix matching works end-to-end.
    """
    stem = path.replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
    s = {
        "qualname": name,
        "display_name": f"{stem}.{name}",
        "qualified_name": f"{stem}.{name}",
        "kind": kind,
        "path": path,
    }
    if lead_id:
        s["lead_id"] = lead_id
    return s


def _edge(subject: str, object_: str, lead_id: str = "lead-edge") -> dict:
    """Build a production-shaped edge row.

    Wraps bare names in a synthetic module prefix to match _fact_result format
    ("{module}.{qualname}").  Tests that check subject/object by bare name still
    work because _edges_touch_names does suffix matching.
    """
    return {"subject": f"mod.{subject}", "object": f"mod.{object_}", "lead_id": lead_id}


class TestComponentListRenderIdentityDrift(unittest.TestCase):
    """Family: component_list_render_identity_drift"""

    def _call(self, **ctx_overrides):
        return review_hypotheses_for_context(**_base_context(**ctx_overrides))

    def test_positive_component_with_call_edge_emits_hypothesis(self):
        # Two component symbols in .tsx, one callee edge between them → trigger fires
        hypotheses = self._call(
            changed_files=["src/ItemList.tsx", "src/Row.tsx"],
            changed_symbols=[
                _sym("ItemList", "src/ItemList.tsx", kind="class", lead_id="lead-1"),
                _sym("Row", "src/Row.tsx", kind="class", lead_id="lead-2"),
            ],
            direct_callees=[_edge("ItemList", "Row", "lead-edge-1")],
            review_leads={
                "changed_symbols": [
                    {"lead_id": "lead-1", "path": "src/ItemList.tsx"},
                    {"lead_id": "lead-2", "path": "src/Row.tsx"},
                ],
                "direct_callees": [{"lead_id": "lead-edge-1", "path": "src/Row.tsx"}],
            },
        )
        risk_types = [h["risk_type"] for h in hypotheses]
        self.assertIn("component_list_render_identity_drift", risk_types)

    def test_positive_component_with_caller_edge_emits_hypothesis(self):
        # Caller edge (not callee) — still triggers
        hypotheses = self._call(
            changed_files=["ui/Card.tsx"],
            changed_symbols=[_sym("Card", "ui/Card.tsx", kind="class", lead_id="lead-3")],
            direct_callers=[_edge("Page", "Card", "lead-edge-2")],
            review_leads={
                "changed_symbols": [{"lead_id": "lead-3", "path": "ui/Card.tsx"}],
                "direct_callers": [{"lead_id": "lead-edge-2", "path": "ui/Page.tsx"}],
            },
        )
        risk_types = [h["risk_type"] for h in hypotheses]
        self.assertIn("component_list_render_identity_drift", risk_types)

    def test_positive_jsx_extension_triggers(self):
        # .jsx (not .tsx) also qualifies
        hypotheses = self._call(
            changed_files=["src/Button.jsx"],
            changed_symbols=[_sym("Button", "src/Button.jsx", kind="class", lead_id="lead-4")],
            direct_callees=[_edge("Button", "Icon", "lead-edge-3")],
            review_leads={
                "changed_symbols": [{"lead_id": "lead-4", "path": "src/Button.jsx"}],
                "direct_callees": [{"lead_id": "lead-edge-3", "path": "src/Icon.jsx"}],
            },
        )
        risk_types = [h["risk_type"] for h in hypotheses]
        self.assertIn("component_list_render_identity_drift", risk_types)

    def test_positive_medium_confidence_with_call_edges(self):
        hypotheses = self._call(
            changed_files=["src/List.tsx"],
            changed_symbols=[_sym("List", "src/List.tsx", kind="class", lead_id="lead-5")],
            direct_callees=[_edge("List", "Item", "lead-edge-4")],
            review_leads={
                "changed_symbols": [{"lead_id": "lead-5", "path": "src/List.tsx"}],
                "direct_callees": [{"lead_id": "lead-edge-4", "path": "src/Item.tsx"}],
            },
        )
        hyp = next(h for h in hypotheses if h["risk_type"] == "component_list_render_identity_drift")
        self.assertEqual(hyp["confidence"], "medium")

    def test_negative_edge_not_touching_changed_symbols_does_not_emit(self):
        # Component symbol changed, but the only edge connects two unrelated symbols
        hypotheses = self._call(
            changed_files=["src/Panel.tsx"],
            changed_symbols=[_sym("Panel", "src/Panel.tsx", kind="class", lead_id="lead-11")],
            direct_callees=[_edge("otherFn", "helperFn", "lead-edge-9")],
            review_leads={
                "changed_symbols": [{"lead_id": "lead-11", "path": "src/Panel.tsx"}],
                "direct_callees": [{"lead_id": "lead-edge-9", "path": "src/other.ts"}],
            },
        )
        risk_types = [h["risk_type"] for h in hypotheses]
        self.assertNotIn("component_list_render_identity_drift", risk_types)

    def test_negative_no_edges_does_not_emit(self):
        # Component symbol with no edges → no trigger
        hypotheses = self._call(
            changed_files=["src/Banner.tsx"],
            changed_symbols=[_sym("Banner", "src/Banner.tsx", kind="class", lead_id="lead-6")],
            direct_callers=[],
            direct_callees=[],
            review_leads={"changed_symbols": [{"lead_id": "lead-6", "path": "src/Banner.tsx"}]},
        )
        risk_types = [h["risk_type"] for h in hypotheses]
        self.assertNotIn("component_list_render_identity_drift", risk_types)

    def test_negative_python_only_does_not_emit(self):
        # Python symbols with edges → no component family
        hypotheses = self._call(
            changed_files=["payments/checkout.py"],
            changed_symbols=[_sym("checkout", "payments/checkout.py", lead_id="lead-7")],
            direct_callers=[_edge("order", "checkout", "lead-edge-5")],
            review_leads={
                "changed_symbols": [{"lead_id": "lead-7", "path": "payments/checkout.py"}],
                "direct_callers": [{"lead_id": "lead-edge-5", "path": "payments/order.py"}],
            },
        )
        risk_types = [h["risk_type"] for h in hypotheses]
        self.assertNotIn("component_list_render_identity_drift", risk_types)

    def test_negative_lowercase_tsx_symbol_does_not_emit(self):
        # Symbol in .tsx but name starts lowercase — not a component
        hypotheses = self._call(
            changed_files=["src/helpers.tsx"],
            changed_symbols=[_sym("formatDate", "src/helpers.tsx", lead_id="lead-8")],
            direct_callees=[_edge("formatDate", "parseISO", "lead-edge-6")],
            review_leads={
                "changed_symbols": [{"lead_id": "lead-8", "path": "src/helpers.tsx"}],
                "direct_callees": [{"lead_id": "lead-edge-6", "path": "src/utils.tsx"}],
            },
        )
        risk_types = [h["risk_type"] for h in hypotheses]
        self.assertNotIn("component_list_render_identity_drift", risk_types)

    def test_negative_non_frontend_extension_does_not_emit(self):
        # Uppercase symbol name but .py file — not a component
        hypotheses = self._call(
            changed_files=["models/User.py"],
            changed_symbols=[_sym("User", "models/User.py", kind="class", lead_id="lead-9")],
            direct_callees=[_edge("User", "Profile", "lead-edge-7")],
            review_leads={
                "changed_symbols": [{"lead_id": "lead-9", "path": "models/User.py"}],
                "direct_callees": [{"lead_id": "lead-edge-7", "path": "models/Profile.py"}],
            },
        )
        risk_types = [h["risk_type"] for h in hypotheses]
        self.assertNotIn("component_list_render_identity_drift", risk_types)

    def test_supporting_lead_ids_contain_only_component_touching_leads(self):
        # Component symbol lead-comp + non-component symbol lead-util; only edge touches component.
        # supporting_lead_ids must contain lead-comp and lead-edge-comp but NOT lead-util.
        hypotheses = self._call(
            changed_files=["src/Widget.tsx", "src/utils.tsx"],
            changed_symbols=[
                _sym("Widget", "src/Widget.tsx", kind="class", lead_id="lead-comp"),
                _sym("formatData", "src/utils.tsx", lead_id="lead-util"),
            ],
            direct_callees=[_edge("Widget", "Child", "lead-edge-comp")],
            review_leads={
                "changed_symbols": [
                    {"lead_id": "lead-comp", "path": "src/Widget.tsx"},
                    {"lead_id": "lead-util", "path": "src/utils.tsx"},
                ],
                "direct_callees": [{"lead_id": "lead-edge-comp", "subject": "mod.Widget", "object": "mod.Child"}],
            },
        )
        hyp = next(h for h in hypotheses if h["risk_type"] == "component_list_render_identity_drift")
        slids = set(hyp["supporting_lead_ids"])
        self.assertIn("lead-comp", slids)
        self.assertIn("lead-edge-comp", slids)
        self.assertNotIn("lead-util", slids)

    def test_negative_low_coverage_does_not_emit(self):
        # low_coverage gate must suppress new families
        hypotheses = self._call(
            changed_files=["src/Grid.tsx"],
            changed_symbols=[_sym("Grid", "src/Grid.tsx", kind="class", lead_id="lead-10")],
            direct_callees=[_edge("Grid", "Cell", "lead-edge-8")],
            review_leads={
                "changed_symbols": [{"lead_id": "lead-10", "path": "src/Grid.tsx"}],
                "direct_callees": [{"lead_id": "lead-edge-8"}],
            },
            review_lead_status={"coverage_status": "low_coverage"},
        )
        risk_types = [h["risk_type"] for h in hypotheses]
        self.assertNotIn("component_list_render_identity_drift", risk_types)

    def test_negative_edge_only_on_non_component_symbol_does_not_fire(self):
        # Changed .tsx component + changed non-component; only non-component has edges.
        # Family must NOT fire because no edge touches a component symbol.
        hypotheses = self._call(
            changed_files=["src/Table.tsx", "src/utils.tsx"],
            changed_symbols=[
                _sym("Table", "src/Table.tsx", kind="class", lead_id="lead-comp-1"),
                _sym("formatRow", "src/utils.tsx", lead_id="lead-util-1"),
            ],
            direct_callees=[_edge("formatRow", "helper", "lead-edge-util")],
            review_leads={
                "changed_symbols": [
                    {"lead_id": "lead-comp-1", "path": "src/Table.tsx"},
                    {"lead_id": "lead-util-1", "path": "src/utils.tsx"},
                ],
                "direct_callees": [{"lead_id": "lead-edge-util", "path": "src/helper.tsx"}],
            },
        )
        risk_types = [h["risk_type"] for h in hypotheses]
        self.assertNotIn("component_list_render_identity_drift", risk_types)


class TestHookGateRenderMismatch(unittest.TestCase):
    """Family: hook_gate_render_mismatch"""

    def _call(self, **ctx_overrides):
        return review_hypotheses_for_context(**_base_context(**ctx_overrides))

    def test_positive_hook_with_component_consumer_emits_hypothesis(self):
        # Hook symbol in .ts with a callee edge to a component → trigger fires
        hypotheses = self._call(
            changed_files=["src/useAuth.ts"],
            changed_symbols=[_sym("useAuth", "src/useAuth.ts", lead_id="lead-h1")],
            direct_callees=[_edge("useAuth", "Dashboard", "lead-he1")],
            review_leads={
                "changed_symbols": [{"lead_id": "lead-h1", "path": "src/useAuth.ts"}],
                "direct_callees": [{"lead_id": "lead-he1", "path": "src/Dashboard.tsx"}],
            },
        )
        risk_types = [h["risk_type"] for h in hypotheses]
        self.assertIn("hook_gate_render_mismatch", risk_types)

    def test_positive_hook_with_hook_consumer_emits_hypothesis(self):
        # Hook → hook edge also qualifies
        hypotheses = self._call(
            changed_files=["src/usePermissions.tsx"],
            changed_symbols=[_sym("usePermissions", "src/usePermissions.tsx", lead_id="lead-h2")],
            direct_callees=[_edge("usePermissions", "useFeatureFlag", "lead-he2")],
            review_leads={
                "changed_symbols": [{"lead_id": "lead-h2", "path": "src/usePermissions.tsx"}],
                "direct_callees": [{"lead_id": "lead-he2", "path": "src/useFeatureFlag.ts"}],
            },
        )
        risk_types = [h["risk_type"] for h in hypotheses]
        self.assertIn("hook_gate_render_mismatch", risk_types)

    def test_positive_hook_as_caller_target_emits_hypothesis(self):
        # Component is a direct_caller of the hook (hook is the callee/object)
        hypotheses = self._call(
            changed_files=["src/useSearch.ts"],
            changed_symbols=[_sym("useSearch", "src/useSearch.ts", lead_id="lead-h3")],
            direct_callers=[_edge("SearchPage", "useSearch", "lead-he3")],
            review_leads={
                "changed_symbols": [{"lead_id": "lead-h3", "path": "src/useSearch.ts"}],
                "direct_callers": [{"lead_id": "lead-he3", "path": "src/SearchPage.tsx"}],
            },
        )
        risk_types = [h["risk_type"] for h in hypotheses]
        self.assertIn("hook_gate_render_mismatch", risk_types)

    def test_positive_medium_confidence_with_consumer_edge(self):
        hypotheses = self._call(
            changed_files=["src/useData.js"],
            changed_symbols=[_sym("useData", "src/useData.js", lead_id="lead-h4")],
            direct_callers=[_edge("Widget", "useData", "lead-he4")],
            review_leads={
                "changed_symbols": [{"lead_id": "lead-h4", "path": "src/useData.js"}],
                "direct_callers": [{"lead_id": "lead-he4", "path": "src/Widget.jsx"}],
            },
        )
        hyp = next(h for h in hypotheses if h["risk_type"] == "hook_gate_render_mismatch")
        self.assertEqual(hyp["confidence"], "medium")

    def test_positive_weak_confidence_hook_no_consumer_edge(self):
        # Hook changed but no edge to component/hook → fires at weak confidence
        hypotheses = self._call(
            changed_files=["src/useCounter.ts"],
            changed_symbols=[_sym("useCounter", "src/useCounter.ts", lead_id="lead-h5")],
            direct_callers=[],
            direct_callees=[],
            review_leads={"changed_symbols": [{"lead_id": "lead-h5", "path": "src/useCounter.ts"}]},
        )
        risk_types = [h["risk_type"] for h in hypotheses]
        self.assertIn("hook_gate_render_mismatch", risk_types)
        hyp = next(h for h in hypotheses if h["risk_type"] == "hook_gate_render_mismatch")
        self.assertEqual(hyp["confidence"], "weak")

    def test_negative_python_hook_lookalike_does_not_emit(self):
        # Python function named use_auth — not a hook (no use-prefix convention in Python)
        hypotheses = self._call(
            changed_files=["auth/use_auth.py"],
            changed_symbols=[_sym("useAuth", "auth/use_auth.py", lead_id="lead-h6")],
            direct_callers=[_edge("View", "useAuth", "lead-he5")],
            review_leads={
                "changed_symbols": [{"lead_id": "lead-h6", "path": "auth/use_auth.py"}],
                "direct_callers": [{"lead_id": "lead-he5", "path": "auth/view.py"}],
            },
        )
        risk_types = [h["risk_type"] for h in hypotheses]
        self.assertNotIn("hook_gate_render_mismatch", risk_types)

    def test_negative_use_lowercase_third_char_does_not_emit(self):
        # "usecounter" — "use" prefix but 4th char lowercase — not a React hook
        hypotheses = self._call(
            changed_files=["src/usecounter.ts"],
            changed_symbols=[_sym("usecounter", "src/usecounter.ts", lead_id="lead-h7")],
            direct_callers=[_edge("Widget", "usecounter", "lead-he6")],
            review_leads={
                "changed_symbols": [{"lead_id": "lead-h7", "path": "src/usecounter.ts"}],
                "direct_callers": [{"lead_id": "lead-he6", "path": "src/Widget.tsx"}],
            },
        )
        risk_types = [h["risk_type"] for h in hypotheses]
        self.assertNotIn("hook_gate_render_mismatch", risk_types)

    def test_negative_non_frontend_extension_does_not_emit(self):
        # "useAuth" in a .py file → not a hook
        hypotheses = self._call(
            changed_files=["services/useAuth.py"],
            changed_symbols=[_sym("useAuth", "services/useAuth.py", lead_id="lead-h8")],
            direct_callers=[_edge("Page", "useAuth", "lead-he7")],
            review_leads={
                "changed_symbols": [{"lead_id": "lead-h8", "path": "services/useAuth.py"}],
                "direct_callers": [{"lead_id": "lead-he7", "path": "services/page.py"}],
            },
        )
        risk_types = [h["risk_type"] for h in hypotheses]
        self.assertNotIn("hook_gate_render_mismatch", risk_types)

    def test_negative_low_coverage_does_not_emit(self):
        hypotheses = self._call(
            changed_files=["src/useModal.ts"],
            changed_symbols=[_sym("useModal", "src/useModal.ts", lead_id="lead-h9")],
            direct_callers=[_edge("Dialog", "useModal", "lead-he8")],
            review_leads={
                "changed_symbols": [{"lead_id": "lead-h9", "path": "src/useModal.ts"}],
                "direct_callers": [{"lead_id": "lead-he8"}],
            },
            review_lead_status={"coverage_status": "low_coverage"},
        )
        risk_types = [h["risk_type"] for h in hypotheses]
        self.assertNotIn("hook_gate_render_mismatch", risk_types)


class TestTestLocksInRegression(unittest.TestCase):
    """Family: test_locks_in_regression"""

    def _call(self, **ctx_overrides):
        return review_hypotheses_for_context(**_base_context(**ctx_overrides))

    def test_positive_test_file_plus_code_symbol_plus_edge_emits_hypothesis(self):
        # Test file + non-test code symbol + direct edge → trigger fires
        hypotheses = self._call(
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
        risk_types = [h["risk_type"] for h in hypotheses]
        self.assertIn("test_locks_in_regression", risk_types)

    def test_positive_spec_path_segment_triggers(self):
        # "spec" test path segment
        hypotheses = self._call(
            changed_files=["lib/calc.js", "spec/calc_spec.js"],
            changed_symbols=[
                _sym("add", "lib/calc.js", lead_id="lead-t3"),
                _sym("testAdd", "spec/calc_spec.js", lead_id="lead-t4"),
            ],
            direct_callers=[_edge("testAdd", "add", "lead-te2")],
            review_leads={
                "changed_symbols": [
                    {"lead_id": "lead-t3", "path": "lib/calc.js"},
                    {"lead_id": "lead-t4", "path": "spec/calc_spec.js"},
                ],
                "direct_callers": [{"lead_id": "lead-te2"}],
            },
        )
        risk_types = [h["risk_type"] for h in hypotheses]
        self.assertIn("test_locks_in_regression", risk_types)

    def test_positive_weak_confidence(self):
        hypotheses = self._call(
            changed_files=["src/formatter.ts", "tests/formatter.test.ts"],
            changed_symbols=[
                _sym("format", "src/formatter.ts", lead_id="lead-t5"),
                _sym("testFormat", "tests/formatter.test.ts", lead_id="lead-t6"),
            ],
            direct_callees=[_edge("testFormat", "format", "lead-te3")],
            review_leads={
                "changed_symbols": [
                    {"lead_id": "lead-t5", "path": "src/formatter.ts"},
                    {"lead_id": "lead-t6", "path": "tests/formatter.test.ts"},
                ],
                "direct_callees": [{"lead_id": "lead-te3"}],
            },
        )
        hyp = next(h for h in hypotheses if h["risk_type"] == "test_locks_in_regression")
        self.assertEqual(hyp["confidence"], "weak")

    def test_negative_test_file_only_no_code_symbol_does_not_emit(self):
        # Only test files changed, no code symbols
        hypotheses = self._call(
            changed_files=["tests/test_utils.py"],
            changed_symbols=[_sym("test_something", "tests/test_utils.py", lead_id="lead-t7")],
            direct_callees=[],
            direct_callers=[],
            review_leads={"changed_symbols": [{"lead_id": "lead-t7", "path": "tests/test_utils.py"}]},
        )
        risk_types = [h["risk_type"] for h in hypotheses]
        self.assertNotIn("test_locks_in_regression", risk_types)

    def test_negative_code_only_no_test_file_does_not_emit(self):
        # Only code files changed, no test files
        hypotheses = self._call(
            changed_files=["src/service.py"],
            changed_symbols=[_sym("process", "src/service.py", lead_id="lead-t8")],
            direct_callees=[_edge("process", "helper", "lead-te4")],
            review_leads={
                "changed_symbols": [{"lead_id": "lead-t8", "path": "src/service.py"}],
                "direct_callees": [{"lead_id": "lead-te4"}],
            },
        )
        risk_types = [h["risk_type"] for h in hypotheses]
        self.assertNotIn("test_locks_in_regression", risk_types)

    def test_negative_test_and_code_but_no_edge_does_not_emit(self):
        # Test file + code symbol but no direct edge between changed symbols
        hypotheses = self._call(
            changed_files=["src/api.py", "tests/test_api.py"],
            changed_symbols=[
                _sym("handler", "src/api.py", lead_id="lead-t9"),
                _sym("test_handler", "tests/test_api.py", lead_id="lead-t10"),
            ],
            direct_callers=[],
            direct_callees=[],
            review_leads={
                "changed_symbols": [
                    {"lead_id": "lead-t9", "path": "src/api.py"},
                    {"lead_id": "lead-t10", "path": "tests/test_api.py"},
                ],
            },
        )
        risk_types = [h["risk_type"] for h in hypotheses]
        self.assertNotIn("test_locks_in_regression", risk_types)

    def test_negative_low_coverage_does_not_emit(self):
        hypotheses = self._call(
            changed_files=["src/validator.py", "tests/test_validator.py"],
            changed_symbols=[
                _sym("validate", "src/validator.py", lead_id="lead-t11"),
                _sym("test_validate", "tests/test_validator.py", lead_id="lead-t12"),
            ],
            direct_callees=[_edge("test_validate", "validate", "lead-te5")],
            review_leads={
                "changed_symbols": [
                    {"lead_id": "lead-t11", "path": "src/validator.py"},
                    {"lead_id": "lead-t12", "path": "tests/test_validator.py"},
                ],
                "direct_callees": [{"lead_id": "lead-te5"}],
            },
            review_lead_status={"coverage_status": "low_coverage"},
        )
        risk_types = [h["risk_type"] for h in hypotheses]
        self.assertNotIn("test_locks_in_regression", risk_types)

    def test_negative_edge_not_touching_changed_symbols_does_not_emit(self):
        # Test file + code symbol, but the only edge connects two unrelated symbols
        hypotheses = self._call(
            changed_files=["src/report.py", "tests/test_report.py"],
            changed_symbols=[
                _sym("build_report", "src/report.py", lead_id="lead-t14"),
                _sym("test_build_report", "tests/test_report.py", lead_id="lead-t15"),
            ],
            direct_callees=[_edge("unrelated_a", "unrelated_b", "lead-te7")],
            review_leads={
                "changed_symbols": [
                    {"lead_id": "lead-t14", "path": "src/report.py"},
                    {"lead_id": "lead-t15", "path": "tests/test_report.py"},
                ],
                "direct_callees": [{"lead_id": "lead-te7"}],
            },
        )
        risk_types = [h["risk_type"] for h in hypotheses]
        self.assertNotIn("test_locks_in_regression", risk_types)

    def test_negative_edge_only_on_test_symbol_does_not_fire(self):
        # Changed test file + changed code symbol; only the test symbol has edges.
        # Family must NOT fire because no edge touches a non-test code symbol.
        hypotheses = self._call(
            changed_files=["src/loader.py", "tests/test_loader.py"],
            changed_symbols=[
                _sym("load", "src/loader.py", lead_id="lead-code-1"),
                _sym("test_load", "tests/test_loader.py", lead_id="lead-test-1"),
            ],
            direct_callees=[_edge("test_load", "mock_helper", "lead-te-test-only")],
            review_leads={
                "changed_symbols": [
                    {"lead_id": "lead-code-1", "path": "src/loader.py"},
                    {"lead_id": "lead-test-1", "path": "tests/test_loader.py"},
                ],
                "direct_callees": [{"lead_id": "lead-te-test-only"}],
            },
        )
        risk_types = [h["risk_type"] for h in hypotheses]
        self.assertNotIn("test_locks_in_regression", risk_types)

    def test_negative_config_file_with_code_does_not_emit_test_locks(self):
        # test_or_config_masks_runtime_change may fire but NOT test_locks_in_regression
        hypotheses = self._call(
            changed_files=["src/app.py", "config.yaml"],
            changed_symbols=[_sym("run", "src/app.py", lead_id="lead-t13")],
            direct_callees=[_edge("run", "init", "lead-te6")],
            review_leads={
                "changed_symbols": [{"lead_id": "lead-t13", "path": "src/app.py"}],
                "direct_callees": [{"lead_id": "lead-te6"}],
            },
        )
        risk_types = [h["risk_type"] for h in hypotheses]
        self.assertNotIn("test_locks_in_regression", risk_types)


if __name__ == "__main__":
    unittest.main()
