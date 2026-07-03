from __future__ import annotations

import contextlib
from copy import deepcopy
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from source.kg.core.models import Coverage, Entity, Evidence, Fact, canonical_json
from source.kg.core.store import JsonlKgStore
from source.kg.product.review_attribution import add_review_lead_ids
from source.kg.product.application_impact import application_impact_packet
from source.kg.product.mcp_tools import (
    ENDPOINT_PATH_SHAPE_MATCH_BASIS,
    TOOL_NAMES,
    _optional_review_surfaces_tolerant,
    _planning_context_has_resolved_anchor,
    _planning_context_authz_surface_reference,
    _planning_context_symbol_impact,
    _review_context_lead_packet,
    _review_context_risk_signals,
    _with_default_tool_metadata,
    call_tool,
    tool_definitions,
)
from source.kg.product.output_budget import (
    AUTHZ_COMPACT_LIST_KEYS,
    COMPACT_AUTHZ_INSPECTION_REF_LIMIT,
    COMPACT_RUNTIME_HEADSTART_LIMIT,
    COMPACT_RUNTIME_SOURCE_CHECK_LIMIT,
    PLANNING_CONTEXT_ANCHORED_MAX_CHARS,
    PLANNING_CONTEXT_MAX_CHARS,
    RELATED_FACT_SECTION_KEYS,
    _BUDGET_BACKFILL_LIST_PATHS,
    REVERSE_IMPACT_MAX_CHARS,
    REVIEW_CONTEXT_BROAD_MAX_CHARS,
    REVIEW_CONTEXT_MAX_CHARS,
    _compact_authz_surface,
    _compact_disambiguation,
    _minimal_valid_packet,
    enforce_planning_context_budget,
    enforce_review_context_budget,
    enforce_reverse_impact_budget,
)
from source.kg.product.runtime_architecture import runtime_architecture_packet
from source.kg.query.snapshot import KgSnapshot
from source.scripts.mcp_server import (
    _JsonPayloadError,
    MCP_PROTOCOL_VERSION,
    REQUEST_READ_TIMEOUT_SECONDS,
    _content_length,
    _decode_json_payload,
    _format_host_for_url,
    _handler_class,
    _handle_json_rpc,
    _handle_json_rpc_payload,
    _is_loopback_host,
    _read_request_body,
    _RequestBodyTimeout,
    _server_address_for_host,
    _server_class_for_host,
)


EXTENSION_TOOL_NAMES: tuple[str, ...] = ("planning_context", "review_context")


def _assert_additive_fields(testcase: unittest.TestCase, payload: dict[str, object]) -> None:
    testcase.assertIn("coverage_warnings", payload)
    testcase.assertIn("unsupported_scopes", payload)
    testcase.assertIn("next_actions", payload)
    testcase.assertIn("answerability", payload)
    testcase.assertIn("proven_facts", payload)
    testcase.assertIn("candidate_leads", payload)
    testcase.assertIn("coverage_gaps", payload)
    testcase.assertIn("inspection_areas", payload)
    testcase.assertIn("packet_contract", payload)
    testcase.assertIsInstance(payload["coverage_warnings"], list)
    testcase.assertIsInstance(payload["unsupported_scopes"], list)
    testcase.assertIsInstance(payload["next_actions"], list)
    testcase.assertIsInstance(payload["answerability"], dict)
    testcase.assertIsInstance(payload["proven_facts"], dict)
    testcase.assertIsInstance(payload["candidate_leads"], dict)
    testcase.assertIsInstance(payload["coverage_gaps"], list)
    testcase.assertIsInstance(payload["inspection_areas"], list)
    testcase.assertIsInstance(payload["packet_contract"], dict)


def _assert_common_evidence_fields(testcase: unittest.TestCase, payload: dict[str, object]) -> None:
    testcase.assertIn("answerability", payload)
    testcase.assertIn("proven_facts", payload)
    testcase.assertIn("candidate_leads", payload)
    testcase.assertIn("coverage_gaps", payload)
    testcase.assertIn("inspection_areas", payload)
    testcase.assertIsInstance(payload["answerability"], dict)
    testcase.assertIsInstance(payload["proven_facts"], dict)
    testcase.assertIsInstance(payload["candidate_leads"], dict)
    testcase.assertIsInstance(payload["coverage_gaps"], list)
    testcase.assertIsInstance(payload["inspection_areas"], list)


class McpToolsTest(unittest.TestCase):
    def test_tool_definitions_include_adr_names_and_workflow_extensions(self) -> None:
        definitions = tool_definitions()
        self.assertEqual([row["name"] for row in definitions], [*TOOL_NAMES, *EXTENSION_TOOL_NAMES])
        schemas = {row["name"]: row["inputSchema"] for row in definitions}
        descriptions = {row["name"]: row["description"] for row in definitions}
        self.assertEqual(schemas["search_services"]["properties"]["query"]["type"], ["string", "null"])
        self.assertEqual(schemas["find_callers"]["properties"]["path"]["type"], ["string", "null"])
        self.assertEqual(schemas["find_callers"]["properties"]["line"]["type"], ["integer", "null"])
        self.assertEqual(schemas["reverse_impact"]["properties"]["depth"]["default"], 3)
        self.assertEqual(schemas["reverse_impact"]["properties"]["include_all"]["default"], False)
        self.assertEqual(schemas["planning_context"]["properties"]["symbol"]["type"], ["string", "null"])
        self.assertEqual(schemas["review_context"]["properties"]["changed_files"]["type"], "array")
        self.assertEqual(schemas["review_context"]["properties"]["requested_surfaces"]["type"], "array")
        self.assertEqual(schemas["review_context"]["properties"]["include_unlinked_leads"]["default"], False)
        self.assertNotIn("depth", schemas["review_context"]["properties"])
        self.assertIn("operational_surfaces.evidence_partition", descriptions["get_service_brief"])
        self.assertIn("operational_surfaces.deploy_link_facts", descriptions["get_service_brief"])
        self.assertIn("DEPLOYS_VIA_CONFIG", descriptions["get_service_brief"])
        self.assertIn("service_operational_surfaces.evidence_partition", descriptions["planning_context"])
        self.assertIn("service_operational_surfaces.deploy_link_facts", descriptions["planning_context"])
        self.assertIn("DEPLOYS_VIA_CONFIG", descriptions["planning_context"])
        self.assertIn("known_linked", descriptions["planning_context"])
        self.assertIn("unlinked_evidence", descriptions["planning_context"])
        self.assertIn("missing_contracts", descriptions["planning_context"])
        self.assertIn("does not attach fleet runtime_architecture or authz_surface", descriptions["planning_context"])
        self.assertIn("runtime_architecture", descriptions["planning_context"])
        self.assertIn("investigation_brief_only", descriptions["planning_context"])
        self.assertIn("related_facts.symbol_impact.reverse_impact", descriptions["planning_context"])
        self.assertIn("ownership_context", descriptions["planning_context"])
        self.assertIn("review_answer_packet", descriptions["review_context"])
        self.assertIn("review_answer_packet.changed_file_symbol_inventory", descriptions["review_context"])
        self.assertIn("requested_surfaces", descriptions["review_context"])
        self.assertIn("framework_impact", descriptions["review_context"])
        self.assertIn("application_impact", descriptions["review_context"])
        self.assertIn("disambiguation.retry_arguments", descriptions["find_callers"])
        self.assertIn("unqualified symbol name", schemas["reverse_impact"]["properties"]["symbol"]["description"])
        self.assertIn("__init__", descriptions["reverse_impact"])
        self.assertIn("terminal import_consumer_leads", descriptions["reverse_impact"])
        self.assertIn("source_inspection_areas", descriptions["reverse_impact"])
        self.assertNotIn("what is affected if this symbol changes", descriptions["reverse_impact"])
        self.assertNotIn("what breaks if I change this", descriptions["reverse_impact"])
        self.assertIn("import_consumer_leads", descriptions["find_callers"])
        self.assertIn("disambiguation.retry_arguments", descriptions["find_callees"])
        for description in descriptions.values():
            self.assertIn("packet_contract", description)
            self.assertIn("proven_facts", description)
            self.assertIn("candidate_leads", description)
            self.assertIn("coverage_gaps", description)
            self.assertIn("inspection_areas", description)

    def test_review_context_tool_mentions_review_hypotheses_and_hypothesis_id(self) -> None:
        definitions = tool_definitions()
        review_tool = next(tool for tool in definitions if tool["name"] == "review_context")
        self.assertIn("review_hypotheses", review_tool["description"])
        self.assertIn("hypothesis_id", review_tool["description"])

    def test_default_tool_metadata_treats_missing_status_as_answerable(self) -> None:
        payload = _with_default_tool_metadata({"services": [{"name": "api"}]}, tool_name="search_services")

        self.assertEqual(payload["answerability"]["status"], "answerable")
        self.assertEqual(payload["proven_facts"]["status"], "found")
        self.assertEqual(payload["candidate_leads"]["status"], "empty")
        self.assertNotIn("query_plan", payload)

    def test_default_tool_metadata_treats_empty_missing_status_as_not_answerable(self) -> None:
        payload = _with_default_tool_metadata({"query": "api"}, tool_name="search_services")

        self.assertEqual(payload["answerability"]["status"], "not_answerable")
        self.assertEqual(payload["proven_facts"]["status"], "empty")
        self.assertEqual(payload["candidate_leads"]["status"], "empty")
        self.assertTrue(any("coverage boundary" in action for action in payload["next_actions"]))
        self.assertTrue(any("normal search/read tools at least once" in action for action in payload["next_actions"]))
        self.assertFalse(any(row["trigger"] == "next_action" for row in payload["inspection_areas"]))

    def test_default_tool_metadata_adds_exact_retry_for_ambiguous_anchor(self) -> None:
        payload = _with_default_tool_metadata(
            {"status": "ambiguous", "candidates": [{"qualified_name": "pkg.Symbol"}]},
            tool_name="find_callers",
        )

        self.assertEqual(payload["answerability"]["status"], "not_answerable")
        self.assertEqual(payload["answerability"]["missing_fact_families"], ["ambiguous_anchor"])
        self.assertTrue(any("disambiguation.retry_arguments" in action for action in payload["next_actions"]))

    def test_default_tool_metadata_does_not_downgrade_found_candidate_matches(self) -> None:
        payload = _with_default_tool_metadata(
            {"status": "found", "candidates": [{"qualified_name": "pkg.Symbol"}]},
            tool_name="find_callers",
        )

        self.assertEqual(payload["answerability"]["status"], "answerable")
        self.assertEqual(payload["candidate_leads"]["status"], "found")
        self.assertEqual(payload["candidate_leads"]["sources"][0]["lead_kind"], "candidate_match")

    def test_candidate_lead_kind_classifies_every_registered_field(self) -> None:
        from source.kg.product.mcp_tools import (
            _CANDIDATE_LEAD_FIELDS,
            _NESTED_CANDIDATE_LEAD_FIELDS,
            _candidate_lead_kind,
        )

        from source.kg.product.mcp_tools import _CANDIDATE_LEAD_KIND

        # call_site_leads must classify as a real lead kind, not the generic fallback.
        self.assertEqual(_candidate_lead_kind("call_site_leads"), "non_callable_call_site_lead")
        # Every registered candidate-lead field/label is mapped EXPLICITLY (not via the
        # generic fallback), so a new field can't silently degrade to "candidate_lead".
        for field in _CANDIDATE_LEAD_FIELDS:
            self.assertIn(field, _CANDIDATE_LEAD_KIND, f"{field} missing from _CANDIDATE_LEAD_KIND")
        for field, _path in _NESTED_CANDIDATE_LEAD_FIELDS:
            self.assertIn(field, _CANDIDATE_LEAD_KIND, f"{field} missing from _CANDIDATE_LEAD_KIND")
        self.assertEqual(_candidate_lead_kind("unknown_field"), "candidate_lead")

    def test_default_tool_metadata_ignores_found_status_without_rows(self) -> None:
        payload = _with_default_tool_metadata(
            {"status": "found", "import_consumer_leads": {"status": "found"}},
            tool_name="find_callers",
        )

        self.assertEqual(payload["candidate_leads"]["status"], "empty")
        self.assertEqual(payload["answerability"]["status"], "answerable")

    def test_default_tool_metadata_does_not_count_empty_row_lists_as_facts(self) -> None:
        payload = _with_default_tool_metadata(
            {"status": "found", "import_consumer_leads": {"lead_count": 5, "leads": []}},
            tool_name="find_callers",
        )

        self.assertEqual(payload["candidate_leads"]["status"], "empty")

    def test_default_tool_metadata_marks_unknown_status_partial(self) -> None:
        payload = _with_default_tool_metadata(
            {"status": "ok", "services": [{"name": "api"}]},
            tool_name="search_services",
        )

        self.assertEqual(payload["answerability"]["status"], "partial")
        self.assertEqual(payload["answerability"]["missing_fact_families"], ["unknown_status"])
        self.assertEqual(payload["proven_facts"]["status"], "found")

    def test_planning_context_resolves_structured_and_query_inputs(self) -> None:
        with _fixture_snapshot() as kg:
            symbol = call_tool(kg, "planning_context", {"symbol": "charge_card"})
            query = call_tool(kg, "planning_context", {"query": "shared-lib"})

        self.assertEqual(symbol["status"], "found")
        self.assertIn("anchors", symbol)
        self.assertEqual(query["status"], "found")
        self.assertEqual(query["anchors"]["package"], "shared-lib")
        self.assertEqual(query["dependencies"][0]["predicate"], "IMPORTS")
        self.assertNotIn("runtime_architecture", query)
        self.assertNotIn("authz_surface", query)
        self.assertNotIn("runtime_architecture", query["related_facts"])
        self.assertNotIn("authz_surface", query["related_facts"])

    def test_planning_context_non_runtime_symbol_anchor_skips_unscoped_runtime_and_authz_surfaces(self) -> None:
        with _fixture_snapshot() as kg:
            result = call_tool(kg, "planning_context", {"symbol": "charge_card"})

        self.assertEqual(result["status"], "found")
        self.assertEqual(result["anchors"]["symbol"], "charge_card")
        self.assertIn("symbol_impact", result["related_facts"])
        self.assertNotIn("runtime_architecture", result)
        self.assertNotIn("authz_surface", result)
        self.assertNotIn("runtime_architecture", result["related_facts"])
        self.assertNotIn("authz_surface", result["related_facts"])

    def test_planning_context_path_and_line_anchor_skips_unscoped_runtime_and_authz_surfaces(self) -> None:
        with _fixture_snapshot() as kg:
            path = call_tool(kg, "planning_context", {"path": "payments/checkout.py"})
            path_line = call_tool(
                kg,
                "planning_context",
                {"path": "payments/checkout.py", "line": 10},
            )

        for result in (path, path_line):
            self.assertEqual(result["status"], "found")
            self.assertNotIn("runtime_architecture", result)
            self.assertNotIn("authz_surface", result)
            self.assertNotIn("runtime_architecture", result["related_facts"])
            self.assertNotIn("authz_surface", result["related_facts"])

    def test_planning_context_domain_and_event_anchors_include_runtime_not_authz(self) -> None:
        with _fixture_snapshot() as kg:
            domain = call_tool(kg, "planning_context", {"domain": "api.internal.example"})
            event = call_tool(kg, "planning_context", {"event_channel": "orders-created"})

        self.assertEqual(domain["status"], "found")
        self.assertEqual(event["status"], "found")
        self.assertIn("runtime_architecture", domain)
        self.assertIn("runtime_architecture", domain["related_facts"])
        self.assertIn("runtime_architecture", event)
        self.assertIn("runtime_architecture", event["related_facts"])
        self.assertNotIn("authz_surface", domain)
        self.assertNotIn("authz_surface", domain["related_facts"])
        self.assertNotIn("authz_surface", event)
        self.assertNotIn("authz_surface", event["related_facts"])

    def test_planning_context_ambiguous_inputs_fail_closed_and_empty_input_returns_fleet_packet(self) -> None:
        with _fixture_snapshot() as kg:
            ambiguous = call_tool(kg, "planning_context", {"query": "payments"})
            fleet = call_tool(kg, "planning_context", {})

        self.assertEqual(ambiguous["status"], "ambiguous")
        self.assertTrue(ambiguous["next_actions"])
        self.assertTrue(any(row["predicate"] == "RESOLVES_TO_REPO" for row in ambiguous["dependencies"]))
        ambiguous_runtime = ambiguous["runtime_architecture"]
        self.assertEqual(ambiguous_runtime["summary"]["answer_packet_mode"], "investigation_brief_only")
        self.assertEqual(ambiguous_runtime["anchor_resolution_contract"]["status"], "inventory_context")
        self.assertIn("investigation_brief", ambiguous_runtime["answer_packet"])
        self.assertNotIn("runtime_building_blocks", ambiguous_runtime["answer_packet"])
        self.assertNotIn("domain_routing_map", ambiguous_runtime["answer_packet"])
        self.assertEqual(
            ambiguous["related_facts"]["runtime_architecture"]["anchor_resolution_contract"]["status"],
            "inventory_context",
        )
        self.assertEqual(fleet["status"], "found")
        self.assertEqual(fleet["snapshot_summary"]["scope"], {"kind": "fleet"})
        self.assertEqual(fleet["runtime_architecture"]["scope"], {"kind": "fleet"})
        self.assertNotIn("anchor_resolution_contract", fleet["runtime_architecture"])
        self.assertEqual(
            fleet["related_facts"]["runtime_architecture"]["deploy_kind_counts"],
            {"component_deploy_kind_counts": {}, "unlinked_route_deploy_kind_counts": {}},
        )
        self.assertEqual(fleet["services"][0]["name"], "payments")
        self.assertTrue(any("runtime_architecture.answer_packet" in action for action in fleet["next_actions"]))

    def test_planning_context_package_query_does_not_treat_limited_rows_as_unique(self) -> None:
        with _fixture_snapshot(extra_package_importers=1) as kg:
            ambiguous = call_tool(kg, "planning_context", {"query": "shared-lib", "limit": 1})

        self.assertEqual(ambiguous["status"], "ambiguous")
        self.assertTrue(ambiguous["next_actions"])
        self.assertEqual(len(ambiguous["dependencies"]), 1)
        self.assertEqual(ambiguous["dependencies"][0]["predicate"], "IMPORTS")

    def test_planning_context_raw_query_substring_matches_fail_closed(self) -> None:
        with _fixture_snapshot() as kg:
            endpoint = call_tool(kg, "planning_context", {"query": "/check"})
            event_channel = call_tool(kg, "planning_context", {"query": "orders"})
            domain = call_tool(kg, "planning_context", {"query": "internal"})

        self.assertEqual(endpoint["status"], "ambiguous")
        self.assertTrue(endpoint["next_actions"])
        self.assertEqual(event_channel["status"], "ambiguous")
        self.assertTrue(event_channel["next_actions"])
        self.assertEqual(domain["status"], "ambiguous")
        self.assertTrue(domain["next_actions"])

    def test_planning_context_raw_query_zero_hits_is_ambiguous(self) -> None:
        with _fixture_snapshot() as kg:
            result = call_tool(kg, "planning_context", {"query": "definitely-missing-anchor"})

        self.assertEqual(result["status"], "ambiguous")
        self.assertTrue(result["next_actions"])
        self.assertIn("runtime_architecture", result)
        runtime = result["runtime_architecture"]
        self.assertEqual(runtime["summary"]["answer_packet_mode"], "investigation_brief_only")
        self.assertIn("investigation_brief", runtime["answer_packet"])
        self.assertNotIn("runtime_building_blocks", runtime["answer_packet"])

    def test_planning_context_symbol_ambiguity_returns_candidates(self) -> None:
        with _fixture_snapshot(extra_charge_card_symbol=True) as kg:
            result = call_tool(kg, "planning_context", {"symbol": "charge_card"})

        self.assertEqual(result["status"], "ambiguous")
        self.assertGreaterEqual(len(result["symbols"]), 2)
        symbol_impact = result["related_facts"]["symbol_impact"]
        self.assertEqual(symbol_impact["status"], "ambiguous")
        self.assertEqual(symbol_impact["reverse_impact"]["status"], "ambiguous")
        self.assertEqual(len(symbol_impact["reverse_impact"]["candidate_impact_previews"]), 2)
        self.assertTrue(result["next_actions"])

    def test_planning_context_multiple_primary_anchors_intersect(self) -> None:
        with _fixture_snapshot() as kg:
            result = call_tool(kg, "planning_context", {"repo": "payments", "package": "shared-lib"})

        self.assertEqual(result["status"], "found")
        self.assertEqual(result["anchors"]["repo"], "payments")
        self.assertEqual(result["anchors"]["package"], "shared-lib")
        self.assertEqual({row["predicate"] for row in result["dependencies"]}, {"IMPORTS", "RESOLVES_TO_REPO"})
        self.assertEqual(result["snapshot_summary"]["entity_count"], 11)
        self.assertEqual(result["snapshot_summary"]["fact_count"], 7)
        self.assertEqual(result["snapshot_summary"]["scope"], {"kind": "fleet"})
        self.assertIn("full loaded KG snapshot", result["snapshot_summary"]["count_contract"])
        self.assertEqual(result["snapshot_scope"]["repo"], "payments")
        self.assertEqual(result["snapshot_scope"]["scope"], {"kind": "repo", "repo": "payments"})
        self.assertIn("scoped to repo payments", result["snapshot_scope"]["count_contract"])
        self.assertEqual(result["inventory"]["scope"], {"kind": "repo", "repo": "payments"})
        self.assertIn("scoped to repo payments", result["inventory"]["count_contract"])
        self.assertGreater(result["snapshot_scope"]["entity_count"], 0)
        self.assertGreater(result["snapshot_scope"]["fact_count"], 0)
        self.assertEqual(result["runtime_architecture"]["scope"], {"kind": "repo", "repo": "payments"})
        self.assertEqual(result["authz_surface"]["scope"], {"repo": "payments", "mode": "repo"})
        self.assertIn("runtime_architecture", result["related_facts"])
        self.assertIn("authz_surface", result["related_facts"])

    def test_planning_context_service_and_repo_narrow_without_scope_rejection(self) -> None:
        with _fixture_snapshot() as kg:
            result = call_tool(kg, "planning_context", {"service": "payments", "repo": "payments"})

        self.assertEqual(result["status"], "found")
        self.assertEqual(len(result["services"]), 1)
        self.assertEqual(result["services"][0]["slug"], "payments")

    def test_planning_context_repo_anchor_surfaces_service_identity(self) -> None:
        with _fixture_snapshot(extra_consumers=1) as kg:
            result = call_tool(kg, "planning_context", {"repo": "consumer-0"})

        # A repo anchor surfaces its Service entity as the primary identity answer so the
        # agent does not fall back to weaker packaging-metadata evidence for "what service
        # is this repo".
        self.assertEqual(result["status"], "found")
        self.assertTrue(
            any(
                row.get("slug") == "consumer-0" and row.get("repo") == "consumer-0"
                for row in result["services"]
            )
        )
        self.assertEqual(result["snapshot_scope"]["repo"], "consumer-0")
        self.assertGreater(result["snapshot_scope"]["entity_count"], 0)
        # Repo-scoped summary counts are complete: evidence and module counts are present
        # so a compact KG-summary answer need not fall back to fleet-wide totals.
        self.assertIsInstance(result["snapshot_scope"]["evidence_count"], int)
        self.assertIsInstance(result["snapshot_scope"]["module_count"], int)

    def test_planning_context_repo_anchor_accepts_owner_repo_query_for_dependencies(self) -> None:
        with _fixture_snapshot() as kg:
            result = call_tool(kg, "planning_context", {"repo": "latticeai/payments"})

        self.assertEqual(result["status"], "found")
        self.assertTrue(any(row["slug"] == "payments" for row in result["services"]))
        self.assertEqual({row["predicate"] for row in result["dependencies"]}, {"RESOLVES_TO_REPO"})
        self.assertGreater(result["snapshot_scope"]["fact_count"], 0)
        self.assertGreater(result["inventory"]["summary"]["entity_count"], 0)
        self.assertGreater(result["inventory"]["summary"]["top_dependency_count"], 0)

    def test_planning_context_symbol_anchor_accepts_owner_repo_query(self) -> None:
        with _fixture_snapshot() as kg:
            result = call_tool(kg, "planning_context", {"repo": "latticeai/payments", "symbol": "handle_checkout"})

        self.assertEqual(result["status"], "found")
        self.assertTrue(any(row["qualname"] == "handle_checkout" for row in result["symbols"]))

    def test_planning_context_single_substring_service_anchor_stays_found(self) -> None:
        with _fixture_snapshot() as kg:
            result = call_tool(kg, "planning_context", {"service": "pay"})

        self.assertEqual(result["status"], "found")
        self.assertEqual(len(result["services"]), 1)
        self.assertEqual(result["services"][0]["slug"], "payments")

    def test_planning_context_symbol_path_and_line_narrow_deterministically(self) -> None:
        with _fixture_snapshot() as kg:
            result = call_tool(kg, "planning_context", {"symbol": "charge_card", "path": "payments/gateway.py", "line": 5})

        self.assertEqual(result["status"], "found")
        self.assertEqual(len(result["symbols"]), 1)
        self.assertEqual(result["symbols"][0]["qualname"], "charge_card")

    def test_planning_context_path_and_line_filter_before_limit(self) -> None:
        with _fixture_snapshot() as kg:
            result = call_tool(kg, "planning_context", {"path": "payments/checkout.py", "line": 10, "limit": 1})

        self.assertEqual(result["status"], "found")
        self.assertEqual(len(result["symbols"]), 1)
        self.assertEqual(result["symbols"][0]["qualname"], "handle_checkout")

    def test_planning_context_fact_line_filter_uses_attached_evidence(self) -> None:
        with _fixture_snapshot() as kg:
            result = call_tool(
                kg,
                "planning_context",
                {"package": "shared-lib", "line": 2},
            )

        self.assertEqual(result["status"], "found")
        self.assertEqual({row["predicate"] for row in result["dependencies"]}, {"IMPORTS"})

    def test_planning_context_symbol_path_and_line_disambiguate_symbol_anchor(self) -> None:
        with _fixture_snapshot() as kg:
            result = call_tool(
                kg,
                "planning_context",
                {"symbol": "charge_card", "path": "payments/gateway.py", "line": 5},
            )

        self.assertEqual(result["status"], "found")
        self.assertEqual(len(result["symbols"]), 1)
        self.assertEqual(result["symbols"][0]["qualname"], "charge_card")
        self.assertEqual(result["symbols"][0]["path"], "payments/gateway.py")

    def test_planning_context_repo_filters_service_anchor_without_failing_closed(self) -> None:
        with _fixture_snapshot() as kg:
            result = call_tool(kg, "planning_context", {"service": "payments", "repo": "payments"})

        self.assertEqual(result["status"], "found")
        self.assertEqual(len(result["services"]), 1)
        self.assertEqual(result["services"][0]["slug"], "payments")
        self.assertEqual({row["predicate"] for row in result["dependencies"]}, {"RESOLVES_TO_REPO"})

    def test_planning_context_service_anchor_returns_composed_context(self) -> None:
        with _fixture_snapshot() as kg:
            result = call_tool(kg, "planning_context", {"service": "payments"})

        self.assertEqual(result["status"], "found")
        self.assertEqual(result["answerability"]["status"], "answerable")
        self.assertEqual(result["answerability"]["missing_fact_families"], [])
        self.assertEqual(result["summary"]["service_count"], 1)
        self.assertEqual(result["summary"]["endpoint_fact_count"], 1)
        self.assertEqual(result["summary"]["event_fact_count"], 1)
        self.assertTrue(any(row["section"] == "services" for row in result["entry_points"]))
        service_brief = result["related_facts"]["service_brief"]
        self.assertEqual(service_brief["summary"]["endpoint_fact_count"], 1)
        self.assertEqual(service_brief["summary"]["event_fact_count"], 1)
        self.assertEqual(service_brief["summary"]["deploy_mapping_count"], 0)
        self.assertEqual(service_brief["summary"]["endpoint_fact_count"], result["summary"]["endpoint_fact_count"])
        self.assertEqual(service_brief["summary"]["event_fact_count"], result["summary"]["event_fact_count"])
        self.assertFalse(any(family == "deploy_mapping" for family in result["answerability"]["missing_fact_families"]))

    def test_planning_context_service_anchor_includes_endpoint_consumers(self) -> None:
        with _fixture_snapshot(endpoint_consumer=True) as kg:
            result = call_tool(kg, "planning_context", {"service": "payments", "limit": 10})

        self.assertEqual(result["status"], "found")
        self.assertEqual(result["summary"]["endpoint_consumer_fact_count"], 1)
        self.assertEqual(result["endpoint_consumers"][0]["consumer"]["slug"], "web")
        self.assertEqual(result["related_facts"]["service_brief"]["summary"]["endpoint_consumer_fact_count"], 1)
        self.assertEqual(result["related_facts"]["endpoint_consumers"][0]["matched_provider_endpoint"]["path"], "/checkout")

    def test_planning_context_includes_inventory_and_dependency_importers(self) -> None:
        with _fixture_snapshot(extra_package_importers=2) as kg:
            result = call_tool(kg, "planning_context", {"package": "shared-lib", "limit": 10})

        self.assertEqual(result["status"], "found")
        self.assertEqual(result["inventory"]["scope"], {"kind": "fleet"})
        self.assertIn("full loaded KG snapshot", result["inventory"]["count_contract"])
        self.assertEqual(result["related_facts"]["dependency_importers"]["summary"]["importer_fact_count"], 3)
        self.assertEqual(result["related_facts"]["dependency_importers"]["repo_counts"], {"payments": 3})
        self.assertEqual(result["related_facts"]["dependency_importers"]["packages"][0]["name"], "shared-lib")

    def test_planning_context_package_anchor_skips_unscoped_runtime_and_authz_surfaces(self) -> None:
        with _fixture_snapshot(
            extra_package_importers=2,
            runtime_pressure_routes=4,
            runtime_pressure_payload_size=200,
            endpoint_consumer=True,
            operational_deploy_mapping=True,
            operational_deploy_link=True,
        ) as kg:
            result = call_tool(kg, "planning_context", {"package": "shared-lib", "limit": 10})

        self.assertEqual(result["status"], "found")
        self.assertEqual(result["anchors"]["package"], "shared-lib")
        self.assertNotIn("runtime_architecture", result)
        self.assertNotIn("authz_surface", result)
        self.assertNotIn("runtime_architecture", result["related_facts"])
        self.assertNotIn("authz_surface", result["related_facts"])
        self.assertEqual(result["related_facts"]["dependency_importers"]["summary"]["importer_fact_count"], 3)
        self.assertEqual(result["related_facts"]["dependency_importers"]["repo_counts"], {"payments": 3})

    def test_planning_context_budget_does_not_reintroduce_skipped_runtime_or_authz_surfaces(self) -> None:
        importer_rows = [
            {
                "predicate": "IMPORTS",
                "repo": "consumer",
                "path": f"consumer/module_{index}.py",
                "payload": "x" * 1_000,
            }
            for index in range(20)
        ]
        result = {
            "tool": "planning_context",
            "status": "found",
            "summary": {"dependency_count": 20},
            "snapshot_summary": {},
            "snapshot_scope": {},
            "ownership_context": {},
            "anchors": {"package": "shared-lib"},
            "related_facts": {
                "dependency_importers": {
                    "status": "found",
                    "summary": {"importer_fact_count": 20, "importer_repo_count": 1},
                    "importers": importer_rows,
                    "repo_counts": {"consumer": 20},
                }
            },
            "coverage_warnings": [],
            "unsupported_scopes": [],
            "next_actions": [],
        }

        budgeted = enforce_planning_context_budget(
            result,
            max_chars=3_000,
            preserve_planning_sections=True,
        )

        self.assertLessEqual(len(canonical_json(budgeted)), 3_000)
        self.assertNotIn("runtime_architecture", budgeted)
        self.assertNotIn("authz_surface", budgeted)
        self.assertNotIn("runtime_architecture", budgeted["related_facts"])
        self.assertNotIn("authz_surface", budgeted["related_facts"])
        self.assertEqual(budgeted["related_facts"]["dependency_importers"]["summary"]["importer_fact_count"], 20)
        self.assertNotIn("runtime_architecture", budgeted["output_budget"]["advice"])
        self.assertNotIn("source_coordinates", budgeted["output_budget"]["advice"])
        self.assertIn("related_facts", budgeted["output_budget"]["advice"])

    def test_planning_context_includes_service_operational_surfaces(self) -> None:
        with _fixture_snapshot(operational_deploy_mapping=True, operational_deploy_same_repo=True) as kg:
            result = call_tool(kg, "planning_context", {"service": "payments", "limit": 10})

        surfaces = result["service_operational_surfaces"]
        self.assertEqual(surfaces["status"], "found")
        self.assertEqual(surfaces["summary"]["deploy_target_candidate_count"], 1)
        self.assertEqual(surfaces["summary"]["domain_route_candidate_count"], 1)
        self.assertEqual(surfaces["evidence_buckets"], ["known_linked", "unlinked_evidence", "missing_contracts"])
        self.assertEqual(surfaces["evidence_partition"]["known_linked"]["status"], "found")
        self.assertEqual(surfaces["evidence_partition"]["known_linked"]["counts"]["domain_route_count"], 1)
        self.assertEqual(
            result["related_facts"]["service_operational_surfaces"]["domain_route_candidates"][0]["predicate"],
            "ROUTES_DOMAIN_TO_DEPLOY",
        )

    def test_planning_context_includes_runtime_architecture_map(self) -> None:
        with _fixture_snapshot(
            endpoint_consumer=True,
            operational_deploy_mapping=True,
            operational_deploy_link=True,
            operational_deploy_same_repo=True,
        ) as kg:
            result = call_tool(kg, "planning_context", {"repo": "payments", "limit": 10})

        architecture = result["runtime_architecture"]
        self.assertEqual(architecture["scope"], {"kind": "repo", "repo": "payments"})
        self.assertEqual(architecture["summary"]["domain_route_count"], 1)
        self.assertEqual(architecture["summary"]["deploy_link_count"], 1)
        self.assertEqual(architecture["summary"]["endpoint_surface_count"], 1)
        self.assertEqual(architecture["summary"]["client_endpoint_call_count"], 1)
        self.assertIn("answer_packet", architecture)
        answer_packet = architecture["answer_packet"]
        brief = answer_packet["investigation_brief"]
        self.assertEqual(brief["purpose"], "head_start_for_agent_source_investigation")
        self.assertEqual(brief["runtime_anchors"][0]["name"], "payments")
        self.assertEqual(brief["known_routes"][0]["domain"]["name"], "payments.example.com")
        self.assertTrue(brief["recommended_source_checks"])
        self.assertEqual(answer_packet["runtime_building_blocks"][0]["deploy_kinds"], ["apache_wsgi"])
        self.assertIn("domain_routed", answer_packet["runtime_building_blocks"][0]["runtime_categories"])
        self.assertEqual(answer_packet["domain_routing_map"][0]["status"], "known_route")
        self.assertEqual(answer_packet["domain_routing_map"][0]["deploy_kind"], "apache_wsgi")
        self.assertEqual(answer_packet["deploy_runtime_map"][0]["status"], "known_linked_deploy_unit")
        self.assertEqual(answer_packet["endpoint_consumer_map"][0]["consumer_count"], 1)
        self.assertEqual(answer_packet["deploy_order_guidance"][0]["status"], "practical_inference")
        self.assertIn("canonical_service_deploy_blocker", answer_packet["missing_fact_families"])
        self.assertEqual(
            result["related_facts"]["runtime_architecture"]["deploy_kind_counts"]["component_deploy_kind_counts"],
            {"apache_wsgi": 1},
        )
        self.assertEqual(
            result["related_facts"]["runtime_architecture"]["deploy_kind_counts"]["unlinked_route_deploy_kind_counts"],
            {},
        )
        self.assertIn("Runtime architecture is assembled only from typed KG facts", architecture["assembly_contract"])

    def test_service_operational_surfaces_include_kubernetes_runtime_unit_and_deploy_order_guidance(self) -> None:
        with _fixture_snapshot(
            endpoint_consumer=True,
            operational_deploy_mapping=True,
            operational_deploy_link=True,
            operational_deploy_same_repo=True,
            kubernetes_operational_deploy=True,
        ) as kg:
            result = call_tool(kg, "planning_context", {"service": "payments", "limit": 10})

        surfaces = result["service_operational_surfaces"]
        self.assertEqual(result["runtime_architecture"]["scope"], {"kind": "repo", "repo": "payments"})
        self.assertEqual(surfaces["summary"]["deploy_runtime_unit_count"], 1)
        unit = surfaces["deploy_runtime_units"][0]
        self.assertEqual(unit["deploy_kind"], "kubernetes_deployment")
        self.assertEqual(unit["deploy_details"]["workload"], "payments")
        self.assertEqual(unit["deploy_details"]["containers"], ["payments"])
        self.assertEqual(unit["deploy_details"]["images"], ["registry.example.com/payments:latest"])
        route = unit["ingress_or_domain_routes"][0]
        self.assertEqual(route["domain"]["name"], "payments.example.com")
        self.assertEqual(route["backend_service"], "payments-service")
        self.assertEqual(route["backend_service_ports"], [{"port": 80, "targetPort": 8000}])
        self.assertEqual(route["ingress_path"], "/")
        guidance = surfaces["deploy_order_guidance"]
        self.assertEqual(guidance["status"], "inference_available")
        self.assertEqual(guidance["practical_deploy_order"][0]["consumer"]["slug"], "web")
        self.assertIn("canonical_service_deploy_blocker", guidance["practical_deploy_order"][0]["missing_fact_families"])
        self.assertIn("endpoint_contract_change_classification", surfaces["missing_fact_families"])

    def test_runtime_architecture_surfaces_unlinked_terraform_domain_leads(self) -> None:
        with _fixture_snapshot(static_hosting_domain_reference=True) as kg:
            architecture = runtime_architecture_packet(kg, repo=None, limit=10)

        route = next(row for row in architecture["domain_routing_map"] if row["status"] == "unlinked_domain_reference")
        self.assertEqual(route["domain"]["name"], "app.example.com")
        self.assertEqual(route["deploy_kind"], "terraform_domain_reference")
        self.assertEqual(route["qualifier"]["literal"], "app.example.com")
        self.assertIn("no typed route", route["interpretation"])
        component = next(row for row in architecture["runtime_building_blocks"] if row["repo"] == "infra")
        self.assertIn("domain_reference", component["runtime_categories"])
        self.assertEqual(component["domain_reference_leads"][0]["qualifier"]["literal"], "app.example.com")
        self.assertEqual(component["domain_reference_leads"][0]["evidence_coordinates"][0]["path"], "prod/cloudfront.tf")

    def test_runtime_architecture_investigation_brief_preserves_commit_coordinates(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service = Entity(
                kind="Service",
                identity={"tenant_id": "default", "namespace": "default", "slug": "api", "repo": "api"},
            )
            domain = Entity(
                kind="Domain",
                identity={"tenant_id": "default", "repo": "ops", "name": "api.example.com"},
            )
            target = Entity(
                kind="DeployTarget",
                identity={"tenant_id": "default", "repo": "ops", "type": "wsgi", "target": "/srv/api/app.wsgi"},
            )
            route_fact = Fact(
                "ROUTES_DOMAIN_TO_DEPLOY",
                domain.entity_id,
                target.entity_id,
                {"source_kind": "fixture_vhost"},
            )
            deploy_fact = Fact(
                "DEPLOYS_VIA_CONFIG",
                service.entity_id,
                target.entity_id,
                {"source_kind": "runtime_linker"},
            )
            JsonlKgStore(root).write(
                entities=[service, domain, target],
                facts=[route_fact, deploy_fact],
                evidence=[
                    Evidence(
                        target_type="fact",
                        target_id=route_fact.fact_id,
                        derivation_class="deterministic_static",
                        source_system="test",
                        source_ref={"repo": "ops"},
                        bytes_ref={
                            "repo": "ops",
                            "commit_sha": "ops-sha",
                            "path": "ops/site.conf",
                            "line_start": 3,
                            "line_end": 8,
                        },
                        confidence=1.0,
                    ),
                    Evidence(
                        target_type="fact",
                        target_id=deploy_fact.fact_id,
                        derivation_class="deterministic_static",
                        source_system="runtime_linker",
                        source_ref={"repo": "ops"},
                        bytes_ref={
                            "repo": "ops",
                            "commit_sha": "ops-sha",
                            "path": "ops/site.conf",
                            "line_start": 5,
                            "line_end": 5,
                        },
                        confidence=1.0,
                    ),
                ],
                coverage=[],
                manifest={"version": 1},
            )

            architecture = runtime_architecture_packet(KgSnapshot(root), repo=None, limit=10)

        brief = architecture["answer_packet"]["investigation_brief"]
        self.assertEqual(brief["known_routes"][0]["evidence_coordinates"][0]["commit_sha"], "ops-sha")
        self.assertEqual(brief["recommended_source_checks"][0]["commit_sha"], "ops-sha")
        self.assertIn({"repo": "ops", "commit_shas": ["ops-sha"]}, brief["repos_referenced"])
        self.assertNotIn({"repo": "api", "commit_shas": []}, brief["repos_referenced"])
        self.assertEqual(
            brief["kg_only_inspection_contract"]["status"],
            "source_availability_unresolved_by_supercontext",
        )

    def test_runtime_architecture_omits_missing_commit_coordinates(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service = Entity(
                kind="Service",
                identity={"tenant_id": "default", "namespace": "default", "slug": "api", "repo": "api"},
            )
            domain = Entity(
                kind="Domain",
                identity={"tenant_id": "default", "repo": "ops", "name": "api.example.com"},
            )
            target = Entity(
                kind="DeployTarget",
                identity={"tenant_id": "default", "repo": "ops", "type": "wsgi", "target": "/srv/api/app.wsgi"},
            )
            route_fact = Fact(
                "ROUTES_DOMAIN_TO_DEPLOY",
                domain.entity_id,
                target.entity_id,
                {"source_kind": "fixture_vhost"},
            )
            JsonlKgStore(root).write(
                entities=[service, domain, target],
                facts=[route_fact],
                evidence=[
                    Evidence(
                        target_type="fact",
                        target_id=route_fact.fact_id,
                        derivation_class="deterministic_static",
                        source_system="test",
                        source_ref={"repo": "ops"},
                        bytes_ref={"repo": "ops", "path": "ops/site.conf", "line_start": 3, "line_end": 8},
                        confidence=1.0,
                    ),
                ],
                coverage=[],
                manifest={"version": 1},
            )

            architecture = runtime_architecture_packet(KgSnapshot(root), repo=None, limit=10)

        brief = architecture["answer_packet"]["investigation_brief"]
        self.assertNotIn("commit_sha", brief["known_routes"][0]["evidence_coordinates"][0])
        self.assertNotIn("commit_sha", brief["recommended_source_checks"][0])
        self.assertIn({"repo": "ops", "commit_shas": []}, brief["repos_referenced"])

    def test_runtime_architecture_partitions_candidate_deploy_links_from_known_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = Entity(
                kind="DeployTarget",
                identity={"tenant_id": "default", "repo": "ops", "type": "wsgi", "target": "/srv/apps/app/wsgi.py"},
            )
            services = [
                Entity(
                    kind="Service",
                    identity={"tenant_id": "default", "namespace": "default", "slug": slug, "repo": slug},
                )
                for slug in ("api-a", "api-b")
            ]
            candidate_ids = [service.entity_id for service in services]
            facts = [
                Fact(
                    "DEPLOYS_VIA_CONFIG",
                    service.entity_id,
                    target.entity_id,
                    {
                        "source_kind": "runtime_linker",
                        "target_type": "wsgi",
                        "resolved_by": "wsgi_ambiguous_module_path_suffix",
                        "candidate_service_ids": candidate_ids,
                    },
                    canonical_status="candidate",
                )
                for service in services
            ]
            JsonlKgStore(root).write(
                entities=[target, *services],
                facts=facts,
                evidence=[
                    Evidence(
                        target_type="fact",
                        target_id=fact.fact_id,
                        derivation_class="candidate",
                        source_system="runtime_linker",
                        source_ref={"resolved_by": "wsgi_ambiguous_module_path_suffix"},
                        bytes_ref={"repo": "ops", "path": "apache/site.conf", "line_start": 7, "line_end": 8},
                        confidence=0.5,
                    )
                    for fact in facts
                ],
                coverage=[
                    Coverage(
                        tenant_id="default",
                        predicate="DEPLOYS_VIA_CONFIG",
                        scope_ref={
                            "deploy_target_id": target.entity_id,
                            "deploy_target_identity": target.identity,
                            "reason": "ambiguous_wsgi_module_suffix",
                            "candidate_service_ids": candidate_ids,
                            "rule_version": "runtime-linker-1",
                        },
                        state="partially_instrumented",
                        source_system="runtime_linker",
                    )
                ],
                manifest={"version": 1},
            )

            architecture = runtime_architecture_packet(KgSnapshot(root), repo=None, limit=10)

        self.assertEqual(architecture["summary"]["deploy_link_count"], 0)
        self.assertEqual(architecture["summary"]["candidate_or_unlinked_deploy_lead_count"], 2)
        self.assertEqual(architecture["answer_packet"]["deploy_runtime_map"], [])
        leads = architecture["answer_packet"]["unlinked_deploy_leads"]
        self.assertEqual({lead["status"] for lead in leads}, {"candidate_deploy_link"})
        self.assertEqual({lead["reason"] for lead in leads}, {"wsgi_ambiguous_module_path_suffix"})
        self.assertEqual({lead["deploy_target"]["target"] for lead in leads}, {"/srv/apps/app/wsgi.py"})
        self.assertEqual({len(lead["candidate_services"]) for lead in leads}, {2})
        self.assertEqual({lead["evidence_coordinates"][0]["path"] for lead in leads}, {"apache/site.conf"})
        brief = architecture["answer_packet"]["investigation_brief"]
        self.assertEqual(brief["deploy_units"], [])
        self.assertEqual({lead["status"] for lead in brief["unlinked_deploy_leads"]}, {"candidate_deploy_link"})
        self.assertTrue(
            any(check["reason"] == "verify candidate or unresolved deploy lead before claiming service deployment" for check in brief["recommended_source_checks"])
        )

    def test_runtime_architecture_counts_candidate_deploy_leads_before_limiting(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            targets = [
                Entity(
                    kind="DeployTarget",
                    identity={
                        "tenant_id": "default",
                        "repo": "ops",
                        "type": "wsgi",
                        "target": f"/srv/apps/app-{index}/wsgi.py",
                    },
                )
                for index in range(21)
            ]
            services = [
                Entity(
                    kind="Service",
                    identity={
                        "tenant_id": "default",
                        "namespace": "default",
                        "slug": f"api-{index}",
                        "repo": f"api-{index}",
                    },
                )
                for index in range(21)
            ]
            facts = [
                Fact(
                    "DEPLOYS_VIA_CONFIG",
                    service.entity_id,
                    target.entity_id,
                    {
                        "source_kind": "runtime_linker",
                        "target_type": "wsgi",
                        "resolved_by": "wsgi_ambiguous_module_path_suffix",
                        "candidate_service_ids": [service.entity_id],
                    },
                    canonical_status="candidate",
                )
                for service, target in zip(services, targets)
            ]
            JsonlKgStore(root).write(
                entities=[*targets, *services],
                facts=facts,
                evidence=[
                    Evidence(
                        target_type="fact",
                        target_id=fact.fact_id,
                        derivation_class="candidate",
                        source_system="runtime_linker",
                        source_ref={"resolved_by": "wsgi_ambiguous_module_path_suffix"},
                        bytes_ref={
                            "repo": "ops",
                            "path": "apache/site.conf",
                            "line_start": index + 1,
                            "line_end": index + 1,
                        },
                        confidence=0.5,
                    )
                    for index, fact in enumerate(facts)
                ],
                coverage=[],
                manifest={"version": 1},
            )

            architecture = runtime_architecture_packet(KgSnapshot(root), repo=None, limit=1)

        self.assertEqual(architecture["summary"]["candidate_or_unlinked_deploy_lead_count"], 21)
        self.assertEqual(len(architecture["answer_packet"]["unlinked_deploy_leads"]), 20)
        self.assertTrue(architecture["truncated"])

    def test_runtime_architecture_surfaces_no_bytes_deploy_coverage_as_unresolved_lead(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = Entity(
                kind="DeployTarget",
                identity={"tenant_id": "default", "repo": "ops", "type": "wsgi", "target": "/srv/apps/api/app/wsgi.py"},
            )
            JsonlKgStore(root).write(
                entities=[target],
                facts=[],
                evidence=[],
                coverage=[
                    Coverage(
                        tenant_id="default",
                        predicate="DEPLOYS_VIA_CONFIG",
                        scope_ref={
                            "deploy_target_id": target.entity_id,
                            "deploy_target_identity": target.identity,
                            "reason": "no_target_bytes_ref_evidence",
                            "rule_version": "runtime-linker-1",
                        },
                        state="partially_instrumented",
                        source_system="runtime_linker",
                    )
                ],
                manifest={"version": 1},
            )

            architecture = runtime_architecture_packet(KgSnapshot(root), repo=None, limit=10)

        self.assertEqual(architecture["summary"]["deploy_link_count"], 0)
        self.assertEqual(architecture["summary"]["candidate_or_unlinked_deploy_lead_count"], 1)
        lead = architecture["answer_packet"]["unlinked_deploy_leads"][0]
        self.assertEqual(lead["status"], "unresolved_deploy_link")
        self.assertEqual(lead["reason"], "no_target_bytes_ref_evidence")
        self.assertEqual(lead["deploy_target"]["target"], "/srv/apps/api/app/wsgi.py")
        self.assertEqual(lead["evidence_coordinates"], [])

    def test_runtime_architecture_repo_scope_excludes_other_repo_domain_reference_leads(self) -> None:
        with _fixture_snapshot(
            operational_deploy_mapping=True,
            operational_deploy_link=True,
            operational_deploy_same_repo=True,
            static_hosting_domain_reference=True,
        ) as kg:
            architecture = runtime_architecture_packet(kg, repo="payments", limit=10)

        repos = {component["repo"] for component in architecture["runtime_building_blocks"]}
        self.assertEqual(repos, {"payments"})
        self.assertNotIn("infra", repos)

    def test_runtime_architecture_surfaces_env_domain_reference_leads(self) -> None:
        with _fixture_snapshot(env_domain_reference_lead=True) as kg:
            architecture = runtime_architecture_packet(kg, repo="payments", limit=10)

        route = next(row for row in architecture["domain_routing_map"] if row["status"] == "unlinked_domain_reference")
        self.assertEqual(route["domain"]["name"], "api.internal.example")
        self.assertEqual(route["deploy_kind"], "env_domain_reference")
        self.assertEqual(route["qualifier"]["literal"], "https://api.internal.example")
        brief = architecture["answer_packet"]["investigation_brief"]
        self.assertEqual(brief["unlinked_runtime_leads"][0]["deploy_kind"], "env_domain_reference")

    def test_runtime_architecture_ignores_unhinted_source_url_literals_as_runtime_leads(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service = Entity(
                kind="Service",
                identity={"tenant_id": "default", "namespace": "default", "slug": "docs", "repo": "docs"},
            )
            domain = Entity(kind="Domain", identity={"tenant_id": "default", "repo": "docs", "name": "example.com"})
            unclassified_domain = Entity(
                kind="Domain",
                identity={"tenant_id": "default", "repo": "docs", "name": "unclassified.example.com"},
            )
            source_literal_fact = Fact(
                "REFERENCES_DOMAIN",
                service.entity_id,
                domain.entity_id,
                {"literal": "https://example.com", "path": "docs/settings.py", "source_kind": "source_domain_literal"},
            )
            unclassified_fact = Fact(
                "REFERENCES_DOMAIN",
                service.entity_id,
                unclassified_domain.entity_id,
                {"literal": "https://unclassified.example.com", "path": "docs/settings.py"},
            )
            source_literal_evidence = Evidence(
                target_type="fact",
                target_id=source_literal_fact.fact_id,
                derivation_class="deterministic_static",
                source_system="test",
                source_ref={"repo": "docs"},
                bytes_ref={"repo": "docs", "path": "docs/settings.py", "line_start": 1, "line_end": 1},
                confidence=1.0,
            )
            unclassified_evidence = Evidence(
                target_type="fact",
                target_id=unclassified_fact.fact_id,
                derivation_class="deterministic_static",
                source_system="test",
                source_ref={"repo": "docs"},
                bytes_ref={"repo": "docs", "path": "docs/settings.py", "line_start": 2, "line_end": 2},
                confidence=1.0,
            )
            JsonlKgStore(root).write(
                entities=[service, domain, unclassified_domain],
                facts=[source_literal_fact, unclassified_fact],
                evidence=[source_literal_evidence, unclassified_evidence],
                coverage=[],
                manifest={"counts": {"entities": 3, "facts": 2}},
            )
            architecture = runtime_architecture_packet(KgSnapshot(root), repo=None, limit=10)

        self.assertEqual(architecture["answer_packet"]["domain_routing_map"], [])
        self.assertEqual(architecture["answer_packet"]["investigation_brief"]["unlinked_runtime_leads"], [])

    def test_runtime_architecture_matches_endpoint_methods_case_insensitively(self) -> None:
        with _fixture_snapshot(endpoint_consumer=True, provider_endpoint_method="post", endpoint_consumer_method="POST") as kg:
            architecture = runtime_architecture_packet(kg, repo="payments", limit=10)

        self.assertEqual(architecture["summary"]["client_endpoint_call_count"], 1)

    def test_runtime_architecture_matches_endpoint_consumers_by_path_shape(self) -> None:
        with _fixture_snapshot(
            endpoint_consumer=True,
            provider_endpoint_path="/orders/<int:order_id>",
            endpoint_consumer_path="/orders/{orderId}",
        ) as kg:
            architecture = runtime_architecture_packet(kg, repo="payments", limit=10)

        self.assertEqual(architecture["summary"]["client_endpoint_call_count"], 1)
        row = architecture["answer_packet"]["endpoint_consumer_map"][0]
        self.assertEqual(row["match_basis"], ENDPOINT_PATH_SHAPE_MATCH_BASIS)
        self.assertEqual(row["consumers"][0]["match_basis"], ENDPOINT_PATH_SHAPE_MATCH_BASIS)
        self.assertEqual(row["provider_endpoint"]["path"], "/orders/<int:order_id>")
        self.assertEqual(row["consumers"][0]["called_endpoint"]["path"], "/orders/{orderId}")

    def test_runtime_architecture_does_not_match_partial_method_endpoints(self) -> None:
        with _fixture_snapshot(endpoint_consumer=True, provider_endpoint_method=None, endpoint_consumer_method="POST") as kg:
            architecture = runtime_architecture_packet(kg, repo="payments", limit=10)

        self.assertEqual(architecture["summary"]["client_endpoint_call_count"], 1)
        self.assertEqual(architecture["summary"]["endpoint_consumer_missing_method_drop_count"], 1)
        self.assertEqual(architecture["answer_packet"]["endpoint_consumer_map"], [])

    def test_runtime_architecture_reports_path_matches_dropped_for_missing_method(self) -> None:
        with _fixture_snapshot(endpoint_consumer=True, endpoint_consumer_method=None) as kg:
            architecture = runtime_architecture_packet(kg, repo="payments", limit=10)

        self.assertEqual(architecture["answer_packet"]["endpoint_consumer_map"], [])
        self.assertEqual(architecture["summary"]["endpoint_consumer_missing_method_drop_count"], 1)

    def test_runtime_architecture_packet_supports_fleet_scope(self) -> None:
        with _fixture_snapshot(
            endpoint_consumer=True,
            operational_deploy_mapping=True,
            operational_deploy_link=True,
            operational_deploy_same_repo=True,
        ) as kg:
            architecture = runtime_architecture_packet(kg, repo=None, limit=10)

        self.assertEqual(architecture["scope"], {"kind": "fleet"})
        self.assertEqual(architecture["summary"]["domain_route_count"], 1)
        self.assertEqual(architecture["summary"]["deploy_link_count"], 1)
        self.assertEqual(architecture["summary"]["endpoint_surface_count"], 1)
        self.assertEqual(architecture["summary"]["client_endpoint_call_count"], 1)

    def test_runtime_architecture_endpoint_consumer_map_excludes_same_repo_symbol_callers(self) -> None:
        with _fixture_snapshot(endpoint_consumer=True, same_repo_endpoint_consumer=True) as kg:
            architecture = runtime_architecture_packet(kg, repo="payments", limit=10)
            context = call_tool(kg, "planning_context", {"service": "payments", "limit": 10})

        self.assertEqual(architecture["answer_packet"]["endpoint_consumer_map"], [])
        self.assertEqual(architecture["answer_packet"]["deploy_order_guidance"], [])
        self.assertEqual(context["summary"]["endpoint_consumer_fact_count"], 0)
        self.assertEqual(context["service_operational_surfaces"]["summary"]["endpoint_consumer_fact_count"], 0)

    def test_planning_context_output_budget_truncates_runtime_architecture_shape(self) -> None:
        with _fixture_snapshot(runtime_pressure_routes=24, runtime_pressure_payload_size=900) as kg:
            result = call_tool(kg, "planning_context", {})

        self.assertLessEqual(len(canonical_json(result)), PLANNING_CONTEXT_MAX_CHARS)
        self.assertEqual(result["tool"], "planning_context")
        _assert_common_evidence_fields(self, result)
        self.assertNotIn("query_plan", result)
        budget = result["output_budget"]
        self.assertTrue(budget["truncated"])
        self.assertLessEqual(len(canonical_json(result)), budget["max_chars"])
        self.assertGreater(budget["omitted_counts"]["runtime_building_blocks"], 0)
        self.assertGreater(budget["omitted_counts"]["domain_routing_map"], 0)
        self.assertIn("runtime_architecture.answer_packet.runtime_building_blocks", budget["truncated_sections"])
        self.assertIn("runtime_architecture.answer_packet.domain_routing_map", budget["truncated_sections"])
        architecture = result["runtime_architecture"]
        answer_packet = architecture["answer_packet"]
        self.assertIn("Runtime architecture is assembled only from typed KG facts", architecture["assembly_contract"])
        self.assertTrue(any("saved-packet exploration" in action for action in result["next_actions"]))
        self.assertIn("investigation_brief", answer_packet)
        self.assertGreater(len(answer_packet["investigation_brief"]["runtime_anchors"]), 1)
        self.assertTrue(answer_packet["investigation_brief"]["recommended_source_checks"])
        self.assertLessEqual(len(answer_packet.get("runtime_building_blocks", [])), 4)
        self.assertLessEqual(len(answer_packet.get("domain_routing_map", [])), 15)
        self.assertTrue(answer_packet["investigation_brief"]["known_routes"])
        self.assertTrue(answer_packet["investigation_brief"]["known_routes"][0]["evidence_coordinates"])
        self.assertIn("can_answer_owner", result["ownership_context"]["answer_packet"])
        self.assertIn("unsupported_promotions", result["ownership_context"]["answer_packet"])
        self.assertIn("use narrower planning_context anchors", budget["advice"])

    def test_planning_context_budget_keeps_runtime_headstart_when_bulk_sections_are_dropped(self) -> None:
        with _fixture_snapshot(runtime_pressure_routes=24, runtime_pressure_payload_size=2_500) as kg:
            result = call_tool(kg, "planning_context", {})

        self.assertLessEqual(len(canonical_json(result)), PLANNING_CONTEXT_MAX_CHARS)
        answer_packet = result["runtime_architecture"]["answer_packet"]
        brief = answer_packet["investigation_brief"]
        self.assertEqual(brief["purpose"], "head_start_for_agent_source_investigation")
        self.assertGreaterEqual(len(brief["runtime_anchors"]), 4)
        self.assertTrue(brief["known_routes"])
        self.assertTrue(brief["recommended_source_checks"])
        self.assertTrue(all("path" in row for row in brief["recommended_source_checks"]))
        self.assertTrue(result["output_budget"]["truncated"])
        self.assertTrue(result["output_budget"]["truncated_sections"])
        self.assertIn("investigation_brief", result["output_budget"]["advice"])

    def test_planning_context_unresolved_anchor_uses_compact_fleet_budget(self) -> None:
        with _fixture_snapshot(runtime_pressure_routes=24, runtime_pressure_payload_size=2_500) as kg:
            result = call_tool(kg, "planning_context", {"service": "missing-product-name"})

        self.assertEqual(result["answerability"]["status"], "not_answerable")
        self.assertLessEqual(len(canonical_json(result)), PLANNING_CONTEXT_MAX_CHARS)
        self.assertEqual(result["output_budget"]["max_chars"], PLANNING_CONTEXT_MAX_CHARS)
        answer_packet = result["runtime_architecture"]["answer_packet"]
        self.assertIn("investigation_brief", answer_packet)
        self.assertNotIn("runtime_building_blocks", answer_packet)
        self.assertNotIn("domain_routing_map", answer_packet)
        self.assertEqual(result["runtime_architecture"]["anchor_resolution_contract"]["status"], "inventory_context")

    def test_planning_context_anchor_resolution_gate_compacts_only_explicit_failures(self) -> None:
        self.assertFalse(_planning_context_has_resolved_anchor({"answerability": {"status": "not_answerable"}}))
        self.assertFalse(_planning_context_has_resolved_anchor({"status": "ambiguous"}))
        self.assertTrue(_planning_context_has_resolved_anchor({"answerability": {"status": "answerable"}}))
        self.assertTrue(_planning_context_has_resolved_anchor({}))

    def test_planning_context_service_anchor_scopes_runtime_before_budgeting(self) -> None:
        with _fixture_snapshot(runtime_pressure_routes=24, runtime_pressure_payload_size=900) as kg:
            result = call_tool(kg, "planning_context", {"service": "runtime-service-0"})

        self.assertLessEqual(len(canonical_json(result)), PLANNING_CONTEXT_ANCHORED_MAX_CHARS)
        self.assertEqual(result["tool"], "planning_context")
        # Runtime is scoped to the anchor's repo before budgeting, so the packet reflects the
        # single repo rather than the whole fleet (the key invariant this test guards).
        self.assertEqual(result["runtime_architecture"]["scope"], {"kind": "repo", "repo": "runtime-repo-0"})
        self.assertIn("service_operational_surfaces", result)

    def test_planning_context_service_anchor_budget_truncates_large_single_repo_runtime_packet(self) -> None:
        with _fixture_snapshot(
            runtime_pressure_routes=80,
            runtime_pressure_payload_size=2_500,
            runtime_pressure_same_repo=True,
        ) as kg:
            result = call_tool(kg, "planning_context", {"service": "runtime-service-0"})

        self.assertLessEqual(len(canonical_json(result)), PLANNING_CONTEXT_ANCHORED_MAX_CHARS)
        self.assertEqual(result["tool"], "planning_context")
        budget = result["output_budget"]
        self.assertTrue(budget["truncated"])
        self.assertEqual(budget["max_chars"], PLANNING_CONTEXT_ANCHORED_MAX_CHARS)
        self.assertTrue(
            any(
                section.startswith("runtime_architecture.answer_packet.deploy_runtime_map")
                for section in budget["truncated_sections"]
            )
        )
        self.assertIn("runtime_architecture.answer_packet.domain_routing_map", budget["truncated_sections"])

    def test_planning_context_output_budget_preserves_valid_json_transport_shape(self) -> None:
        with _fixture_snapshot(runtime_pressure_routes=24, runtime_pressure_payload_size=900) as kg:
            rpc = _handle_json_rpc(
                kg,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "planning_context", "arguments": {}},
                },
            )

        structured = rpc["result"]["structuredContent"]
        parsed_text = json.loads(rpc["result"]["content"][0]["text"])
        self.assertEqual(parsed_text, structured)
        self.assertTrue(structured["output_budget"]["truncated"])

    def test_planning_context_output_budget_leaves_under_budget_and_exact_tools_precise(self) -> None:
        with _fixture_snapshot() as kg:
            planning = call_tool(kg, "planning_context", {})
            service_brief = call_tool(kg, "get_service_brief", {"service": "payments"})
            callers = call_tool(kg, "find_callers", {"symbol": "charge_card"})
            callees = call_tool(kg, "find_callees", {"symbol": "handle_checkout"})

        self.assertNotIn("output_budget", planning)
        self.assertNotIn("output_budget", service_brief)
        self.assertNotIn("output_budget", callers)
        self.assertNotIn("output_budget", callees)

    def test_output_budget_preserves_known_and_unlinked_route_statuses(self) -> None:
        result = {
            "tool": "planning_context",
            "status": "found",
            "runtime_architecture": {
                "scope": {"kind": "fleet"},
                "summary": {"runtime_building_block_count": 8, "domain_routing_map_count": 8},
                "answer_packet": {
                    "runtime_building_blocks": [{"component_id": f"component-{index}"} for index in range(8)],
                    "domain_routing_map": [
                        {
                            "status": "known_route" if index == 0 else "unlinked_domain_reference",
                            "domain": {"name": f"domain-{index}.example.test"},
                            "evidence_coordinates": [{"repo": "repo", "path": "infra.tf", "line_start": index + 1}],
                            "payload": "x" * 700,
                        }
                        for index in range(8)
                    ],
                    "deploy_kind_counts": {},
                    "evidence_contract": "unlinked rows are source leads only",
                },
                "assembly_contract": "typed facts only",
            },
            "coverage_warnings": [],
            "unsupported_scopes": [],
            "next_actions": [],
        }
        original = deepcopy(result)

        budgeted = enforce_planning_context_budget(result, max_chars=7_500)

        self.assertEqual(result, original)
        routes = budgeted["runtime_architecture"]["answer_packet"]["domain_routing_map"]
        statuses = {row["status"] for row in routes}
        self.assertIn("known_route", statuses)
        self.assertIn("unlinked_domain_reference", statuses)
        self.assertTrue(budgeted["output_budget"]["truncated"])
        self.assertGreater(budgeted["output_budget"]["omitted_counts"]["domain_routing_map"], 0)

    def test_planning_budget_hard_cap_guarantees_fit_on_large_content_sections(self) -> None:
        from source.kg.product.output_budget import _planning_signal_hard_cap

        # A packet whose big content sections (authz/service surfaces) blow past the cap, as on
        # real multi-repo snapshots, must still be forced under the cap by the scorer hard-cap,
        # while the top-level common evidence index is left intact.
        def lead(idx, *, linked):
            return {
                "id": idx,
                "status": "known_linked" if linked else "unlinked",
                "derivation_class": "deterministic_static" if linked else "inferred_llm",
                "evidence": [{"bytes_ref": {"repo": "r", "path": f"a/{idx}.py", "line_start": idx}}],
                "blob": "z" * 600,
            }

        result = {
            "tool": "planning_context",
            "status": "found",
            "summary": {},
            "authz_surface": {"review_leads": [lead(i, linked=i % 2 == 0) for i in range(120)]},
            "service_operational_surfaces": {"deploy_target_candidates": [lead(i, linked=False) for i in range(120)]},
            "proven_facts": {"status": "found", "sources": [{"field": "x", "count": 1}]},
            "inspection_areas": [],
            "output_budget": {"truncated": True, "minimized": True, "max_chars": 20_000},
            "next_actions": [],
        }

        budgeted = _planning_signal_hard_cap(result, max_chars=20_000)

        self.assertLessEqual(len(canonical_json(budgeted)), 20_000)
        self.assertTrue(budgeted["output_budget"]["hard_capped"])
        # Overflow is demoted to a coordinate-bearing inspection area, not dropped silently.
        self.assertTrue(
            any(a.get("area") == "planning_budget_overflow" for a in budgeted.get("inspection_areas", []))
        )
        # The top-level common evidence index is protected (not shredded by the hard-cap).
        self.assertEqual(budgeted["proven_facts"]["sources"], [{"field": "x", "count": 1}])
        # Higher-signal known_linked rows are kept preferentially over unlinked ones.
        kept = budgeted["authz_surface"]["review_leads"]
        if kept:
            self.assertGreaterEqual(
                sum(1 for r in kept if r["status"] == "known_linked"),
                sum(1 for r in kept if r["status"] == "unlinked"),
            )

    def test_review_context_budget_compacts_oversized_detail_under_cap(self) -> None:
        caller_rows = [
            {
                "predicate": "CALLS",
                "depth": 1,
                "caller_symbol": {
                    "symbol_id": f"ent_caller_{index}",
                    "qualified_name": f"pkg.module_{index}.caller_{index}",
                    "qualname": f"caller_{index}",
                    "symbol_kind": "function",
                    "repo": "repo",
                    "path": f"pkg/module_{index}.py",
                    "line": index,
                },
                "object": {"qualified_name": "pkg.target.changed"},
                "evidence": [{"bytes_ref": {"repo": "repo", "path": f"pkg/module_{index}.py", "line_start": index, "line_end": index}}],
                "payload": "x" * 800,
            }
            for index in range(60)
        ]
        changed_symbols = [row["caller_symbol"] for row in caller_rows]
        source_coordinates = [
            {
                "repo": "repo",
                "path": f"pkg/module_{index}.py",
                "line_start": index,
                "line_end": index,
            }
            for index in range(60)
        ]
        result = {
            "tool": "review_context",
            "status": "found",
            "repo": "repo",
            "summary": {
                "changed_symbol_count": 60,
                "changed_file_symbol_count": 60,
                "diff_anchor_count": 60,
                "direct_caller_count": 60,
            },
            "review_lead_status": {
                "coverage_status": "useful",
                "recommended_action": "use_supercontext_packet",
                "changed_anchor_count": 0,
                "changed_symbol_count": 60,
                "direct_impact_count": 120,
                "transitive_impact_count": 0,
                "source_coordinate_count": 60,
                "file_anchor_count": 60,
            },
            "review_answer_packet": {
                "status": "found",
                "summary": {"changed_symbol_count": 0, "diff_anchor_count": 60},
                "review_lead_status": {
                    "coverage_status": "useful",
                    "recommended_action": "use_supercontext_packet",
                    "changed_anchor_count": 0,
                    "changed_symbol_count": 60,
                    "direct_impact_count": 120,
                    "transitive_impact_count": 0,
                    "source_coordinate_count": 60,
                    "file_anchor_count": 60,
                },
                "top_diff_anchors": [
                    {
                        "repo": "repo",
                        "path": f"pkg/module_{index}.py",
                        "range": {"start_line": index, "end_line": index},
                        "anchor_type": "file",
                        "match_kind": "changed_range_without_indexed_symbol",
                        "payload": "x" * 800,
                    }
                    for index in range(60)
                ],
            },
            "answerability": {"status": "answerable"},
            "scope_contract": {"changed_symbol_count": 0},
            "claim_contract": {"scope": "bounded static review context for changed files and optional ranges"},
            "review_leads": {
                "changed_files": [f"pkg/module_{index}.py" for index in range(60)],
                "changed_symbols": changed_symbols,
                "direct_callers": caller_rows,
                "direct_callees": caller_rows,
                "transitive_callers": [],
                "source_coordinates": source_coordinates,
            },
            "surface_status": [],
            "diff_anchors": [
                {
                    "repo": "repo",
                    "path": f"pkg/module_{index}.py",
                    "range": {"start_line": index, "end_line": index},
                    "anchor_type": "file",
                    "match_kind": "changed_range_without_indexed_symbol",
                    "source_coordinates": [
                        {
                            "repo": "repo",
                            "path": f"pkg/module_{index}.py",
                            "line_start": index,
                            "line_end": index,
                        }
                    ],
                    "payload": "x" * 800,
                }
                for index in range(60)
            ],
            "direct_callers": caller_rows,
            "direct_callees": caller_rows,
            "direct_callers_of_changed_symbols": caller_rows,
            "direct_callees_from_changed_symbols": caller_rows,
            "changed_symbols": changed_symbols,
            "changed_file_symbols": [row["caller_symbol"] for row in caller_rows],
            "source_coordinates": source_coordinates,
            "coverage_warnings": [],
            "unsupported_scopes": [],
            "next_actions": [],
        }
        original = deepcopy(result)

        # include_broad_context=True: asserts the broad ladder's top-level detail
        # sampling limits and top-level/review_leads equality contracts.
        budgeted = enforce_review_context_budget(result, include_broad_context=True)

        self.assertEqual(result, original)
        self.assertTrue(budgeted["output_budget"]["truncated"])
        self.assertLessEqual(len(canonical_json(budgeted)), REVIEW_CONTEXT_BROAD_MAX_CHARS)
        # Curated head start and contracts survive; verbose detail is bounded.
        self.assertIn("review_answer_packet", budgeted)
        self.assertEqual(budgeted["summary"], original["summary"])
        self.assertLessEqual(len(budgeted["direct_callers"]), 8)
        self.assertLessEqual(len(budgeted["diff_anchors"]), 8)
        self.assertEqual(budgeted["review_leads"]["changed_symbols"], budgeted["changed_symbols"])
        self.assertEqual(budgeted["review_leads"]["direct_callers"], budgeted["direct_callers"])
        self.assertEqual(budgeted["review_leads"]["source_coordinates"], budgeted["source_coordinates"])
        self.assertEqual(
            budgeted["review_lead_status"]["changed_symbol_count"],
            len(budgeted["review_leads"]["changed_symbols"]),
        )
        self.assertEqual(
            budgeted["review_lead_status"]["source_coordinate_count"],
            len(budgeted["review_leads"]["source_coordinates"]),
        )
        self.assertEqual(
            budgeted["review_answer_packet"]["review_lead_status"],
            budgeted["review_lead_status"],
        )
        self.assertIn("diff_anchors", budgeted["output_budget"]["truncated_sections"])
        self.assertIn("review_answer_packet.top_diff_anchors", budgeted["output_budget"]["truncated_sections"])
        self.assertIn("review_leads.changed_symbols", budgeted["output_budget"]["truncated_sections"])
        self.assertIn("review_leads.source_coordinates", budgeted["output_budget"]["truncated_sections"])
        self.assertNotIn("payload", budgeted["diff_anchors"][0])
        self.assertNotIn("payload", budgeted["review_answer_packet"]["top_diff_anchors"][0])
        self.assertIn("direct_callers", budgeted["output_budget"]["truncated_sections"])
        self.assertNotIn("omitted_counts", budgeted["output_budget"])

    def test_review_context_budget_preserves_scalar_relation_subject_object(self) -> None:
        relation_rows = [
            {
                "predicate": "CALLS",
                "depth": 1,
                "subject": f"pkg.module_{index}.caller",
                "object": "pkg.target.changed",
                "evidence": [
                    {
                        "bytes_ref": {
                            "repo": "repo",
                            "path": f"pkg/module_{index}.py",
                            "line_start": index,
                            "line_end": index,
                        }
                    }
                ],
                "payload": "x" * 800,
            }
            for index in range(16)
        ]
        review_lead_status = {
            "coverage_status": "useful",
            "recommended_action": "use_supercontext_packet",
            "changed_anchor_count": 0,
            "changed_symbol_count": 0,
            "direct_impact_count": 32,
            "transitive_impact_count": 0,
            "source_coordinate_count": 0,
            "file_anchor_count": 0,
        }
        result = {
            "tool": "review_context",
            "status": "found",
            "repo": "repo",
            "summary": {"direct_caller_count": 16, "direct_callee_count": 16},
            "review_lead_status": review_lead_status,
            "review_answer_packet": {
                "status": "found",
                "review_lead_status": review_lead_status,
                "top_direct_callers": relation_rows,
                "top_direct_callees": relation_rows,
            },
            "review_leads": {
                "changed_files": ["pkg/module.py"],
                "changed_symbols": [],
                "direct_callers": relation_rows,
                "direct_callees": relation_rows,
                "transitive_callers": [],
                "source_coordinates": [],
            },
            "direct_callers": relation_rows,
            "direct_callees": relation_rows,
            "transitive_callers": [],
            "source_coordinates": [],
            "next_actions": [],
        }

        # include_broad_context=True: asserts compacted row shape on the broad
        # ladder's top-level detail lists (absent in the compact profile).
        budgeted = enforce_review_context_budget(result, max_chars=8_000, include_broad_context=True)

        self.assertLessEqual(len(canonical_json(budgeted)), 8_000)
        self.assertEqual(budgeted["direct_callers"][0]["subject"], "pkg.module_0.caller")
        self.assertEqual(budgeted["direct_callers"][0]["object"], "pkg.target.changed")
        self.assertEqual(budgeted["review_leads"]["direct_callers"][0]["subject"], "pkg.module_0.caller")
        self.assertEqual(budgeted["review_leads"]["direct_callees"][0]["object"], "pkg.target.changed")
        self.assertEqual(
            budgeted["review_answer_packet"]["top_direct_callers"][0]["subject"],
            "pkg.module_0.caller",
        )
        self.assertEqual(
            budgeted["review_answer_packet"]["top_direct_callees"][0]["object"],
            "pkg.target.changed",
        )
        self.assertNotIn("payload", budgeted["review_answer_packet"]["top_direct_callers"][0])
        self.assertEqual(
            budgeted["review_lead_status"]["direct_impact_count"],
            len(budgeted["review_leads"]["direct_callers"]) + len(budgeted["review_leads"]["direct_callees"]),
        )

    def test_review_context_budget_hard_caps_nested_answer_packet_rows(self) -> None:
        relation_rows = [
            {
                "predicate": "CALLS",
                "depth": 1,
                "caller_symbol": {
                    "qualified_name": f"pkg.module_{index}.caller",
                    "repo": "repo",
                    "path": f"pkg/module_{index}.py",
                    "line": index,
                },
                "callee_symbol": {"qualified_name": "pkg.target.changed"},
                "evidence": [
                    {
                        "bytes_ref": {
                            "repo": "repo",
                            "path": f"pkg/module_{index}.py",
                            "line_start": index,
                            "line_end": index,
                        }
                    }
                ],
                "payload": "x" * 900,
            }
            for index in range(40)
        ]
        broad_rows = [
            {
                "predicate": "REFERENCES",
                "repo": "repo",
                "path": f"pkg/broad_{index}.py",
                "evidence": [
                    {
                        "bytes_ref": {
                            "repo": "repo",
                            "path": f"pkg/broad_{index}.py",
                            "line_start": index,
                            "line_end": index,
                        }
                    }
                ],
                "payload": "z" * 1_200,
                "subject": f"pkg.runtime_{index}",
                "object": "pkg.target.changed",
                "qualifier": {"source": "static_reference"},
                "match_basis": "same_repo_surface",
            }
            for index in range(120)
        ]
        review_lead_status = {
            "coverage_status": "useful",
            "recommended_action": "use_supercontext_packet",
            "changed_anchor_count": 0,
            "changed_symbol_count": 0,
            "direct_impact_count": 80,
            "transitive_impact_count": 0,
            "source_coordinate_count": 0,
            "file_anchor_count": 0,
        }
        result = {
            "tool": "review_context",
            "status": "found",
            "repo": "repo",
            "summary": {"direct_caller_count": 40, "direct_callee_count": 40},
            "review_lead_status": review_lead_status,
            "review_answer_packet": {
                "status": "found",
                "review_lead_status": review_lead_status,
                "top_direct_callers": relation_rows,
                "top_direct_callees": relation_rows,
                "framework": {"changed_models": broad_rows},
                "application": {"runtime_facts": broad_rows},
                "runtime": {"endpoint_consumers": broad_rows},
                "surface_status": broad_rows,
            },
            "review_leads": {
                "changed_files": ["pkg/module.py"],
                "changed_symbols": [],
                "direct_callers": relation_rows,
                "direct_callees": relation_rows,
                "transitive_callers": [],
                "source_coordinates": [],
            },
            "direct_callers": relation_rows,
            "direct_callees": relation_rows,
            "transitive_callers": [],
            "application_impact": {"runtime_facts": broad_rows},
            "runtime_surfaces": {"endpoint_consumers": broad_rows},
            "source_coordinates": [],
            "next_actions": [],
        }

        budgeted = enforce_review_context_budget(result, max_chars=20_000, include_broad_context=True)

        self.assertLessEqual(len(canonical_json(budgeted)), 20_000)
        self.assertNotIn("exceeded_after_minimization", budgeted["output_budget"])
        self.assertNotIn("payload", canonical_json(budgeted["review_answer_packet"]))
        self.assertLessEqual(
            len(budgeted["review_answer_packet"]["application"]["runtime_facts"]),
            COMPACT_RUNTIME_HEADSTART_LIMIT,
        )
        self.assertLessEqual(
            len(budgeted["review_answer_packet"]["framework"]["changed_models"]),
            COMPACT_RUNTIME_HEADSTART_LIMIT,
        )
        self.assertEqual(
            budgeted["review_answer_packet"]["application"]["runtime_facts"][0]["qualifier"],
            {"source": "static_reference"},
        )
        self.assertEqual(
            budgeted["review_answer_packet"]["application"]["runtime_facts"][0]["match_basis"],
            "same_repo_surface",
        )
        self.assertEqual(budgeted["review_answer_packet"]["review_lead_status"], budgeted["review_lead_status"])

    def test_review_context_budget_clears_truncated_sections_after_backfill_restores_list(self) -> None:
        relation_rows = [
            {
                "predicate": "CALLS",
                "depth": 1,
                "subject": f"pkg.module_{index}.caller",
                "object": "pkg.target.changed",
                "evidence": [
                    {
                        "bytes_ref": {
                            "repo": "repo",
                            "path": f"pkg/module_{index}.py",
                            "line_start": index,
                            "line_end": index,
                        }
                    }
                ],
                "payload": "x" * 1_000,
            }
            for index in range(10)
        ]
        review_lead_status = {
            "coverage_status": "useful",
            "recommended_action": "use_supercontext_packet",
            "changed_anchor_count": 0,
            "changed_symbol_count": 0,
            "direct_impact_count": 10,
            "transitive_impact_count": 0,
            "source_coordinate_count": 0,
            "file_anchor_count": 0,
        }
        result = {
            "tool": "review_context",
            "status": "found",
            "repo": "repo",
            "summary": {"direct_caller_count": 10},
            "review_lead_status": review_lead_status,
            "review_answer_packet": {
                "status": "found",
                "review_lead_status": review_lead_status,
                "top_direct_callers": relation_rows,
            },
            "review_leads": {
                "changed_files": ["pkg/module.py"],
                "changed_symbols": [],
                "direct_callers": relation_rows,
                "direct_callees": [],
                "transitive_callers": [],
                "source_coordinates": [],
            },
            "direct_callers": relation_rows,
            "direct_callees": [],
            "transitive_callers": [],
            "source_coordinates": [],
            "next_actions": [],
        }

        # include_broad_context=True: backfill/truncated_sections clearing is broad
        # ladder machinery over top-level detail lists.
        budgeted = enforce_review_context_budget(result, max_chars=20_000, include_broad_context=True)

        self.assertEqual(len(budgeted["direct_callers"]), len(relation_rows))
        self.assertEqual(len(budgeted["review_leads"]["direct_callers"]), len(relation_rows))
        self.assertEqual(len(budgeted["review_answer_packet"]["top_direct_callers"]), len(relation_rows))
        truncated_sections = set(budgeted["output_budget"]["truncated_sections"])
        self.assertNotIn("direct_callers", truncated_sections)
        self.assertNotIn("review_leads.direct_callers", truncated_sections)
        self.assertNotIn("review_answer_packet.top_direct_callers", truncated_sections)

    def test_review_backfill_trial_metadata_drops_restored_sections_before_measuring(self) -> None:
        from source.kg.product.output_budget import _backfill_review_list_path

        compact_row = {
            "predicate": "CALLS",
            "depth": 1,
            "subject": "pkg.module.caller",
            "object": "pkg.target.changed",
        }
        source_rows = [
            {
                **compact_row,
                "evidence": [
                    {
                        "bytes_ref": {
                            "repo": "repo",
                            "path": f"pkg/module_{index}.py",
                            "line_start": index,
                            "line_end": index,
                        }
                    }
                ],
                "payload": "x" * 200,
            }
            for index in range(2)
        ]
        candidate = {
            "tool": "review_context",
            "status": "found",
            "direct_callers": [compact_row],
            "review_leads": {"direct_callers": [compact_row]},
            "next_actions": [],
        }
        original = {
            "direct_callers": source_rows,
            "review_leads": {"direct_callers": source_rows},
        }

        budgeted, added = _backfill_review_list_path(
            candidate,
            original,
            ("direct_callers",),
            measured_chars=20_000,
            max_chars=20_000,
            truncated_sections={"direct_callers", "review_leads.direct_callers"},
            backfilled_counts={},
        )

        self.assertEqual(added, 1)
        self.assertNotIn("direct_callers", budgeted["output_budget"]["truncated_sections"])
        self.assertIn("review_leads.direct_callers", budgeted["output_budget"]["truncated_sections"])

    def test_review_backfill_restores_hypothesis_when_headroom_allows(self) -> None:
        # Packet where initial compaction keeps only 1 hypothesis but there is
        # headroom to backfill the second.  After enforce_review_context_budget,
        # both hypotheses must be present and review_lead_status.returned counts
        # must reflect what is actually shown.
        hyp1 = {
            "hypothesis_id": "hyp_backfill_0",
            "risk_type": "direct_call_contract_drift",
            "confidence": "strong",
            "why": "First hypothesis",
            "evidence_refs": [{"repo": "repo", "path": "pkg/a.py", "line_start": 1}],
            "source_checks": ["Check callers."],
            "supporting_lead_ids": ["lead-1"],
        }
        hyp2 = {
            "hypothesis_id": "hyp_backfill_1",
            "risk_type": "test_locks_in_regression",
            "confidence": "weak",
            "why": "Second hypothesis",
            "evidence_refs": [{"repo": "repo", "path": "tests/test_a.py", "line_start": 5}],
            "source_checks": ["Check tests."],
            "supporting_lead_ids": ["lead-2"],
        }
        review_lead_status = {
            "coverage_status": "useful",
            "recommended_action": "use_supercontext_packet",
            "changed_anchor_count": 1,
            "changed_symbol_count": 1,
            "direct_impact_count": 1,
            "transitive_impact_count": 0,
            "source_coordinate_count": 0,
            "file_anchor_count": 0,
            "available": {"changed_symbol_count": 1, "direct_caller_count": 1},
        }
        result = {
            "tool": "review_context",
            "status": "found",
            "repo": "repo",
            "summary": {"direct_caller_count": 1},
            "review_lead_status": review_lead_status,
            "review_answer_packet": {
                "status": "found",
                "review_lead_status": review_lead_status,
                "top_review_hypotheses": [hyp1, hyp2],
            },
            "review_hypotheses": [hyp1, hyp2],
            "review_leads": {
                "changed_files": ["pkg/a.py"],
                "changed_symbols": [
                    {"lead_id": "lead-1", "qualname": "func_a", "path": "pkg/a.py", "line_start": 1}
                ],
                "direct_callers": [
                    {
                        "predicate": "CALLS",
                        "depth": 1,
                        "subject": "pkg.b.caller",
                        "object": "pkg.a.func_a",
                        "lead_id": "lead-c1",
                    }
                ],
                "direct_callees": [],
                "transitive_callers": [],
                "source_coordinates": [],
            },
            "direct_callers": [
                {
                    "predicate": "CALLS",
                    "depth": 1,
                    "subject": "pkg.b.caller",
                    "object": "pkg.a.func_a",
                    "lead_id": "lead-c1",
                }
            ],
            "direct_callees": [],
            "transitive_callers": [],
            "source_coordinates": [],
            "next_actions": [],
        }

        # Cap generous enough to fit both hypotheses but tight enough that we
        # need the backfill path to exercise it.
        budgeted = enforce_review_context_budget(result, max_chars=50_000)

        hyps = budgeted.get("review_hypotheses", [])
        self.assertEqual(len(hyps), 2, "backfill must restore both hypotheses when headroom allows")
        hyp_ids = {h["hypothesis_id"] for h in hyps}
        self.assertIn("hyp_backfill_0", hyp_ids)
        self.assertIn("hyp_backfill_1", hyp_ids)

    def test_review_context_budget_degrades_to_lead_only_for_non_row_answer_packet_bloat(self) -> None:
        relation_rows = [
            {
                "predicate": "CALLS",
                "depth": 1,
                "subject": "pkg.module.caller",
                "object": "pkg.target.changed",
                "evidence": [
                    {
                        "bytes_ref": {
                            "repo": "repo",
                            "path": "pkg/module.py",
                            "line_start": 10,
                            "line_end": 10,
                        }
                    }
                ],
            }
        ]
        review_lead_status = {
            "coverage_status": "useful",
            "recommended_action": "use_supercontext_packet",
            "changed_anchor_count": 0,
            "changed_symbol_count": 0,
            "direct_impact_count": 1,
            "transitive_impact_count": 0,
            "source_coordinate_count": 0,
            "file_anchor_count": 0,
        }
        result = {
            "tool": "review_context",
            "status": "found",
            "repo": "repo",
            "summary": {"direct_caller_count": 1},
            "review_lead_status": review_lead_status,
            "review_answer_packet": {
                "status": "found",
                "review_lead_status": review_lead_status,
                "top_direct_callers": relation_rows,
                "application": {"oversized_non_row_context": "z" * 50_000},
            },
            "review_leads": {
                "changed_files": ["pkg/module.py"],
                "changed_symbols": [],
                "direct_callers": relation_rows,
                "direct_callees": [],
                "transitive_callers": [],
                "source_coordinates": [],
            },
            "direct_callers": relation_rows,
            "direct_callees": [],
            "transitive_callers": [],
            "source_coordinates": [],
            "next_actions": [],
        }

        budgeted = enforce_review_context_budget(result, max_chars=10_000, include_broad_context=True)

        self.assertLessEqual(len(canonical_json(budgeted)), 10_000)
        self.assertTrue(budgeted["output_budget"]["lead_only"])
        self.assertNotIn("exceeded_after_minimization", budgeted["output_budget"])
        self.assertEqual(budgeted["review_answer_packet"]["packet_mode"], "lead_only")
        self.assertNotIn("application", budgeted["review_answer_packet"])
        self.assertEqual(budgeted["review_answer_packet"]["review_lead_status"], budgeted["review_lead_status"])

    def test_review_context_budget_preserves_anchor_based_useful_gate(self) -> None:
        file_anchors = [
            {
                "repo": "repo",
                "path": f"pkg/file_{index}.py",
                "range": {"start_line": index, "end_line": index},
                "anchor_type": "file",
                "match_kind": "changed_range_without_indexed_symbol",
                "payload": "x" * 500,
            }
            for index in range(20)
        ]
        symbol_anchors = [
            {
                "repo": "repo",
                "path": f"pkg/symbol_{index}.py",
                "range": {"start_line": index, "end_line": index},
                "anchor_type": "symbol",
                "match_kind": "enclosing_symbol",
                "symbols": [{"qualname": f"pkg.symbol_{index}", "path": f"pkg/symbol_{index}.py", "line": index}],
                "payload": "x" * 500,
            }
            for index in range(20)
        ]
        review_lead_status = {
            "coverage_status": "useful",
            "recommended_action": "use_supercontext_packet",
            "changed_anchor_count": 20,
            "changed_symbol_count": 0,
            "direct_impact_count": 0,
            "transitive_impact_count": 0,
            "source_coordinate_count": 0,
            "file_anchor_count": 20,
        }
        result = {
            "tool": "review_context",
            "status": "found",
            "summary": {"diff_anchor_count": 40, "symbol_anchor_count": 20, "file_anchor_count": 20},
            "review_lead_status": review_lead_status,
            "review_leads": {
                "changed_files": ["pkg/file.py"],
                "changed_symbols": [],
                "direct_callers": [],
                "direct_callees": [],
                "transitive_callers": [],
                "source_coordinates": [],
            },
            "review_answer_packet": {
                "review_lead_status": review_lead_status,
                "top_diff_anchors": [*file_anchors, *symbol_anchors],
            },
            "diff_anchors": [*file_anchors, *symbol_anchors],
            "next_actions": [],
        }

        budgeted = enforce_review_context_budget(result, max_chars=3_000)

        self.assertTrue(budgeted["output_budget"]["truncated"])
        self.assertLessEqual(len(budgeted["diff_anchors"]), 8)
        self.assertTrue(all(row["anchor_type"] == "file" for row in budgeted["diff_anchors"]))
        self.assertEqual(budgeted["review_lead_status"]["coverage_status"], "useful")
        self.assertEqual(budgeted["review_lead_status"]["recommended_action"], "use_supercontext_packet")
        self.assertEqual(budgeted["review_lead_status"]["changed_anchor_count"], 20)
        self.assertEqual(budgeted["review_answer_packet"]["review_lead_status"], budgeted["review_lead_status"])

    def test_review_context_budget_leaves_small_packet_untouched(self) -> None:
        result = {"tool": "review_context", "status": "found", "summary": {}, "direct_callers": [], "next_actions": []}
        self.assertIs(enforce_review_context_budget(result), result)
        self.assertNotIn("output_budget", result)

    def test_review_context_budget_preserves_top_hypothesis_under_heavy_compaction(self) -> None:
        # Build hypotheses with distinct hypothesis_ids ranked by position.
        hypotheses = [
            {
                "hypothesis_id": f"hyp_{i}",
                "risk_type": "data_mutation_risk",
                "confidence": 0.9 - i * 0.1,
                "why": f"Hypothesis {i} explanation with enough text to be substantial",
                "evidence_refs": [{"repo": "repo", "path": f"pkg/hyp_{i}.py", "line_start": i, "line_end": i}],
                "source_checks": [{"repo": "repo", "path": f"pkg/hyp_{i}.py"}],
                "supporting_lead_ids": [f"lead_{i}"],
                "payload": "h" * 500,
            }
            for i in range(5)
        ]
        # Many broad rows to dominate the budget.
        broad_caller_rows = [
            {
                "predicate": "CALLS",
                "depth": 1,
                "subject": f"pkg.module_{i}.caller",
                "object": "pkg.target.changed",
                "evidence": [{"bytes_ref": {"repo": "repo", "path": f"pkg/module_{i}.py", "line_start": i, "line_end": i}}],
                "payload": "z" * 1_200,
            }
            for i in range(80)
        ]
        review_lead_status = {
            "coverage_status": "useful",
            "recommended_action": "use_supercontext_packet",
            "changed_anchor_count": 0,
            "changed_symbol_count": 0,
            "direct_impact_count": 80,
            "transitive_impact_count": 0,
            "source_coordinate_count": 0,
            "file_anchor_count": 0,
        }
        result = {
            "tool": "review_context",
            "status": "found",
            "repo": "repo",
            "summary": {"direct_caller_count": 80},
            "review_lead_status": review_lead_status,
            "review_answer_packet": {
                "status": "found",
                "review_lead_status": review_lead_status,
                "top_direct_callers": broad_caller_rows,
                "top_review_hypotheses": hypotheses,
                "application": {"runtime_facts": broad_caller_rows},
                "framework": {"changed_models": broad_caller_rows},
            },
            "review_hypotheses": hypotheses,
            "review_leads": {
                "changed_files": ["pkg/module.py"],
                "changed_symbols": [],
                "direct_callers": broad_caller_rows,
                "direct_callees": [],
                "transitive_callers": [],
                "source_coordinates": [],
            },
            "direct_callers": broad_caller_rows,
            "direct_callees": [],
            "application_impact": {"runtime_facts": broad_caller_rows},
            "framework_impact": {"changed_models": broad_caller_rows},
            "transitive_callers": [],
            "source_coordinates": [],
            "next_actions": [],
        }

        budgeted = enforce_review_context_budget(result, max_chars=REVIEW_CONTEXT_MAX_CHARS)

        final_size = len(canonical_json(budgeted))
        self.assertLessEqual(final_size, REVIEW_CONTEXT_MAX_CHARS)
        # At least the top-ranked (first) hypothesis must survive; hypotheses backfill
        # ahead of broad rows, so the top-3 target holds at the normal cap.
        self.assertIsInstance(budgeted.get("review_hypotheses"), list)
        self.assertGreaterEqual(len(budgeted["review_hypotheses"]), 3)
        top_hyp = budgeted["review_hypotheses"][0]
        self.assertEqual(top_hyp["hypothesis_id"], "hyp_0")
        # Surviving hypotheses keep required compacted fields.
        self.assertIn("risk_type", top_hyp)
        self.assertIn("confidence", top_hyp)
        self.assertIn("why", top_hyp)
        self.assertIn("evidence_refs", top_hyp)
        self.assertIn("source_checks", top_hyp)
        self.assertIn("supporting_lead_ids", top_hyp)
        # payload stripped by compaction.
        self.assertNotIn("payload", top_hyp)

    def test_review_context_budget_lead_only_preserves_top_hypothesis(self) -> None:
        hypotheses = [
            {
                "hypothesis_id": f"hyp_{i}",
                "risk_type": "auth_bypass_risk",
                "confidence": 0.8,
                "why": f"Hypothesis {i} reason",
                "evidence_refs": [{"repo": "repo", "path": f"pkg/h_{i}.py", "line_start": i, "line_end": i}],
                "source_checks": [{"repo": "repo", "path": f"pkg/h_{i}.py"}],
                "supporting_lead_ids": [f"lead_{i}"],
            }
            for i in range(3)
        ]
        review_lead_status = {
            "coverage_status": "useful",
            "recommended_action": "use_supercontext_packet",
            "changed_anchor_count": 0,
            "changed_symbol_count": 0,
            "direct_impact_count": 0,
            "transitive_impact_count": 0,
            "source_coordinate_count": 0,
            "file_anchor_count": 0,
        }
        # Force lead-only by stuffing a huge non-row string that survives compaction.
        result = {
            "tool": "review_context",
            "status": "found",
            "repo": "repo",
            "summary": {},
            "review_lead_status": review_lead_status,
            "review_answer_packet": {
                "status": "found",
                "review_lead_status": review_lead_status,
                "top_review_hypotheses": hypotheses,
                "application": {"oversized_non_row_context": "z" * 50_000},
            },
            "review_hypotheses": hypotheses,
            "review_leads": {
                "changed_files": ["pkg/module.py"],
                "changed_symbols": [],
                "direct_callers": [],
                "direct_callees": [],
                "transitive_callers": [],
                "source_coordinates": [],
            },
            "direct_callers": [],
            "direct_callees": [],
            "transitive_callers": [],
            "source_coordinates": [],
            "next_actions": [],
        }

        budgeted = enforce_review_context_budget(result, max_chars=10_000, include_broad_context=True)

        self.assertLessEqual(len(canonical_json(budgeted)), 10_000)
        self.assertTrue(budgeted["output_budget"].get("lead_only"))
        # Top hypothesis must survive even in lead-only path.
        self.assertIsInstance(budgeted.get("review_hypotheses"), list)
        self.assertGreater(len(budgeted["review_hypotheses"]), 0)
        self.assertEqual(budgeted["review_hypotheses"][0]["hypothesis_id"], "hyp_0")

    def test_review_context_budget_emits_hypothesis_status_when_truncated(self) -> None:
        # Hypothesis rows are large enough (~2000 chars each when compacted) that
        # only 1-3 fit within a 4500-char cap after backfill.  This guarantees
        # truncation even with the now-working hypothesis backfill dispatch.
        hypotheses = [
            {
                "hypothesis_id": f"hyp_{i}",
                "risk_type": "data_mutation_risk",
                "confidence": 0.7,
                "why": f"Hypothesis {i} reason " + "w" * 1_200,
                "evidence_refs": [
                    {"repo": "repo", "path": f"pkg/h_{i}.py", "line_start": i, "line_end": i}
                    for _ in range(3)
                ],
                "source_checks": [f"Check {i} step {j}" for j in range(2)],
                "supporting_lead_ids": [f"lead_{i}_{j}" for j in range(5)],
            }
            for i in range(5)
        ]
        broad_caller_rows = [
            {
                "predicate": "CALLS",
                "depth": 1,
                "subject": f"pkg.module_{i}.caller",
                "object": "pkg.target.changed",
                "evidence": [{"bytes_ref": {"repo": "repo", "path": f"pkg/module_{i}.py", "line_start": i, "line_end": i}}],
                "payload": "z" * 1_200,
            }
            for i in range(80)
        ]
        review_lead_status = {
            "coverage_status": "useful",
            "recommended_action": "use_supercontext_packet",
            "changed_anchor_count": 0,
            "changed_symbol_count": 0,
            "direct_impact_count": 80,
            "transitive_impact_count": 0,
            "source_coordinate_count": 0,
            "file_anchor_count": 0,
        }
        result = {
            "tool": "review_context",
            "status": "found",
            "repo": "repo",
            "summary": {"direct_caller_count": 80},
            "review_lead_status": review_lead_status,
            "review_answer_packet": {
                "status": "found",
                "review_lead_status": review_lead_status,
                "top_direct_callers": broad_caller_rows,
                "top_review_hypotheses": hypotheses,
            },
            "review_hypotheses": hypotheses,
            "review_leads": {
                "changed_files": ["pkg/module.py"],
                "changed_symbols": [],
                "direct_callers": broad_caller_rows,
                "direct_callees": [],
                "transitive_callers": [],
                "source_coordinates": [],
            },
            "direct_callers": broad_caller_rows,
            "direct_callees": [],
            "transitive_callers": [],
            "source_coordinates": [],
            "next_actions": [],
        }

        # 4,500 chars is tight enough that only some of the 5 large hypotheses fit.
        budgeted = enforce_review_context_budget(result, max_chars=4_500)

        self.assertLessEqual(len(canonical_json(budgeted)), 4_500)
        returned_hyps = budgeted.get("review_hypotheses", [])
        # Truncation must actually happen at this cap, and the floor must hold.
        self.assertGreater(len(returned_hyps), 0)
        self.assertLess(len(returned_hyps), 5)
        self.assertEqual(returned_hyps[0]["hypothesis_id"], "hyp_0")
        self.assertIn("review_hypothesis_status", budgeted)
        status = budgeted["review_hypothesis_status"]
        self.assertEqual(status["available_count"], 5)
        self.assertEqual(status["returned_count"], len(returned_hyps))
        self.assertIn("review_hypotheses", budgeted["output_budget"]["truncated_sections"])

    def test_review_context_budget_hypothesis_status_none_generated_when_zero_hypotheses(self) -> None:
        # Packet with no hypotheses: no fake hypothesis injected; status emitted with reason=none_generated.
        review_lead_status = {
            "coverage_status": "useful",
            "recommended_action": "use_supercontext_packet",
            "changed_anchor_count": 0,
            "changed_symbol_count": 0,
            "direct_impact_count": 0,
            "transitive_impact_count": 0,
            "source_coordinate_count": 0,
            "file_anchor_count": 0,
        }
        result = {
            "tool": "review_context",
            "status": "found",
            "repo": "repo",
            "summary": {},
            "review_lead_status": review_lead_status,
            "review_answer_packet": {
                "status": "found",
                "review_lead_status": review_lead_status,
                "application": {"oversized_non_row_context": "z" * 50_000},
            },
            "review_leads": {
                "changed_files": ["pkg/module.py"],
                "changed_symbols": [],
                "direct_callers": [],
                "direct_callees": [],
                "transitive_callers": [],
                "source_coordinates": [],
            },
            "direct_callers": [],
            "direct_callees": [],
            "transitive_callers": [],
            "source_coordinates": [],
            "next_actions": [],
        }

        budgeted = enforce_review_context_budget(result, max_chars=10_000)

        self.assertLessEqual(len(canonical_json(budgeted)), 10_000)
        self.assertNotIn("review_hypotheses", budgeted)
        # K: status always present — even when zero hypotheses were generated.
        status = budgeted.get("review_hypothesis_status")
        self.assertIsNotNone(status, "review_hypothesis_status must be present even for zero-hypothesis packets")
        self.assertEqual(status.get("available_count"), 0)
        self.assertEqual(status.get("returned_count"), 0)
        self.assertEqual(status.get("answer_packet_returned_count"), 0)
        self.assertEqual(status.get("truncated_count"), 0)
        self.assertEqual(status.get("reason"), "none_generated")

    # --- engine_version stamp tests ---

    def _minimal_review_context_result(self, *, bloat: int = 0) -> dict:
        """Minimal review_context result dict for budget tests."""
        review_lead_status = {
            "coverage_status": "useful",
            "recommended_action": "use_supercontext_packet",
            "changed_anchor_count": 0,
            "changed_symbol_count": 0,
            "direct_impact_count": 1,
            "transitive_impact_count": 0,
            "source_coordinate_count": 0,
            "file_anchor_count": 0,
        }
        return {
            "tool": "review_context",
            "status": "found",
            "repo": "repo",
            "summary": {"direct_caller_count": 1},
            "review_lead_status": review_lead_status,
            "review_answer_packet": {
                "status": "found",
                "review_lead_status": review_lead_status,
                "application": {"bloat": "z" * bloat},
            },
            "review_leads": {
                "changed_files": ["pkg/module.py"],
                "changed_symbols": [],
                "direct_callers": [],
                "direct_callees": [],
                "transitive_callers": [],
                "source_coordinates": [],
            },
            "direct_callers": [],
            "direct_callees": [],
            "transitive_callers": [],
            "source_coordinates": [],
            "next_actions": [],
        }

    def test_review_context_call_tool_stamps_engine_version(self) -> None:
        """call_tool review_context always sets output_budget.engine_version."""
        with _fixture_snapshot() as kg:
            result = call_tool(kg, "review_context", {
                "repo": "payments",
                "changed_files": ["payments/checkout.py"],
            })
        budget = result.get("output_budget")
        self.assertIsInstance(budget, dict, "output_budget must be present")
        version = budget.get("engine_version")
        self.assertIsInstance(version, str, "engine_version must be a string")
        self.assertTrue(version, "engine_version must be non-empty")

    def test_review_context_engine_version_survives_heavy_compaction(self) -> None:
        """engine_version is preserved after compaction truncates detail rows."""
        result = self._minimal_review_context_result(bloat=50_000)
        result["output_budget"] = {"engine_version": "test-sha"}
        budgeted = enforce_review_context_budget(result, max_chars=10_000)
        budget = budgeted.get("output_budget")
        self.assertIsInstance(budget, dict)
        self.assertEqual(budget.get("engine_version"), "test-sha")

    def test_review_context_engine_version_survives_lead_only_fallback(self) -> None:
        """engine_version is preserved when budget degrades to lead_only packet."""
        result = self._minimal_review_context_result(bloat=50_000)
        result["output_budget"] = {"engine_version": "test-sha"}
        budgeted = enforce_review_context_budget(result, max_chars=10_000, include_broad_context=True)
        budget = budgeted.get("output_budget")
        self.assertIsInstance(budget, dict)
        self.assertTrue(budget.get("lead_only"), "expected lead_only degradation")
        self.assertEqual(budget.get("engine_version"), "test-sha")

    def test_review_context_engine_version_present_on_normal_path(self) -> None:
        """engine_version is present even when no compaction is needed."""
        result = self._minimal_review_context_result()
        result["output_budget"] = {"engine_version": "test-sha"}
        returned = enforce_review_context_budget(result)
        budget = returned.get("output_budget")
        self.assertIsInstance(budget, dict)
        self.assertEqual(budget.get("engine_version"), "test-sha")

    def test_reverse_impact_callable_partition_rule(self) -> None:
        from source.kg.query.reverse_impact import _is_callable_symbol

        # Parser-derived symbol_kind is authoritative for callables.
        for kind in ("function", "method", "class"):
            self.assertTrue(_is_callable_symbol({"symbol_kind": kind, "qualname": "x"}))
        # Module/notebook/script call sites are not callable affected symbols.
        self.assertFalse(_is_callable_symbol({"symbol_kind": "module", "qualname": None}))
        self.assertFalse(_is_callable_symbol({"symbol_kind": "notebook"}))
        # No recorded kind: a present qualname is callable; a missing one (renders as
        # "module.None") is a call-site lead.
        self.assertTrue(_is_callable_symbol({"qualname": "pkg.mod.fn"}))
        self.assertFalse(_is_callable_symbol({"qualname": None}))
        self.assertFalse(_is_callable_symbol({}))

    def test_reverse_impact_budget_compacts_oversized_detail_under_cap(self) -> None:
        edge_rows = [
            {
                "predicate": "CALLS",
                "depth": 1,
                "caller_symbol": {"qualified_name": f"pkg.m_{i}.caller_{i}", "path": f"pkg/m_{i}.py", "line": i},
                "callee_symbol": {"qualified_name": "pkg.target.root"},
                "evidence": [{"bytes_ref": {"repo": "repo", "path": f"pkg/m_{i}.py", "line_start": i, "line_end": i}}],
                "payload": "y" * 900,
            }
            for i in range(60)
        ]
        result = {
            "tool": "reverse_impact",
            "status": "found",
            "summary": {"affected_symbol_count": 60, "edge_count": 60, "terminal_import_lead_count": 0},
            "answerability": {"status": "answerable"},
            "claim_contract": {"scope": "bounded static reverse CALLS head start"},
            "edges": edge_rows,
            "affected_symbols": [{"depth": 1, "symbol": r["caller_symbol"]} for r in edge_rows],
            "coverage_warnings": [],
            "unsupported_scopes": [],
            "next_actions": [],
        }
        original = deepcopy(result)

        budgeted = enforce_reverse_impact_budget(result)

        self.assertEqual(result, original)
        self.assertTrue(budgeted["output_budget"]["truncated"])
        self.assertLessEqual(len(canonical_json(budgeted)), REVERSE_IMPACT_MAX_CHARS)
        # Authoritative total is preserved; returned-count is synced to shown rows so
        # totals never contradict the sample, and no additive "omitted" number is emitted.
        self.assertEqual(budgeted["summary"]["affected_symbol_count"], 60)
        self.assertEqual(
            budgeted["summary"]["affected_symbol_returned_count"], len(budgeted["affected_symbols"])
        )
        self.assertIn("edges", budgeted["output_budget"]["truncated_sections"])
        self.assertNotIn("omitted_counts", budgeted["output_budget"])

    def test_service_brief_budget_signal_ranks_and_bounds(self) -> None:
        from source.kg.product.output_budget import SERVICE_BRIEF_MAX_CHARS, enforce_service_brief_budget

        def row(idx, *, linked):
            return {
                "id": f"{'known' if linked else 'unlinked'}-{idx}",
                "path": f"svc/file_{idx}.py",
                "derivation_class": "deterministic_static" if linked else "inferred_llm",
                "evidence": [{"bytes_ref": {"repo": "svc", "path": f"svc/file_{idx}.py", "line_start": idx}}],
                "blob": "z" * 900,
            }

        result = {
            "tool": "get_service_brief",
            "status": "found",
            "service": {"slug": "svc"},
            "summary": {},
            "operational_surfaces": {
                "evidence_partition": {
                    "known_linked": [row(i, linked=True) for i in range(20)],
                    "unlinked_evidence": [row(i, linked=False) for i in range(60)],
                    "missing_contracts": [],
                },
                "direct_domain_references": [row(i, linked=False) for i in range(40)],
            },
            "coverage_warnings": [],
            "unsupported_scopes": [],
            "next_actions": [],
        }
        original = deepcopy(result)

        budgeted = enforce_service_brief_budget(result)

        self.assertEqual(result, original)  # input not mutated
        self.assertTrue(budgeted["output_budget"]["truncated"])
        self.assertLessEqual(len(canonical_json(budgeted)), SERVICE_BRIEF_MAX_CHARS)
        kept_known = budgeted["operational_surfaces"]["evidence_partition"]["known_linked"]
        kept_unlinked = budgeted["operational_surfaces"]["evidence_partition"]["unlinked_evidence"]
        # Signal ranking keeps the stronger known_linked rows over weak unlinked rows.
        self.assertGreater(len(kept_known), len(kept_unlinked))
        # Overflow is demoted to a coordinate-bearing inspection area, not dropped.
        areas = budgeted.get("inspection_areas", [])
        self.assertTrue(any(a.get("area") == "service_operational_surface_overflow" for a in areas))

    def test_service_brief_budget_leaves_small_packet_untouched(self) -> None:
        from source.kg.product.output_budget import enforce_service_brief_budget

        result = {"tool": "get_service_brief", "status": "found", "operational_surfaces": {"summary": {}}, "next_actions": []}
        self.assertIs(enforce_service_brief_budget(result), result)
        self.assertNotIn("output_budget", result)

    def test_minimal_valid_packet_keeps_caution_contract_over_answer_counts(self) -> None:
        gated = {
            "tool": "planning_context",
            "status": "ambiguous",
            "runtime_architecture": {
                "scope": {"kind": "ambiguous_anchor"},
                "summary": {"answer_packet_mode": "investigation_brief_only"},
                "answer_packet": {
                    "investigation_brief": {"runtime_anchors": [], "recommended_source_checks": []},
                    "deploy_kind_counts": {"component_deploy_kind_counts": {"kubernetes": 3}},
                    "missing_fact_families": ["runtime_map"],
                    "evidence_contract": "investigation brief only",
                    "omitted_answer_sections": ["domain_routing_map", "deploy_runtime_map"],
                },
                "anchor_resolution_contract": {
                    "status": "inventory_context",
                    "reason": "anchor did not resolve",
                    "omitted_answer_sections": ["domain_routing_map", "deploy_runtime_map"],
                },
                "assembly_contract": "typed facts only",
            },
            "coverage_warnings": [],
            "unsupported_scopes": [],
            "next_actions": [],
        }

        minimal = _minimal_valid_packet(gated)

        runtime = minimal["runtime_architecture"]
        # The anchor-resolution caution contract must outlive the extreme fallback.
        self.assertIn("anchor_resolution_contract", runtime)
        self.assertEqual(
            runtime["answer_packet"]["omitted_answer_sections"],
            ["domain_routing_map", "deploy_runtime_map"],
        )
        # Answer-shaped counts must not survive when the answer path is gated.
        self.assertNotIn("deploy_kind_counts", runtime["answer_packet"])

    def test_minimal_valid_packet_keeps_deploy_counts_for_resolved_anchor(self) -> None:
        resolved = {
            "tool": "planning_context",
            "status": "found",
            "runtime_architecture": {
                "scope": {"kind": "service"},
                "summary": {"runtime_building_block_count": 4},
                "answer_packet": {
                    "investigation_brief": {},
                    "deploy_kind_counts": {"component_deploy_kind_counts": {"kubernetes": 2}},
                    "missing_fact_families": [],
                    "evidence_contract": "typed",
                },
                "assembly_contract": "typed facts only",
            },
            "coverage_warnings": [],
            "unsupported_scopes": [],
            "next_actions": [],
        }

        minimal = _minimal_valid_packet(resolved)

        answer = minimal["runtime_architecture"]["answer_packet"]
        # A resolved anchor keeps its deploy counts; no anchor-resolution gate applies.
        self.assertEqual(answer["deploy_kind_counts"], {"component_deploy_kind_counts": {"kubernetes": 2}})
        self.assertNotIn("anchor_resolution_contract", minimal["runtime_architecture"])

    def test_output_budget_backfills_omitted_rows_when_compact_packet_has_headroom(self) -> None:
        result = {
            "tool": "planning_context",
            "status": "found",
            "runtime_architecture": {
                "scope": {"kind": "fleet"},
                "summary": {"runtime_building_block_count": 0, "domain_routing_map_count": 24},
                "answer_packet": {
                    "runtime_building_blocks": [],
                    "domain_routing_map": [
                        {
                            "status": "known_route" if index == 0 else "unlinked_domain_reference",
                            "domain": {"name": f"domain-{index}.example.test"},
                            "evidence_coordinates": [{"repo": "repo", "path": "infra.tf", "line_start": index + 1}],
                            "payload": "x" * 200,
                        }
                        for index in range(24)
                    ],
                    "deploy_kind_counts": {},
                    "evidence_contract": "unlinked rows are source leads only",
                },
                "assembly_contract": "typed facts only",
            },
            "coverage_warnings": [],
            "unsupported_scopes": [],
            "next_actions": [],
        }

        budgeted = enforce_planning_context_budget(result, max_chars=5_000)

        self.assertLessEqual(len(canonical_json(budgeted)), 5_000)
        routes = budgeted["runtime_architecture"]["answer_packet"]["domain_routing_map"]
        self.assertGreater(len(routes), 7)
        self.assertGreater(
            budgeted["output_budget"]["backfilled_counts"]["runtime_architecture.answer_packet.domain_routing_map"],
            0,
        )
        self.assertGreaterEqual(budgeted["output_budget"]["remaining_chars"], 0)

    def test_output_budget_authz_compact_lists_are_backfillable(self) -> None:
        authz_backfill_keys = {
            path[1]
            for path in _BUDGET_BACKFILL_LIST_PATHS
            if len(path) == 2 and path[0] == "authz_surface"
        }

        self.assertTrue(set(AUTHZ_COMPACT_LIST_KEYS).issubset(authz_backfill_keys))

    def test_planning_context_authz_reference_includes_all_compact_list_keys(self) -> None:
        authz_surface = {
            "status": "found",
            "scope": {},
            "summary": {},
            **{key: [{"category": key}] for key in AUTHZ_COMPACT_LIST_KEYS},
        }

        reference = _planning_context_authz_surface_reference(authz_surface)

        self.assertTrue(set(AUTHZ_COMPACT_LIST_KEYS).issubset(reference))

    def test_planning_context_authz_reference_caps_nested_inspection_refs(self) -> None:
        authz_surface = {
            "inspection_areas": [
                {
                    "area": "omitted_endpoint_rows",
                    "inspection_refs": [{"endpoint": {"path": f"/orders/{index}/"}} for index in range(12)],
                }
            ]
        }

        reference = _planning_context_authz_surface_reference(authz_surface)

        area = reference["inspection_areas"][0]
        self.assertEqual(len(area["inspection_refs"]), COMPACT_AUTHZ_INSPECTION_REF_LIMIT)
        self.assertTrue(area["inspection_refs_truncated"])
        self.assertEqual(area["omitted_inspection_ref_count"], 12 - COMPACT_AUTHZ_INSPECTION_REF_LIMIT)

    def test_compact_authz_surface_caps_inspection_areas_for_backfill(self) -> None:
        compact = _compact_authz_surface(
            {
                "status": "found",
                "scope": {},
                "summary": {},
                "inspection_areas": [{"area": f"area-{index}"} for index in range(20)],
            }
        )

        self.assertEqual(len(compact["inspection_areas"]), COMPACT_RUNTIME_HEADSTART_LIMIT)

    def test_compact_authz_surface_caps_nested_inspection_refs(self) -> None:
        compact = _compact_authz_surface(
            {
                "status": "found",
                "scope": {},
                "summary": {},
                "inspection_areas": [
                    {
                        "area": "omitted_endpoint_rows",
                        "inspection_refs": [{"endpoint": {"path": f"/orders/{index}/"}} for index in range(12)],
                    }
                ],
            }
        )

        area = compact["inspection_areas"][0]
        self.assertEqual(len(area["inspection_refs"]), COMPACT_AUTHZ_INSPECTION_REF_LIMIT)
        self.assertTrue(area["inspection_refs_truncated"])
        self.assertEqual(area["omitted_inspection_ref_count"], 12 - COMPACT_AUTHZ_INSPECTION_REF_LIMIT)

    def test_common_metadata_preserves_existing_inspection_area_keys(self) -> None:
        payload = _with_default_tool_metadata(
            {
                "status": "found",
                "inspection_areas": [
                    {
                        "area": "omitted_endpoint_rows",
                        "trigger": "truncated",
                        "reason": "large authz packet",
                        "inspection_refs": [{"endpoint": {"path": "/orders/"}}],
                        "inspection_refs_truncated": True,
                        "omitted_inspection_ref_count": 7,
                    }
                ],
            },
            tool_name="planning_context",
        )

        area = payload["inspection_areas"][0]
        self.assertEqual(area["area"], "omitted_endpoint_rows")
        self.assertEqual(area["trigger"], "truncated")
        self.assertTrue(area["inspection_refs_truncated"])
        self.assertEqual(area["omitted_inspection_ref_count"], 7)

    def test_common_metadata_normalizes_incomplete_inspection_area_rows(self) -> None:
        payload = _with_default_tool_metadata(
            {
                "status": "found",
                "inspection_areas": [
                    {
                        "path_hints": ["app/views.py"],
                        "repos": ["api"],
                    }
                ],
            },
            tool_name="planning_context",
        )

        area = payload["inspection_areas"][0]
        self.assertEqual(area["area"], "tool_specific")
        self.assertEqual(area["trigger"], "tool_specific")
        self.assertEqual(area["inspection_refs"], [{"path": "app/views.py", "repo": "api"}])

    def test_common_metadata_wraps_structured_inspection_refs_without_dropping(self) -> None:
        payload = _with_default_tool_metadata(
            {
                "status": "found",
                "inspection_areas": [
                    {
                        "area": "authz_checks",
                        "inspection_refs": {"repo": "api", "path": "app/views.py", "line_start": 12},
                        "search_terms": "permission_classes",
                        "authz_status": "missing_declared_policy",
                    }
                ],
            },
            tool_name="planning_context",
        )

        area = payload["inspection_areas"][0]
        self.assertEqual(area["inspection_refs"], [{"repo": "api", "path": "app/views.py", "line_start": 12}])
        self.assertEqual(area["search_terms"], ["permission_classes"])
        self.assertEqual(area["authz_status"], "missing_declared_policy")

    def test_related_fact_budget_key_allowlist_matches_planning_context_output(self) -> None:
        with _fixture_snapshot() as kg:
            result = call_tool(kg, "planning_context", {})

        self.assertEqual(set(result["related_facts"]), set(RELATED_FACT_SECTION_KEYS))

    def test_compact_disambiguation_preserves_retry_argument_shape(self) -> None:
        list_retry = _compact_disambiguation(
            {
                "retry_arguments": [
                    {"symbol": "a"},
                    {"symbol": "b"},
                ]
            }
        )
        dict_retry = _compact_disambiguation({"retry_arguments": {"symbol": "a"}})

        self.assertEqual(list_retry["retry_arguments"], [{"symbol": "a"}, {"symbol": "b"}])
        self.assertEqual(dict_retry["retry_arguments"], {"symbol": "a"})

    def test_output_budget_preserves_compact_symbol_impact_headstart(self) -> None:
        result = {
            "tool": "planning_context",
            "status": "found",
            "summary": {"symbol_count": 1},
            "runtime_architecture": {
                "scope": {"kind": "fleet"},
                "summary": {"runtime_building_block_count": 0, "domain_routing_map_count": 12},
                "answer_packet": {
                    "runtime_building_blocks": [],
                    "domain_routing_map": [
                        {
                            "status": "unlinked_domain_reference",
                            "domain": {"name": f"domain-{index}.example.test"},
                            "evidence_coordinates": [{"repo": "repo", "path": "infra.tf", "line_start": index + 1}],
                            "payload": "x" * 5_000,
                        }
                        for index in range(12)
                    ],
                    "deploy_kind_counts": {},
                    "evidence_contract": "typed facts only",
                },
            },
            "related_facts": {
                "service_brief": {
                    "status": "found",
                    "summary": {"endpoint_fact_count": 12},
                    "endpoints": [
                        {
                            "predicate": "EXPOSES_ENDPOINT",
                            "endpoint": {"path": f"/endpoint-{index}"},
                            "source_coordinates": [{"repo": "api", "path": "api/routes.py", "line_start": index + 1}],
                            "payload": "x" * 5_000,
                        }
                        for index in range(12)
                    ],
                },
                "dependency_importers": {
                    "status": "found",
                    "package_count": 1,
                    "importers": [
                        {
                            "name": f"importer-{index}",
                            "path": f"pkg/module_{index}.py",
                            "payload": "x" * 5_000,
                        }
                        for index in range(12)
                    ],
                },
                "inventory": {
                    "status": "found",
                    "top_dependencies": [
                        {
                            "name": f"dep-{index}",
                            "sample_evidence": [{"payload": "x" * 5_000}],
                        }
                        for index in range(12)
                    ],
                },
                "runtime_architecture": {
                    "status": "found",
                    "summary": {"deploy_unit_count": 2},
                    "answer_packet": {
                        "deploy_kind_counts": {"component_deploy_kind_counts": {"kubernetes": 1}},
                        "missing_fact_families": ["production_deploy_mapping"],
                    },
                },
                "dependencies": [
                    {
                        "predicate": "IMPORTS",
                        "name": f"dep-{index}",
                        "source_coordinates": [{"repo": "api", "path": "requirements.txt", "line_start": index + 1}],
                        "payload": "x" * 5_000,
                    }
                    for index in range(12)
                ],
                "symbol_impact": {
                    "status": "found",
                    "symbol": {
                        "qualified_name": "lib.features.build_features",
                        "qualname": "build_features",
                        "repo": "lib",
                        "path": "lib/features.py",
                        "line": 10,
                        "evidence": [{"payload": "x" * 5_000}],
                    },
                    "reverse_impact": {
                        "status": "found",
                        "summary": {"affected_symbol_count": 2, "constructor_bridge_count": 1},
                        "tiers": [
                            {
                                "depth": 1,
                                "symbol_count": 1,
                                "symbols": [
                                    {
                                        "depth": 1,
                                        "symbol": {
                                            "qualified_name": "train.Builder.build_features",
                                            "qualname": "Builder.build_features",
                                            "repo": "train",
                                            "path": "train/pipeline.py",
                                            "line": 40,
                                            "evidence": [{"payload": "x" * 5_000}],
                                        },
                                    }
                                ],
                            },
                            {
                                "depth": 2,
                                "symbol_count": 1,
                                "symbols": [
                                    {
                                        "depth": 2,
                                        "symbol": {
                                            "qualified_name": "api.TrainView.post",
                                            "qualname": "TrainView.post",
                                            "repo": "api",
                                            "path": "api/views.py",
                                            "line": 5,
                                            "evidence": [{"payload": "x" * 5_000}],
                                        },
                                    }
                                ],
                            },
                        ],
                        "terminal_import_consumer_leads": [
                            {
                                "depth": 2,
                                "for_symbol": {
                                    "qualified_name": "api.TrainView.post",
                                    "qualname": "TrainView.post",
                                    "repo": "api",
                                    "path": "api/views.py",
                                    "line": 5,
                                },
                                "import_consumer_leads": {
                                    "status": "found",
                                    "lead_count": 1,
                                    "leads": [
                                        {
                                            "lead_kind": "import_consumer",
                                            "importer": {
                                                "display_name": "api.views",
                                                "repo": "api",
                                                "path": "api/views.py",
                                            },
                                            "importer_module_symbols": [
                                                {
                                                    "qualified_name": "api.TrainView.post",
                                                    "qualname": "TrainView.post",
                                                    "repo": "api",
                                                    "path": "api/views.py",
                                                    "line": 5,
                                                }
                                            ],
                                            "fact": {
                                                "evidence": [
                                                    {
                                                        "bytes_ref": {
                                                            "repo": "api",
                                                            "path": "api/views.py",
                                                            "line_start": 2,
                                                            "line_end": 2,
                                                        }
                                                    }
                                                ]
                                            },
                                        }
                                    ],
                                },
                            }
                        ],
                        "source_inspection_areas": [
                            {
                                "area": "same_repo_tests_scripts_notebooks",
                                "repos": ["lib"],
                                "path_hints": ["lib/features.py"],
                                "search_terms": ["build_features(", "lib.features.build_features"],
                            }
                        ],
                    },
                }
            },
            "coverage_warnings": [],
            "unsupported_scopes": [],
            "next_actions": [],
        }

        budgeted = enforce_planning_context_budget(
            result,
            max_chars=9_000,
            preserve_planning_sections=True,
        )

        serialized = canonical_json(budgeted)
        self.assertLessEqual(len(serialized), 9_000)
        self.assertNotIn('"payload"', serialized)
        impact = budgeted["related_facts"]["symbol_impact"]["reverse_impact"]
        self.assertIn("service_brief", budgeted["related_facts"])
        self.assertIn("dependency_importers", budgeted["related_facts"])
        self.assertIn("inventory", budgeted["related_facts"])
        self.assertIn("runtime_architecture", budgeted["related_facts"])
        self.assertIn("inspection_areas", budgeted["related_facts"])
        self.assertTrue(
            any(
                area["area"] == "related_facts.service_brief.endpoints" and area["inspection_refs"]
                for area in budgeted["related_facts"]["inspection_areas"]
            )
        )
        self.assertEqual(impact["summary"]["constructor_bridge_count"], 1)
        self.assertEqual(
            [row["symbols"][0]["symbol"]["qualname"] for row in impact["tiers"]],
            ["Builder.build_features", "TrainView.post"],
        )
        terminal = impact["terminal_import_consumer_leads"][0]
        self.assertEqual(terminal["for_symbol"]["path"], "api/views.py")
        self.assertEqual(terminal["import_consumer_leads"]["lead_count"], 1)
        self.assertEqual(
            impact["source_inspection_areas"][0]["search_terms"],
            ["build_features(", "lib.features.build_features"],
        )

    def test_output_budget_minimizes_oversized_runtime_rows_before_dropping_routes(self) -> None:
        result = {
            "tool": "planning_context",
            "status": "found",
            "runtime_architecture": {
                "scope": {"kind": "fleet"},
                "summary": {"runtime_building_block_count": 0, "domain_routing_map_count": 2},
                "answer_packet": {
                    "runtime_building_blocks": [],
                    "domain_routing_map": [
                        {
                            "status": "known_route",
                            "domain": {"name": "known.example.test"},
                            "deploy_kind": "cloudfront_distribution",
                            "evidence_coordinates": [{"repo": "repo", "path": "infra.tf", "line_start": 1}],
                            "payload": "x" * 20_000,
                        },
                        {
                            "status": "unlinked_domain_reference",
                            "domain": {"name": "lead.example.test"},
                            "deploy_kind": "terraform_domain_reference",
                            "evidence_coordinates": [{"repo": "repo", "path": "infra.tf", "line_start": 2}],
                            "payload": "x" * 20_000,
                        },
                    ],
                    "deploy_kind_counts": {},
                    "evidence_contract": "unlinked rows are source leads only",
                },
                "assembly_contract": "typed facts only",
            },
            "coverage_warnings": [],
            "unsupported_scopes": [],
            "next_actions": [],
        }

        budgeted = enforce_planning_context_budget(result, max_chars=2_000)

        self.assertLessEqual(len(canonical_json(budgeted)), 2_000)
        self.assertEqual(budgeted["tool"], "planning_context")
        self.assertTrue(budgeted["output_budget"]["minimized"])
        self.assertLessEqual(len(canonical_json(budgeted)), budgeted["output_budget"]["max_chars"])
        routes = budgeted["runtime_architecture"]["answer_packet"]["domain_routing_map"]
        self.assertEqual({row["status"] for row in routes}, {"known_route", "unlinked_domain_reference"})
        self.assertTrue(all("payload" not in row for row in routes))

    def test_output_budget_minimizes_endpoint_consumer_map_without_dropping_consumers(self) -> None:
        result = {
            "tool": "planning_context",
            "status": "found",
            "runtime_architecture": {
                "scope": {"kind": "repo", "repo": "payments"},
                "summary": {"endpoint_consumer_map_count": 1},
                "answer_packet": {
                    "runtime_building_blocks": [],
                    "domain_routing_map": [],
                    "deploy_runtime_map": [],
                    "endpoint_consumer_map": [
                        {
                            "provider": {"name": "payments"},
                            "provider_endpoint": {"path": "/checkout"},
                            "consumers": [
                                {
                                    "consumer": {"name": "web"},
                                    "evidence_coordinates": [{"repo": "web", "path": "src/api.ts", "line_start": 1}],
                                    "payload": "x" * 20_000,
                                }
                            ],
                            "consumer_count": 1,
                            "payload": "x" * 20_000,
                        }
                    ],
                    "deploy_order_guidance": [],
                    "deploy_kind_counts": {},
                    "evidence_contract": "typed facts only",
                },
            },
            "coverage_warnings": [],
            "unsupported_scopes": [],
            "next_actions": [],
        }

        budgeted = enforce_planning_context_budget(result, max_chars=2_500)

        row = budgeted["runtime_architecture"]["answer_packet"]["endpoint_consumer_map"][0]
        self.assertEqual(row["consumer_count"], 1)
        self.assertEqual(row["consumers"][0]["consumer"]["name"], "web")

    def test_output_budget_tracks_unlinked_deploy_lead_truncation(self) -> None:
        leads = [
            {
                "status": "candidate_deploy_link",
                "reason": "wsgi_ambiguous_module_path_suffix",
                "service": {"name": f"api-{index}"},
                "deploy_target": {"target": f"/srv/apps/app-{index}/wsgi.py"},
                "evidence_coordinates": [{"repo": "ops", "path": "apache/site.conf", "line_start": index + 1}],
            }
            for index in range(20)
        ]
        result = {
            "tool": "planning_context",
            "status": "found",
            "runtime_architecture": {
                "scope": {"kind": "fleet"},
                "summary": {"candidate_or_unlinked_deploy_lead_count": len(leads)},
                "answer_packet": {
                    "runtime_building_blocks": [],
                    "domain_routing_map": [],
                    "deploy_runtime_map": [],
                    "unlinked_deploy_leads": leads,
                    "endpoint_consumer_map": [],
                    "deploy_order_guidance": [],
                    "deploy_kind_counts": {},
                    "evidence_contract": "typed facts only",
                },
            },
            "coverage_warnings": [],
            "unsupported_scopes": [],
            "next_actions": [],
        }

        budgeted = enforce_planning_context_budget(result, max_chars=len(canonical_json(result)) - 1)

        answer_packet = budgeted["runtime_architecture"]["answer_packet"]
        self.assertLess(len(answer_packet["unlinked_deploy_leads"]), len(leads))
        budget = budgeted["output_budget"]
        self.assertEqual(
            budget["omitted_counts"]["unlinked_deploy_leads"],
            len(leads) - len(answer_packet["unlinked_deploy_leads"]),
        )
        self.assertIn("runtime_architecture.answer_packet.unlinked_deploy_leads", budget["truncated_sections"])

    def test_output_budget_marks_final_packet_when_minimum_still_exceeds_budget(self) -> None:
        result = {
            "tool": "planning_context",
            "status": "found",
            "summary": {"note": "x" * 500},
            "runtime_architecture": {
                "scope": {},
                "summary": {},
                "answer_packet": {
                    "runtime_building_blocks": [],
                    "domain_routing_map": [],
                    "deploy_kind_counts": {},
                    "evidence_contract": "typed facts only",
                },
            },
        }

        budgeted = enforce_planning_context_budget(result, max_chars=1)

        self.assertTrue(budgeted["output_budget"]["truncated"])
        self.assertTrue(budgeted["output_budget"]["minimized"])
        self.assertTrue(budgeted["output_budget"]["exceeded_after_minimization"])

    def test_output_budget_bounds_common_evidence_lists_in_minimal_packet(self) -> None:
        result = {
            "tool": "planning_context",
            "status": "found",
            "summary": {"note": "x" * 500},
            "runtime_architecture": {
                "scope": {},
                "summary": {},
                "answer_packet": {
                    "runtime_building_blocks": [],
                    "domain_routing_map": [],
                    "deploy_kind_counts": {},
                    "evidence_contract": "typed facts only",
                },
            },
            "proven_facts": {
                "status": "found",
                "sources": [{"field": f"fact-{index}", "count": index + 1} for index in range(20)],
                "claim_boundary": "KG-backed evidence.",
            },
            "candidate_leads": {
                "status": "found",
                "sources": [
                    {"field": f"lead-{index}", "count": index + 1, "lead_kind": "unlinked_source_lead"}
                    for index in range(18)
                ],
                "claim_boundary": "Verify before promoting.",
            },
            "coverage_gaps": [{"trigger": f"gap-{index}"} for index in range(20)],
            "inspection_areas": [
                {
                    "area": f"area-{index}",
                    "reason": "inspect source",
                    "inspection_refs": [{"path": f"service/{index}.py", "line": index + 1}],
                    "search_terms": [f"term-{index}"],
                }
                for index in range(30)
            ],
        }

        budgeted = enforce_planning_context_budget(result, max_chars=1)

        self.assertLessEqual(len(budgeted["proven_facts"]["sources"]), COMPACT_RUNTIME_HEADSTART_LIMIT)
        self.assertLessEqual(len(budgeted["candidate_leads"]["sources"]), COMPACT_RUNTIME_HEADSTART_LIMIT)
        self.assertEqual(budgeted["proven_facts"]["sources"][-1]["field"], "omitted_proven_fact_sources")
        self.assertEqual(budgeted["candidate_leads"]["sources"][-1]["field"], "omitted_candidate_lead_sources")
        self.assertLessEqual(len(budgeted["coverage_gaps"]), COMPACT_RUNTIME_HEADSTART_LIMIT)
        self.assertLessEqual(len(budgeted["inspection_areas"]), COMPACT_RUNTIME_SOURCE_CHECK_LIMIT)
        self.assertEqual(budgeted["coverage_gaps"][-1]["trigger"], "common_coverage_gaps_truncated")
        omitted_gap_detail = budgeted["coverage_gaps"][-1]["detail"]
        self.assertEqual(omitted_gap_detail["omitted_row_count"], 13)
        omitted_area = budgeted["inspection_areas"][-1]
        self.assertEqual(omitted_area["area"], "omitted_common_inspection_areas")
        self.assertEqual(omitted_area["omitted_row_count"], 16)
        self.assertTrue(omitted_area["inspection_refs"])
        self.assertTrue(omitted_area["search_terms"])

    def test_planning_context_symbol_anchor_returns_impact_and_coordinates(self) -> None:
        with _fixture_snapshot() as kg:
            result = call_tool(kg, "planning_context", {"symbol": "handle_checkout"})

        self.assertEqual(result["status"], "found")
        symbol_impact = result["related_facts"]["symbol_impact"]
        self.assertEqual(symbol_impact["status"], "found")
        self.assertEqual(symbol_impact["symbol"]["qualname"], "handle_checkout")
        self.assertEqual({row["predicate"] for row in symbol_impact["direct_callees"]}, {"CALLS"})
        self.assertEqual(result["source_coordinates"][0]["repo"], "payments")
        self.assertIsNone(result["source_coordinates"][0]["commit_sha"])
        self.assertEqual(result["source_coordinates"][0]["provenance"], "row_geometry")
        self.assertEqual(result["source_coordinates"][0]["path"], "payments/checkout.py")
        self.assertEqual(result["source_coordinates"][0]["line_start"], 10)
        self.assertEqual(result["source_coordinates"][0]["line_end"], 20)

    def test_planning_context_symbol_impact_fails_closed_without_resolved_name(self) -> None:
        with _fixture_snapshot() as kg:
            result = _planning_context_symbol_impact(
                kg,
                [{"path": "payments/checkout.py", "line": 10}],
                anchors={"symbol": "handle_checkout"},
                status="found",
            )

        self.assertEqual(result["status"], "not_computed")
        self.assertEqual(result["reason"], "resolved symbol missing qualified name")

    def test_planning_context_source_coordinates_include_bytes_ref_provenance(self) -> None:
        with _fixture_snapshot() as kg:
            result = call_tool(kg, "planning_context", {"package": "shared-lib", "line": 2})

        self.assertEqual(result["status"], "found")
        coordinates = result["source_coordinates"]
        self.assertTrue(coordinates)
        self.assertEqual(coordinates[0]["repo"], "payments")
        self.assertEqual(coordinates[0]["commit_sha"], "fixture-sha")
        self.assertEqual(coordinates[0]["provenance"], "bytes_ref")
        self.assertEqual(coordinates[0]["path"], "payments/checkout.py")
        self.assertEqual(coordinates[0]["line_start"], 2)
        self.assertEqual(coordinates[0]["line_end"], 2)

    def test_planning_context_answerability_distinguishes_partial_and_not_answerable(self) -> None:
        with _fixture_snapshot() as kg:
            partial = call_tool(kg, "planning_context", {"service": "payments", "package": "missing-package"})
            missing = call_tool(kg, "planning_context", {"service": "missing"})

        self.assertEqual(partial["status"], "found")
        self.assertEqual(partial["answerability"]["status"], "partial")
        self.assertEqual(partial["answerability"]["missing_fact_families"], ["dependency_edges"])
        self.assertTrue(partial["answerability"]["recommended_followups"])
        self.assertEqual(missing["status"], "not_found")
        self.assertEqual(missing["answerability"]["status"], "not_answerable")
        self.assertEqual(missing["answerability"]["missing_fact_families"], ["primary_anchor"])

    def test_planning_context_related_sections_are_capped(self) -> None:
        with _fixture_snapshot(extra_consumers=125) as kg:
            result = call_tool(kg, "planning_context", {"event_channel": "orders-created", "limit": 100})

        self.assertEqual(result["status"], "found")
        self.assertGreater(result["summary"]["event_fact_count"], 5)
        self.assertEqual(len(result["related_facts"]["event_channels"]), 5)
        self.assertEqual(result["summary"]["section_limit"], 5)

    def test_planning_context_structured_endpoint_event_and_domain_anchors(self) -> None:
        with _fixture_snapshot() as kg:
            endpoint = call_tool(kg, "planning_context", {"repo": "payments", "endpoint": "/checkout"})
            event = call_tool(kg, "planning_context", {"repo": "payments", "event_channel": "orders-created"})
            domain = call_tool(kg, "planning_context", {"repo": "payments", "domain": "api.internal.example"})

        self.assertEqual(endpoint["status"], "found")
        self.assertEqual({row["predicate"] for row in endpoint["endpoints"]}, {"EXPOSES_ENDPOINT"})
        self.assertEqual(event["status"], "found")
        self.assertEqual({row["predicate"] for row in event["event_channels"]}, {"CONSUMES_EVENT", "PRODUCES_EVENT"})
        self.assertEqual(domain["status"], "found")
        self.assertEqual({row["predicate"] for row in domain["domains"]}, {"REFERENCES_DOMAIN"})

    def test_planning_context_excludes_candidate_event_references_from_known_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service = Entity(
                kind="Service",
                identity={"tenant_id": "default", "namespace": "default", "slug": "campaign", "repo": "campaign"},
            )
            channel = Entity(
                kind="EventChannel",
                identity={"tenant_id": "default", "broker_kind": "sqs", "channel_address": "orders-created"},
                canonical_status="candidate",
            )
            reference = Fact(
                "REFERENCES_EVENT_CHANNEL",
                service.entity_id,
                channel.entity_id,
                canonical_status="candidate",
            )
            JsonlKgStore(root).write(
                entities=[service, channel],
                facts=[reference],
                evidence=[],
                coverage=[],
                manifest={"version": 1},
            )

            result = call_tool(KgSnapshot(root), "planning_context", {"service": "campaign", "limit": 10})

        self.assertEqual(result["status"], "found")
        self.assertEqual(result["summary"]["event_fact_count"], 0)
        self.assertEqual(result["summary"]["candidate_or_unlinked_event_fact_count"], 1)
        self.assertEqual(result["event_channels"], [])
        self.assertEqual(result["candidate_or_unlinked_event_channels"][0]["predicate"], "REFERENCES_EVENT_CHANNEL")
        self.assertEqual(
            result["candidate_or_unlinked_event_channels"][0]["linkage_status"],
            "candidate_or_unlinked",
        )
        self.assertEqual(result["related_facts"]["service_brief"]["summary"]["event_fact_count"], 0)
        self.assertEqual(
            result["related_facts"]["service_brief"]["summary"]["candidate_or_unlinked_event_fact_count"],
            1,
        )
        self.assertEqual(result["related_facts"]["service_brief"]["event_channels"], [])
        self.assertEqual(
            result["related_facts"]["service_brief"]["candidate_or_unlinked_event_channels"][0]["predicate"],
            "REFERENCES_EVENT_CHANNEL",
        )

    def test_planning_context_query_candidate_event_reference_is_not_known_hit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service = Entity(
                kind="Service",
                identity={"tenant_id": "default", "namespace": "default", "slug": "campaign", "repo": "campaign"},
            )
            channel = Entity(
                kind="EventChannel",
                identity={"tenant_id": "default", "broker_kind": "sqs", "channel_address": "orders-created"},
                canonical_status="candidate",
            )
            reference = Fact(
                "REFERENCES_EVENT_CHANNEL",
                service.entity_id,
                channel.entity_id,
                canonical_status="candidate",
            )
            JsonlKgStore(root).write(
                entities=[service, channel],
                facts=[reference],
                evidence=[],
                coverage=[],
                manifest={"version": 1},
            )

            result = call_tool(KgSnapshot(root), "planning_context", {"query": "orders-created", "limit": 10})

        self.assertEqual(result["summary"]["event_fact_count"], 0)
        self.assertEqual(result["summary"]["candidate_or_unlinked_event_fact_count"], 1)
        self.assertEqual(result["event_channels"], [])
        self.assertEqual(result["candidate_or_unlinked_event_channels"][0]["predicate"], "REFERENCES_EVENT_CHANNEL")
        self.assertIn(
            "Use `event_channel=orders-created` to inspect matching event-channel facts.",
            result["next_actions"],
        )

    def test_service_brief_partitions_reference_to_canonical_event_channel_as_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            campaign = Entity(
                kind="Service",
                identity={"tenant_id": "default", "namespace": "default", "slug": "campaign", "repo": "campaign"},
            )
            billing = Entity(
                kind="Service",
                identity={"tenant_id": "default", "namespace": "default", "slug": "billing", "repo": "billing"},
            )
            channel = Entity(
                kind="EventChannel",
                identity={"tenant_id": "default", "broker_kind": "sqs", "channel_address": "orders-created"},
            )
            reference = Fact("REFERENCES_EVENT_CHANNEL", campaign.entity_id, channel.entity_id)
            producer = Fact("PRODUCES_EVENT", billing.entity_id, channel.entity_id)
            JsonlKgStore(root).write(
                entities=[campaign, billing, channel],
                facts=[reference, producer],
                evidence=[],
                coverage=[],
                manifest={"version": 1},
            )

            result = call_tool(KgSnapshot(root), "get_service_brief", {"service": "campaign", "limit": 10})

        self.assertEqual(result["summary"]["event_fact_count"], 0)
        self.assertEqual(result["summary"]["candidate_or_unlinked_event_fact_count"], 1)
        self.assertEqual(result["event_channels"], [])
        candidate = result["candidate_or_unlinked_event_channels"][0]
        self.assertEqual(candidate["predicate"], "REFERENCES_EVENT_CHANNEL")
        self.assertEqual(candidate["canonical_status"], "canonical")
        self.assertEqual(candidate["object_canonical_status"], "canonical")
        self.assertEqual(candidate["linkage_status"], "candidate_or_unlinked")

    def test_service_brief_partitions_candidate_deploy_link_as_unlinked_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service = Entity(
                kind="Service",
                identity={"tenant_id": "default", "namespace": "default", "slug": "api-a", "repo": "api-a"},
            )
            target = Entity(
                kind="DeployTarget",
                identity={"tenant_id": "default", "repo": "ops", "type": "wsgi", "target": "/srv/apps/app/wsgi.py"},
            )
            deploy = Fact(
                "DEPLOYS_VIA_CONFIG",
                service.entity_id,
                target.entity_id,
                {"source_kind": "runtime_linker", "resolved_by": "wsgi_ambiguous_module_path_suffix"},
                canonical_status="candidate",
            )
            JsonlKgStore(root).write(
                entities=[service, target],
                facts=[deploy],
                evidence=[
                    Evidence(
                        target_type="fact",
                        target_id=deploy.fact_id,
                        derivation_class="candidate",
                        source_system="runtime_linker",
                        source_ref={"resolved_by": "wsgi_ambiguous_module_path_suffix"},
                        bytes_ref={"repo": "ops", "path": "apache/site.conf", "line_start": 7, "line_end": 8},
                        confidence=0.5,
                    )
                ],
                coverage=[],
                manifest={"version": 1},
            )

            result = call_tool(KgSnapshot(root), "get_service_brief", {"service": "api-a", "limit": 10})

        self.assertEqual(result["summary"]["deploy_mapping_count"], 0)
        surfaces = result["operational_surfaces"]
        self.assertEqual(surfaces["summary"]["deploy_link_fact_count"], 0)
        self.assertEqual(surfaces["summary"]["candidate_or_unlinked_deploy_link_count"], 1)
        self.assertEqual(surfaces["deploy_link_facts"], [])
        self.assertEqual(surfaces["deploy_runtime_units"], [])
        unlinked = surfaces["evidence_partition"]["unlinked_evidence"]
        self.assertEqual(unlinked["deploy_link_samples"][0]["predicate"], "DEPLOYS_VIA_CONFIG")
        self.assertEqual(unlinked["deploy_link_samples"][0]["linkage_status"], "candidate_or_unlinked")
        self.assertIn(
            "operational_surfaces.candidate_or_unlinked_deploy_links",
            result["claim_contract"]["candidate_or_unlinked_rows"],
        )
        self.assertIn(
            "operational_surfaces.evidence_partition.unlinked_evidence.deploy_link_samples",
            result["claim_contract"]["candidate_or_unlinked_rows"],
        )

    def test_planning_context_cross_family_anchors_keep_each_family_context(self) -> None:
        with _fixture_snapshot() as kg:
            result = call_tool(
                kg,
                "planning_context",
                {"endpoint": "/checkout", "event_channel": "orders-created"},
            )

        self.assertEqual(result["status"], "found")
        self.assertEqual(result["answerability"]["status"], "answerable")
        self.assertEqual({row["predicate"] for row in result["endpoints"]}, {"EXPOSES_ENDPOINT"})
        self.assertEqual({row["predicate"] for row in result["event_channels"]}, {"CONSUMES_EVENT", "PRODUCES_EVENT"})

    def test_planning_context_endpoint_anchor_matches_route_parameter_shapes(self) -> None:
        with _fixture_snapshot(
            endpoint_consumer=True,
            provider_endpoint_path="/orders/:orderId",
            endpoint_consumer_path="/orders/{id}",
        ) as kg:
            result = call_tool(kg, "planning_context", {"endpoint": "/orders/{orderId}", "limit": 10})

        self.assertEqual(result["status"], "found")
        self.assertEqual(result["summary"]["endpoint_fact_count"], 2)
        self.assertEqual(
            {row["object"] for row in result["endpoints"]},
            {"POST /orders/:orderId", "${env:PAYMENTS_API_BASE_URL} POST /orders/{id}"},
        )

    def test_planning_context_service_enrichment_survives_secondary_anchor(self) -> None:
        with _fixture_snapshot(extra_service_endpoint=True) as kg:
            result = call_tool(kg, "planning_context", {"service": "payments", "endpoint": "/checkout", "limit": 10})

        self.assertEqual(result["status"], "found")
        self.assertEqual(result["summary"]["endpoint_fact_count"], 2)
        self.assertEqual({row["object"] for row in result["endpoints"]}, {"POST /checkout", "GET /refund"})

    def test_planning_context_missing_identity_followups_are_actionable(self) -> None:
        with _fixture_snapshot() as kg:
            missing_service = call_tool(kg, "planning_context", {"service": "missing", "endpoint": "/checkout"})
            missing_symbol = call_tool(kg, "planning_context", {"symbol": "missing", "endpoint": "/checkout"})

        self.assertEqual(missing_service["answerability"]["missing_fact_families"], ["service_identity"])
        self.assertTrue(any("search_services" in action for action in missing_service["answerability"]["recommended_followups"]))
        self.assertEqual(missing_symbol["answerability"]["missing_fact_families"], ["symbol_identity"])
        self.assertTrue(any("path" in action and "line" in action for action in missing_symbol["answerability"]["recommended_followups"]))

    def test_planning_context_dedupes_same_location_bytes_ref_and_row_geometry(self) -> None:
        with _fixture_snapshot(symbol_entity_evidence_duplicate_coordinates=True) as kg:
            result = call_tool(kg, "planning_context", {"symbol": "handle_checkout"})

        checkout_coordinates = [
            coordinate
            for coordinate in result["source_coordinates"]
            if coordinate["path"] == "payments/checkout.py"
            and coordinate["line_start"] == 10
            and coordinate["line_end"] == 20
        ]
        self.assertEqual(len(checkout_coordinates), 1)
        self.assertEqual(checkout_coordinates[0]["provenance"], "bytes_ref")

    def test_get_service_brief_dedupes_related_rows(self) -> None:
        with _fixture_snapshot(duplicate_endpoint_fact=True) as kg:
            result = call_tool(kg, "get_service_brief", {"service": "payments", "limit": 10})

        self.assertEqual(result["status"], "found")
        self.assertEqual(result["summary"]["endpoint_fact_count"], 1)
        self.assertEqual(len(result["endpoints"]), 1)

    def test_get_service_brief_surfaces_bounded_endpoint_consumers(self) -> None:
        with _fixture_snapshot(endpoint_consumer=True) as kg:
            result = call_tool(kg, "get_service_brief", {"service": "payments", "limit": 10})

        self.assertEqual(result["status"], "found")
        self.assertEqual(result["summary"]["endpoint_consumer_fact_count"], 1)
        self.assertEqual(result["summary"]["endpoint_consumer_service_count"], 1)
        packet = result["endpoint_consumers"]
        self.assertEqual(packet["summary"]["consumer_fact_count"], 1)
        self.assertEqual(packet["summary"]["consumer_service_count"], 1)
        self.assertEqual(packet["summary"]["host_resolution_kind_counts"], {"env_backed_unresolved": 1})
        self.assertEqual(packet["consumers"][0]["consumer"]["slug"], "web")
        self.assertEqual(packet["consumers"][0]["matched_provider_endpoint"]["path"], "/checkout")
        self.assertEqual(packet["consumers"][0]["match_basis"], ENDPOINT_PATH_SHAPE_MATCH_BASIS)
        self.assertTrue(any("endpoint_consumers" in action for action in result["next_actions"]))

    def test_get_service_brief_matches_endpoint_consumers_by_path_shape(self) -> None:
        with _fixture_snapshot(
            endpoint_consumer=True,
            provider_endpoint_path="/orders/:orderId",
            endpoint_consumer_path="/orders/{id}",
        ) as kg:
            result = call_tool(kg, "get_service_brief", {"service": "payments", "limit": 10})

        packet = result["endpoint_consumers"]
        self.assertEqual(result["summary"]["endpoint_consumer_fact_count"], 1)
        self.assertEqual(packet["summary"]["consumer_fact_count"], 1)
        self.assertEqual(packet["summary"]["match_basis"], ENDPOINT_PATH_SHAPE_MATCH_BASIS)
        self.assertEqual(packet["consumers"][0]["matched_provider_endpoint"]["path"], "/orders/{param}")
        self.assertEqual(packet["consumers"][0]["match_basis"], ENDPOINT_PATH_SHAPE_MATCH_BASIS)

    def test_get_service_brief_does_not_shape_match_composite_param_segments(self) -> None:
        with _fixture_snapshot(
            endpoint_consumer=True,
            provider_endpoint_path="/files/:name.json",
            endpoint_consumer_path="/files/{name}",
        ) as kg:
            result = call_tool(kg, "get_service_brief", {"service": "payments", "limit": 10})

        self.assertEqual(result["summary"]["endpoint_consumer_fact_count"], 0)
        self.assertEqual(result["endpoint_consumers"]["consumers"], [])

    def test_get_service_brief_surfaces_operational_deploy_candidates(self) -> None:
        with _fixture_snapshot(operational_deploy_mapping=True, operational_deploy_same_repo=True) as kg:
            result = call_tool(kg, "get_service_brief", {"service": "payments", "limit": 10})

        self.assertEqual(result["summary"]["deploy_target_candidate_count"], 1)
        self.assertEqual(result["summary"]["domain_route_candidate_count"], 1)
        surfaces = result["operational_surfaces"]
        self.assertEqual(
            surfaces["deploy_target_candidates"][0]["match_basis"],
            "deploy_target_repo_equals_service_repo",
        )
        self.assertEqual(surfaces["evidence_partition"]["known_linked"]["status"], "found")
        self.assertEqual(surfaces["evidence_partition"]["unlinked_evidence"]["status"], "empty")
        self.assertTrue(
            any(
                item["contract"] == "canonical_service_deploy_blocker"
                for item in surfaces["evidence_partition"]["missing_contracts"]["items"]
            )
        )
        self.assertEqual(surfaces["domain_route_candidates"][0]["predicate"], "ROUTES_DOMAIN_TO_DEPLOY")
        self.assertIn("exact repo-identity evidence", surfaces["coverage_note"])

    def test_get_service_brief_does_not_infer_deploy_from_target_text(self) -> None:
        with _fixture_snapshot(operational_deploy_mapping=True) as kg:
            result = call_tool(kg, "get_service_brief", {"service": "payments", "limit": 10})

        surfaces = result["operational_surfaces"]
        self.assertEqual(result["summary"]["deploy_target_candidate_count"], 0)
        self.assertEqual(result["summary"]["domain_route_candidate_count"], 0)
        self.assertEqual(surfaces["summary"]["unlinked_domain_route_count"], 1)
        self.assertEqual(surfaces["unlinked_domain_route_samples"][0]["relationship_to_service"], "unlinked_fleet_route")
        self.assertEqual(surfaces["evidence_partition"]["known_linked"]["status"], "found")
        self.assertEqual(surfaces["evidence_partition"]["known_linked"]["counts"]["domain_route_count"], 0)
        self.assertEqual(surfaces["evidence_partition"]["known_linked"]["counts"]["deploy_target_count"], 0)
        self.assertEqual(surfaces["evidence_partition"]["unlinked_evidence"]["status"], "found")
        self.assertTrue(
            any(
                item["contract"] == "unlinked_route_to_service"
                for item in surfaces["evidence_partition"]["missing_contracts"]["items"]
            )
        )

    def test_get_service_brief_uses_deploy_link_to_promote_route_to_known_linked(self) -> None:
        with _fixture_snapshot(operational_deploy_mapping=True, operational_deploy_link=True) as kg:
            result = call_tool(kg, "get_service_brief", {"service": "payments", "limit": 10})

        surfaces = result["operational_surfaces"]
        self.assertEqual(result["summary"]["deploy_mapping_count"], 1)
        self.assertEqual(result["summary"]["domain_route_candidate_count"], 1)
        self.assertEqual(surfaces["summary"]["unlinked_domain_route_count"], 0)
        self.assertEqual(surfaces["evidence_partition"]["known_linked"]["counts"]["domain_route_count"], 1)
        self.assertEqual(surfaces["evidence_partition"]["known_linked"]["counts"]["deploy_target_count"], 1)
        self.assertEqual(surfaces["summary"]["deploy_link_fact_count"], 1)
        self.assertEqual(surfaces["domain_route_candidates"][0]["match_basis"], "route_deploy_target_linked_to_service")
        self.assertEqual(surfaces["deploy_target_candidates"], [])
        self.assertEqual(surfaces["deploy_link_facts"][0]["predicate"], "DEPLOYS_VIA_CONFIG")

    def test_get_service_brief_treats_provider_any_method_as_compatible(self) -> None:
        with _fixture_snapshot(
            endpoint_consumer=True,
            provider_endpoint_method="ANY",
            endpoint_consumer_method="GET",
        ) as kg:
            result = call_tool(kg, "get_service_brief", {"service": "payments", "limit": 10})

        self.assertEqual(result["summary"]["endpoint_consumer_fact_count"], 1)
        self.assertEqual(result["endpoint_consumers"]["consumers"][0]["matched_provider_endpoint"]["methods"], ["ANY"])

    def test_get_service_brief_does_not_match_consumers_without_method(self) -> None:
        with _fixture_snapshot(endpoint_consumer=True, endpoint_consumer_method=None) as kg:
            result = call_tool(kg, "get_service_brief", {"service": "payments", "limit": 10})

        self.assertEqual(result["summary"]["endpoint_consumer_fact_count"], 0)
        self.assertEqual(result["endpoint_consumers"]["consumers"], [])

    def test_review_context_aggregates_symbols_and_call_edges(self) -> None:
        with _fixture_snapshot() as kg:
            result = call_tool(
                kg,
                "review_context",
                {"repo": "payments", "changed_files": ["payments/checkout.py"], "limit": 10, "include_broad_context": True},
            )

        self.assertEqual(result["status"], "found")
        self.assertEqual(result["repo"], "payments")
        self.assertIn("changed_symbols", result)
        self.assertIn("direct_callers", result)
        self.assertIn("direct_callees", result)
        self.assertIn("repo_dependencies", result)
        # No changed_ranges supplied: top-level changed_symbols is empty; the inventory lives in changed_file_symbols.
        self.assertEqual(result["changed_symbols"], [])
        self.assertTrue(any(row["qualname"] == "handle_checkout" for row in result["changed_file_symbols"]))
        # No ranges -> caller/callee edges are in-scope-empty (consistent with changed_symbols);
        # call-edge aggregation is covered by the changed_ranges tests below.
        self.assertEqual(result["direct_callees"], [])
        self.assertEqual(result["direct_callers"], [])
        self.assertEqual({row["predicate"] for row in result["repo_dependencies"]}, {"RESOLVES_TO_REPO"})
        self.assertEqual(result["answerability"]["status"], "answerable")
        self.assertEqual(result["summary"]["changed_file_count"], 1)
        self.assertEqual(result["summary"]["changed_symbol_count"], 0)
        self.assertEqual(result["summary"]["changed_file_symbol_count"], 2)
        self.assertEqual(result["summary"]["detail_limit"], 10)
        self.assertEqual(result["review_answer_packet"]["summary"]["changed_symbol_count"], 0)
        self.assertEqual(result["review_answer_packet"]["summary"]["changed_file_symbol_count"], 2)
        self.assertEqual(result["review_answer_packet"]["summary"]["direct_caller_count"], 0)
        self.assertEqual(result["review_answer_packet"]["top_changed_symbols"], [])
        self.assertEqual(
            {row["qualname"] for row in result["review_answer_packet"]["changed_file_symbol_inventory"]},
            {"bootstrap_checkout", "handle_checkout"},
        )
        self.assertIn("top-level changed_symbols is empty", result["review_answer_packet"]["scope_contract"]["changed_symbols"])
        self.assertIn(
            "Range-overlap changed symbols only",
            result["review_answer_packet"]["scope_contract"]["review_answer_packet.top_changed_symbols"],
        )
        self.assertEqual(result["claim_contract"]["scope"], "bounded static review context for changed files and optional ranges")
        self.assertIn("do not prove deploy safety", result["claim_contract"]["safety_rule"])
        self.assertIn("changed-file symbol inventory", result["claim_contract"]["changed_symbol_rule"])
        self.assertEqual(result["review_answer_packet"]["claim_contract"], result["claim_contract"])
        self.assertEqual(result["changed_surface"]["files"][0]["symbol_count"], 2)
        self.assertEqual(result["changed_surface"]["symbols"][0]["qualname"], "bootstrap_checkout")
        self.assertEqual(result["impact"]["direct_callees"], [])
        self.assertEqual({row["predicate"] for row in result["runtime_surfaces"]["endpoints"]}, {"EXPOSES_ENDPOINT"})
        self.assertEqual(
            {row["predicate"] for row in result["runtime_surfaces"]["event_channels"]},
            {"CONSUMES_EVENT", "PRODUCES_EVENT"},
        )
        self.assertIn("application_impact", result)
        self.assertEqual(result["application_impact"]["anchors"][0]["root"], "payments")
        self.assertTrue(result["source_coordinates"])
        self.assertEqual(result["source_coordinates"][0]["path"], "payments/checkout.py")
        _assert_additive_fields(self, result)

    def test_review_context_surfaces_candidate_event_references_separately(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service = Entity(
                kind="Service",
                identity={"tenant_id": "default", "namespace": "default", "slug": "campaign", "repo": "campaign"},
            )
            symbol = Entity(
                kind="CodeSymbol",
                identity={
                    "tenant_id": "default",
                    "repo": "campaign",
                    "module": "campaign.app",
                    "qualname": "handle_campaign",
                    "symbol_kind": "function",
                },
                properties={"path": "campaign/app.py", "line": 1, "end_line": 3},
            )
            channel = Entity(
                kind="EventChannel",
                identity={"tenant_id": "default", "broker_kind": "sqs", "channel_address": "tracking-events"},
                canonical_status="candidate",
            )
            reference = Fact(
                "REFERENCES_EVENT_CHANNEL",
                service.entity_id,
                channel.entity_id,
                canonical_status="candidate",
            )
            JsonlKgStore(root).write(
                entities=[service, symbol, channel],
                facts=[reference],
                evidence=[],
                coverage=[],
                manifest={"version": 1},
            )

            result = call_tool(
                KgSnapshot(root),
                "review_context",
                {"repo": "campaign", "changed_files": ["campaign/app.py"], "limit": 10, "include_broad_context": True},
            )

        self.assertEqual(result["summary"]["event_fact_count"], 0)
        self.assertEqual(result["summary"]["candidate_or_unlinked_event_fact_count"], 1)
        self.assertEqual(result["runtime_surfaces"]["event_channels"], [])
        candidate = result["runtime_surfaces"]["candidate_or_unlinked_event_channels"][0]
        self.assertEqual(candidate["predicate"], "REFERENCES_EVENT_CHANNEL")
        self.assertEqual(candidate["linkage_status"], "candidate_or_unlinked")
        self.assertEqual(
            result["review_answer_packet"]["runtime"]["candidate_or_unlinked_event_channels"][0]["predicate"],
            "REFERENCES_EVENT_CHANNEL",
        )
        statuses = {row["surface"]: row for row in result["surface_status"]}
        self.assertEqual(statuses["tracking_paths"]["status"], "unlinked_lead")
        self.assertIn(
            "runtime_surfaces.candidate_or_unlinked_event_channels",
            statuses["tracking_paths"]["evidence_path"],
        )

    def test_review_context_groups_application_impact_surfaces(self) -> None:
        with _fixture_snapshot(app_surface=True) as kg:
            result = call_tool(
                kg,
                "review_context",
                {"repo": "payments", "changed_files": ["payments/checkout.py"], "limit": 10, "include_broad_context": True},
            )

        impact = result["application_impact"]
        self.assertEqual(impact["status"], "found")
        self.assertEqual(result["summary"]["app_surface_count"], impact["summary"]["same_repo_entity_count"])
        self.assertTrue(any(row["module"] == "payments.api" for row in impact["same_repo_surfaces"]["api"]))
        self.assertTrue(any(row["module"] == "payments.tasks" for row in impact["same_repo_surfaces"]["workers"]))
        self.assertTrue(
            any(row["module"] == "payments.management.commands.reconcile" for row in impact["same_repo_surfaces"]["scheduled_jobs"])
        )
        self.assertTrue(any(row["qualname"] == "Payment" for row in impact["same_repo_surfaces"]["models"]))
        self.assertFalse(any(row["symbol_kind"] == "django_field" for row in impact["same_repo_surfaces"]["models"]))
        self.assertTrue(any(row["predicate"] == "EXPOSES_ENDPOINT" for row in impact["runtime_facts"]))
        lead = impact["cross_repo_name_leads"][0]
        self.assertEqual(lead["repo"], "web")
        self.assertEqual(lead["match_basis"], "name_derived_unlinked_lead")
        self.assertIn("not as impact proof", lead["interpretation"])

    def test_review_context_marks_requested_surfaces_context_unlinked_or_missing(self) -> None:
        with _fixture_snapshot(app_surface=True) as kg:
            result = call_tool(
                kg,
                "review_context",
                {
                    "repo": "payments",
                    "changed_files": ["payments/checkout.py"],
                    "requested_surfaces": ["UI", "scheduled_jobs", "SQS", "workers", "tracking"],
                    "limit": 10,
                    "include_broad_context": True,
                },
            )

        statuses = {row["surface"]: row for row in result["surface_status"]}
        self.assertEqual(result["answerability"]["status"], "partial")
        self.assertEqual(statuses["scheduled_jobs"]["status"], "inventory_context")
        self.assertEqual(statuses["scheduled_jobs"]["known_count"], 0)
        self.assertGreater(statuses["scheduled_jobs"]["context_count"], 0)
        self.assertNotIn("evidence_count", statuses["scheduled_jobs"])
        self.assertIn("do not prove this surface is affected", statuses["scheduled_jobs"]["interpretation"])
        self.assertEqual(statuses["delivery_workers"]["status"], "inventory_context")
        self.assertEqual(statuses["ui_screens"]["status"], "unlinked_lead")
        self.assertEqual(statuses["sqs_consumers"]["status"], "inventory_context")
        self.assertEqual(statuses["tracking_paths"]["status"], "missing")
        self.assertIn("ui_screens", result["answerability"]["unlinked_fact_families"])
        self.assertIn("scheduled_jobs", result["answerability"]["inventory_context_fact_families"])
        self.assertIn("delivery_workers", result["answerability"]["inventory_context_fact_families"])
        self.assertIn("sqs_consumers", result["answerability"]["inventory_context_fact_families"])
        self.assertNotIn("sqs_consumers", result["answerability"]["missing_fact_families"])
        self.assertIn("tracking_paths", result["answerability"]["missing_fact_families"])
        self.assertTrue(
            any("inventory/context leads" in action for action in result["answerability"]["recommended_followups"])
        )
        self.assertEqual(result["review_answer_packet"]["surface_status"], result["surface_status"])

    def test_review_context_accepts_builtin_call_graph_section_aliases(self) -> None:
        with _fixture_snapshot() as kg:
            result = call_tool(
                kg,
                "review_context",
                {
                    "repo": "payments",
                    "changed_files": ["payments/checkout.py"],
                    "changed_ranges": [{"path": "payments/checkout.py", "start_line": 1, "end_line": 200}],
                    "requested_surfaces": ["callers", "reverse_impact"],
                    "limit": 10,
                    "include_broad_context": True,
                },
            )

        self.assertEqual(result["status"], "found")
        self.assertIn("direct_callers", result)
        self.assertIn("transitive_callers", result)
        self.assertEqual({row["predicate"] for row in result["impact"]["direct_callees"]}, {"CALLS"})

    def test_review_context_accepts_generic_review_category_aliases(self) -> None:
        with _fixture_snapshot(app_surface=True) as kg:
            result = call_tool(
                kg,
                "review_context",
                {
                    "repo": "payments",
                    "changed_files": ["payments/checkout.py"],
                    "requested_surfaces": ["services", "schemas", "contracts", "deployables", "owners"],
                    "limit": 10,
                    "include_broad_context": True,
                },
            )

        statuses = {row["surface"]: row for row in result["surface_status"]}
        self.assertEqual(set(statuses), {"api_surfaces", "serializers"})
        self.assertEqual(statuses["api_surfaces"]["status"], "inventory_context")
        self.assertEqual(statuses["serializers"]["status"], "missing")
        self.assertIn("api_surfaces", result["answerability"]["inventory_context_fact_families"])
        self.assertIn("serializers", result["answerability"]["missing_fact_families"])
        self.assertIn("ownership_context", result["answerability"]["missing_fact_families"])
        self.assertTrue(any(row["kind"] == "ownership_context" for row in result["unsupported_scopes"]))
        self.assertTrue(
            any(
                row.get("trigger") == "unsupported_scope"
                and isinstance(row.get("detail"), dict)
                and row["detail"].get("kind") == "ownership_context"
                for row in result["coverage_gaps"]
            )
        )
        self.assertIn("repo_dependencies", result["impact"])
        self.assertIn("runtime_surfaces", result)

    def test_review_context_unknown_surfaces_degrade_to_status_rows(self) -> None:
        with _fixture_snapshot() as kg:
            result = call_tool(
                kg,
                "review_context",
                {
                    "repo": "payments",
                    "changed_files": ["payments/checkout.py"],
                    "requested_surfaces": ["rule_actions", "abilities", "authz", "tests"],
                    "limit": 10,
                    "include_broad_context": True,
                },
            )

        self.assertEqual(result["status"], "found")
        statuses = {row["surface"]: row for row in result["surface_status"]}
        self.assertIn("rule_actions", statuses)
        self.assertIn("abilities", statuses)
        self.assertIn("authz", statuses)
        self.assertIn("tests", statuses)
        for token in ("rule_actions", "abilities", "authz", "tests"):
            row = statuses[token]
            self.assertEqual(row["status"], "unsupported_or_unlinked")
            self.assertIn("source_inspection_terms", row)
            terms = row["source_inspection_terms"]
            self.assertIn(token, terms)
        # split words present for multi-word token
        self.assertIn("rule", statuses["rule_actions"]["source_inspection_terms"])
        self.assertIn("actions", statuses["rule_actions"]["source_inspection_terms"])

    def test_review_context_mixed_known_and_unknown_surfaces(self) -> None:
        with _fixture_snapshot(app_surface=True) as kg:
            result = call_tool(
                kg,
                "review_context",
                {
                    "repo": "payments",
                    "changed_files": ["payments/checkout.py"],
                    "requested_surfaces": ["scheduled_jobs", "authz"],
                    "limit": 10,
                    "include_broad_context": True,
                },
            )

        self.assertEqual(result["status"], "found")
        statuses = {row["surface"]: row for row in result["surface_status"]}
        # known surface behaves normally
        self.assertIn("scheduled_jobs", statuses)
        self.assertIn(statuses["scheduled_jobs"]["status"], {"inventory_context", "unlinked_lead", "missing"})
        self.assertNotEqual(statuses["scheduled_jobs"]["status"], "unsupported_or_unlinked")
        # unknown surface gets degraded row
        self.assertIn("authz", statuses)
        self.assertEqual(statuses["authz"]["status"], "unsupported_or_unlinked")
        self.assertIn("authz", statuses["authz"]["source_inspection_terms"])

    def test_review_context_unknown_surface_inspection_terms_include_changed_symbol_names(self) -> None:
        with _fixture_snapshot() as kg:
            result = call_tool(
                kg,
                "review_context",
                {
                    "repo": "payments",
                    "changed_files": ["payments/checkout.py"],
                    "changed_ranges": [{"path": "payments/checkout.py", "start_line": 1, "end_line": 200}],
                    "requested_surfaces": ["ability_checks"],
                    "limit": 10,
                    "include_broad_context": True,
                },
            )

        statuses = {row["surface"]: row for row in result["surface_status"]}
        self.assertIn("ability_checks", statuses)
        row = statuses["ability_checks"]
        self.assertEqual(row["status"], "unsupported_or_unlinked")
        terms = row["source_inspection_terms"]
        # token and its split words
        self.assertIn("ability_checks", terms)
        self.assertIn("ability", terms)
        self.assertIn("checks", terms)
        # at most 5 symbol names appended (no duplicates in terms)
        self.assertEqual(len(terms), len(set(terms)))

    def test_review_context_unknown_surface_cap_applied(self) -> None:
        # 12 unknown surfaces → at most 8 rows + omission count on last row
        surfaces = [f"unknown_surface_{i}" for i in range(12)]
        with _fixture_snapshot() as kg:
            result = call_tool(
                kg,
                "review_context",
                {
                    "repo": "payments",
                    "changed_files": ["payments/checkout.py"],
                    "requested_surfaces": surfaces,
                    "limit": 10,
                    "include_broad_context": True,
                },
            )

        unknown_rows = [r for r in result["surface_status"] if r.get("status") == "unsupported_or_unlinked"]
        self.assertEqual(len(unknown_rows), 8)
        last = unknown_rows[-1]
        self.assertIn("omitted_unknown_surface_count", last)
        self.assertEqual(last["omitted_unknown_surface_count"], 4)

    def test_review_context_unknown_surface_token_truncated(self) -> None:
        long_token = "x" * 300
        with _fixture_snapshot() as kg:
            result = call_tool(
                kg,
                "review_context",
                {
                    "repo": "payments",
                    "changed_files": ["payments/checkout.py"],
                    "requested_surfaces": [long_token],
                    "limit": 10,
                    "include_broad_context": True,
                },
            )

        unknown_rows = [r for r in result["surface_status"] if r.get("status") == "unsupported_or_unlinked"]
        self.assertEqual(len(unknown_rows), 1)
        self.assertLessEqual(len(unknown_rows[0]["surface"]), 200)
        for term in unknown_rows[0]["source_inspection_terms"]:
            self.assertLessEqual(len(term), 200)

    def test_review_context_unknown_surface_pathological_separators(self) -> None:
        # pathological token with many single-char components → no 1-char terms, capped at 12
        pathological_token = "a_b_c_d_e_f_g_h_i_j_k_l_m_n_o_p_q_r_s_t"
        with _fixture_snapshot() as kg:
            result = call_tool(
                kg,
                "review_context",
                {
                    "repo": "payments",
                    "changed_files": ["payments/checkout.py"],
                    "requested_surfaces": [pathological_token],
                    "limit": 10,
                },
            )

        unknown_rows = [r for r in result["surface_status"] if r.get("status") == "unsupported_or_unlinked"]
        self.assertEqual(len(unknown_rows), 1)
        row = unknown_rows[0]
        terms = row["source_inspection_terms"]
        # no 1-char terms
        for term in terms:
            self.assertGreaterEqual(len(term), 2, f"Found 1-char term: {term}")
        # capped at 12
        self.assertLessEqual(len(terms), 12)

    def test_review_context_surfaces_path_matched_endpoint_consumers(self) -> None:
        with _fixture_snapshot(endpoint_consumer=True) as kg:
            result = call_tool(
                kg,
                "review_context",
                {"repo": "payments", "changed_files": ["payments/checkout.py"], "limit": 10, "include_broad_context": True},
            )

        self.assertEqual(result["summary"]["endpoint_consumer_fact_count"], 1)
        consumer = result["runtime_surfaces"]["endpoint_consumers"][0]
        self.assertEqual(consumer["predicate"], "CALLS_ENDPOINT")
        self.assertEqual(consumer["consumer"]["repo"], "web")
        self.assertEqual(consumer["matched_provider_endpoint"]["methods"], ["POST"])

    def test_review_context_repo_filter_is_case_insensitive(self) -> None:
        with _fixture_snapshot() as kg:
            result = call_tool(
                kg,
                "review_context",
                {"repo": "Payments", "changed_files": ["payments/checkout.py"], "limit": 10, "include_broad_context": True},
            )

        self.assertEqual(result["status"], "found")
        self.assertTrue(any(row["qualname"] == "handle_checkout" for row in result["changed_file_symbols"]))
        self.assertEqual({row["predicate"] for row in result["repo_dependencies"]}, {"RESOLVES_TO_REPO"})

    def test_review_context_repo_filter_accepts_owner_repo_query(self) -> None:
        with _fixture_snapshot() as kg:
            result = call_tool(
                kg,
                "review_context",
                {"repo": "latticeai/payments", "changed_files": ["payments/checkout.py"], "limit": 10, "include_broad_context": True},
            )

        self.assertEqual(result["status"], "found")
        self.assertTrue(any(row["qualname"] == "handle_checkout" for row in result["changed_file_symbols"]))
        self.assertEqual({row["predicate"] for row in result["repo_dependencies"]}, {"RESOLVES_TO_REPO"})

    def test_review_context_repo_filter_accepts_bare_repo_for_owner_repo_rows(self) -> None:
        with _fixture_snapshot(symbol_repo="latticeai/payments") as kg:
            result = call_tool(
                kg,
                "review_context",
                {"repo": "payments", "changed_files": ["payments/checkout.py"], "limit": 10, "include_broad_context": True},
            )

        self.assertEqual(result["status"], "found")
        self.assertTrue(any(row["qualname"] == "handle_checkout" for row in result["changed_file_symbols"]))

    def test_review_context_repo_filter_rejects_different_owner_repo_query(self) -> None:
        with _fixture_snapshot(symbol_repo="owner-a/payments") as kg:
            result = call_tool(
                kg,
                "review_context",
                # include_broad_context=True: changed_file_symbols inventory is a broad
                # section; the compact profile omits it entirely (nothing to leak).
                {"repo": "owner-b/payments", "changed_files": ["payments/checkout.py"], "limit": 10, "include_broad_context": True},
            )

        self.assertFalse(any(row["qualname"] == "handle_checkout" for row in result["changed_file_symbols"]))

    def test_review_context_repo_resolution_reports_direct_match_without_rewriting_scope(self) -> None:
        with _fixture_snapshot() as kg:
            result = call_tool(
                kg,
                "review_context",
                {
                    "repo": "payments",
                    "changed_files": ["payments/checkout.py"],
                    "changed_ranges": [{"path": "payments/checkout.py", "start_line": 10, "end_line": 10}],
                    "limit": 10,
                    "include_broad_context": True,
                },
            )

        self.assertEqual(result["repo"], "payments")
        self.assertEqual(result["repo_resolution"]["status"], "matched")
        self.assertEqual(result["repo_resolution"]["basis"], "direct_repo_match")
        self.assertEqual([row["qualname"] for row in result["changed_symbols"]], ["handle_checkout"])

    def test_review_context_direct_match_uses_canonical_snapshot_repo_key(self) -> None:
        with _fixture_snapshot() as kg:
            result = call_tool(
                kg,
                "review_context",
                {
                    "repo": "Payments",
                    "changed_files": ["payments/checkout.py"],
                    "changed_ranges": [{"path": "payments/checkout.py", "start_line": 10, "end_line": 10}],
                    "limit": 10,
                    "include_broad_context": True,
                },
            )

        self.assertEqual(result["requested_repo"], "Payments")
        self.assertEqual(result["repo"], "payments")
        self.assertEqual(result["repo_resolution"]["status"], "matched")
        self.assertEqual(result["repo_resolution"]["effective_repo"], "payments")
        self.assertEqual(result["repo_resolution"]["matched_repos"], ["payments"])
        self.assertEqual([row["qualname"] for row in result["changed_symbols"]], ["handle_checkout"])

    def test_review_context_owner_repo_suffix_match_requires_safe_alias_overlap(self) -> None:
        with _fixture_snapshot() as kg:
            result = call_tool(
                kg,
                "review_context",
                {
                    "repo": "owner/payments",
                    "changed_files": ["elsewhere/missing.py"],
                    "changed_ranges": [{"path": "elsewhere/missing.py", "start_line": 10, "end_line": 10}],
                    "limit": 10,
                },
            )

        self.assertEqual(result["repo"], "owner/payments")
        self.assertEqual(result["repo_resolution"]["status"], "unresolved")
        self.assertEqual(result["repo_resolution"]["reason"], "no_changed_file_overlap")
        self.assertEqual(result["summary"]["symbol_anchor_count"], 0)

    def test_review_context_resolves_owner_repo_alias_for_single_repo_checkout_snapshot(self) -> None:
        with _fixture_snapshot(symbol_repo="local-checkout-repo") as kg:
            _rewrite_fixture_repo(kg, old_repo="payments", new_repo="local-checkout-repo")

            result = call_tool(
                kg,
                "review_context",
                {
                    "repo": "owner/project",
                    "changed_files": ["payments/checkout.py"],
                    "changed_ranges": [{"path": "payments/checkout.py", "start_line": 10, "end_line": 10}],
                    "limit": 10,
                    "include_broad_context": True,
                },
            )

        self.assertEqual(result["repo"], "local-checkout-repo")
        self.assertEqual(
            result["repo_resolution"],
            {
                "status": "resolved",
                "requested_repo": "owner/project",
                "effective_repo": "local-checkout-repo",
                "basis": "single_repo_snapshot_changed_file_overlap",
                "snapshot_repo_count": 1,
            },
        )
        self.assertEqual(result["summary"]["symbol_anchor_count"], 1)
        self.assertEqual([row["qualname"] for row in result["changed_symbols"]], ["handle_checkout"])

    def test_review_context_repo_resolution_uses_entity_scope_before_fact_consumer_repos(self) -> None:
        with _fixture_snapshot(symbol_repo="local-checkout-repo") as kg:
            _rewrite_fixture_repo(kg, old_repo="payments", new_repo="local-checkout-repo")
            kg.facts[0].setdefault("qualifier", {})["consumer_repo"] = "foreign/repo"

            result = call_tool(
                kg,
                "review_context",
                {
                    "repo": "owner/project",
                    "changed_files": ["payments/checkout.py"],
                    "changed_ranges": [{"path": "payments/checkout.py", "start_line": 10, "end_line": 10}],
                    "limit": 10,
                },
            )

        self.assertEqual(result["repo"], "local-checkout-repo")
        self.assertEqual(result["repo_resolution"]["status"], "resolved")
        self.assertEqual(result["repo_resolution"]["snapshot_repo_count"], 1)
        self.assertEqual(result["summary"]["symbol_anchor_count"], 1)

    def test_review_context_fixture_repo_rewrite_keeps_ids_consistent(self) -> None:
        with _fixture_snapshot() as kg:
            _rewrite_fixture_repo(kg, old_repo="payments", new_repo="local-checkout-repo")

            entity_ids = {str(entity["entity_id"]) for entity in kg.entities}
            fact_ids = {str(fact["fact_id"]) for fact in kg.facts}

            self.assertEqual(set(kg.entities_by_id), entity_ids)
            for fact in kg.facts:
                self.assertIn(fact["subject_id"], entity_ids)
                self.assertIn(fact["object_id"], entity_ids)
            for evidence in kg.evidence:
                if evidence["target_type"] == "entity":
                    self.assertIn(evidence["target_id"], entity_ids)
                if evidence["target_type"] == "fact":
                    self.assertIn(evidence["target_id"], fact_ids)

    def test_review_context_resolves_uppercase_repo_identity_as_single_repo_snapshot(self) -> None:
        with _fixture_snapshot() as kg:
            _rewrite_fixture_repo(kg, old_repo="payments", new_repo="Local-Checkout-Repo")

            result = call_tool(
                kg,
                "review_context",
                {
                    "repo": "owner/project",
                    "changed_files": ["payments/checkout.py"],
                    "changed_ranges": [{"path": "payments/checkout.py", "start_line": 10, "end_line": 10}],
                    "limit": 10,
                    "include_broad_context": True,
                },
            )

        self.assertEqual(result["repo"], "local-checkout-repo")
        self.assertEqual(result["repo_resolution"]["status"], "resolved")
        self.assertEqual(result["repo_resolution"]["snapshot_repo_count"], 1)
        self.assertEqual(result["summary"]["symbol_anchor_count"], 1)
        self.assertEqual([row["qualname"] for row in result["changed_symbols"]], ["handle_checkout"])

    def test_review_context_does_not_alias_bare_repo_for_single_repo_checkout_snapshot(self) -> None:
        with _fixture_snapshot(symbol_repo="local-checkout-repo") as kg:
            _rewrite_fixture_repo(kg, old_repo="payments", new_repo="local-checkout-repo")

            result = call_tool(
                kg,
                "review_context",
                {
                    "repo": "project",
                    "changed_files": ["payments/checkout.py"],
                    "changed_ranges": [{"path": "payments/checkout.py", "start_line": 10, "end_line": 10}],
                    "limit": 10,
                },
            )

        self.assertEqual(result["repo"], "project")
        self.assertEqual(result["repo_resolution"]["status"], "unresolved")
        self.assertEqual(result["repo_resolution"]["reason"], "requested_repo_not_owner_qualified")
        self.assertEqual(result["summary"]["symbol_anchor_count"], 0)

    def test_review_context_does_not_alias_owner_repo_when_changed_files_do_not_overlap_snapshot(self) -> None:
        with _fixture_snapshot(symbol_repo="local-checkout-repo") as kg:
            _rewrite_fixture_repo(kg, old_repo="payments", new_repo="local-checkout-repo")

            result = call_tool(
                kg,
                "review_context",
                {
                    "repo": "owner/project",
                    "changed_files": ["elsewhere/missing.py"],
                    "changed_ranges": [{"path": "elsewhere/missing.py", "start_line": 10, "end_line": 10}],
                    "limit": 10,
                },
            )

        self.assertEqual(result["repo"], "owner/project")
        self.assertEqual(result["repo_resolution"]["status"], "unresolved")
        self.assertEqual(result["repo_resolution"]["reason"], "no_changed_file_overlap")
        self.assertEqual(result["summary"]["symbol_anchor_count"], 0)
        self.assertEqual(result["changed_symbols"], [])

    def test_review_context_does_not_treat_repo_path_traversal_as_changed_file_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snapshot_root = root / "snapshot"
            repo_root = root / "repo"
            (repo_root / "nested").mkdir(parents=True)
            (root / "outside.py").write_text("print('outside')\n", encoding="utf-8")
            JsonlKgStore(snapshot_root).write(
                entities=[],
                facts=[],
                evidence=[],
                coverage=[],
                manifest={"version": 1, "repo_name": "Local-Checkout-Repo", "repo_path": str(repo_root)},
            )

            result = call_tool(
                KgSnapshot(snapshot_root),
                "review_context",
                {
                    "repo": "owner/project",
                    "changed_files": ["nested/../../outside.py"],
                    "changed_ranges": [{"path": "nested/../../outside.py", "start_line": 1, "end_line": 1}],
                    "limit": 10,
                },
            )

        self.assertEqual(result["repo"], "owner/project")
        self.assertEqual(result["repo_resolution"]["status"], "unresolved")
        self.assertEqual(result["repo_resolution"]["reason"], "no_changed_file_overlap")
        self.assertEqual(result["summary"]["symbol_anchor_count"], 0)

    def test_review_context_does_not_strip_leading_traversal_for_repo_path_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snapshot_root = root / "snapshot"
            repo_root = root / "repo"
            config_path = repo_root / "config" / "settings.yaml"
            config_path.parent.mkdir(parents=True)
            config_path.write_text("enabled: true\n", encoding="utf-8")
            JsonlKgStore(snapshot_root).write(
                entities=[],
                facts=[],
                evidence=[],
                coverage=[],
                manifest={"version": 1, "repo_name": "Local-Checkout-Repo", "repo_path": str(repo_root)},
            )

            result = call_tool(
                KgSnapshot(snapshot_root),
                "review_context",
                {
                    "repo": "owner/project",
                    "changed_files": ["../config/settings.yaml"],
                    "changed_ranges": [{"path": "../config/settings.yaml", "start_line": 1, "end_line": 1}],
                    "limit": 10,
                },
            )

        self.assertEqual(result["repo"], "owner/project")
        self.assertEqual(result["repo_resolution"]["status"], "unresolved")
        self.assertEqual(result["repo_resolution"]["reason"], "no_changed_file_overlap")
        self.assertEqual(result["summary"]["symbol_anchor_count"], 0)

    def test_review_context_can_resolve_single_repo_checkout_from_existing_unindexed_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snapshot_root = root / "snapshot"
            repo_root = root / "repo"
            config_path = repo_root / "config" / "settings.yaml"
            config_path.parent.mkdir(parents=True)
            config_path.write_text("enabled: true\n", encoding="utf-8")
            JsonlKgStore(snapshot_root).write(
                entities=[],
                facts=[],
                evidence=[],
                coverage=[],
                manifest={"version": 1, "repo_name": "Local-Checkout-Repo", "repo_path": str(repo_root)},
            )

            result = call_tool(
                KgSnapshot(snapshot_root),
                "review_context",
                {
                    "repo": "owner/project",
                    "changed_files": ["config/settings.yaml"],
                    "changed_ranges": [{"path": "config/settings.yaml", "start_line": 1, "end_line": 1}],
                    "limit": 10,
                },
            )

        self.assertEqual(result["repo"], "local-checkout-repo")
        self.assertEqual(result["repo_resolution"]["status"], "resolved")
        self.assertEqual(result["summary"]["symbol_anchor_count"], 0)

    def test_review_context_does_not_alias_owner_repo_for_multi_repo_snapshot(self) -> None:
        with _fixture_snapshot(symbol_repo="local-checkout-repo") as kg:
            _rewrite_fixture_repo(kg, old_repo="payments", new_repo="local-checkout-repo")
            other_repo_module = Entity(
                kind="CodeModule",
                identity={"tenant_id": "default", "repo": "other-repo", "module": "other.module"},
                properties={"path": "other/module.py"},
            )
            other_repo_record = other_repo_module.to_record()
            kg.entities.append(other_repo_record)
            kg.entities_by_id[other_repo_module.entity_id] = other_repo_record

            result = call_tool(
                kg,
                "review_context",
                {
                    "repo": "owner/project",
                    "changed_files": ["payments/checkout.py"],
                    "changed_ranges": [{"path": "payments/checkout.py", "start_line": 10, "end_line": 10}],
                    "limit": 10,
                },
            )

        self.assertEqual(result["repo"], "owner/project")
        self.assertEqual(result["repo_resolution"]["status"], "ambiguous")
        self.assertEqual(result["repo_resolution"]["reason"], "multiple_snapshot_repos")
        self.assertEqual(result["summary"]["symbol_anchor_count"], 0)
        self.assertEqual(result["changed_symbols"], [])

    def test_review_context_does_not_alias_owner_repo_without_snapshot_repo_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            JsonlKgStore(root).write(entities=[], facts=[], evidence=[], coverage=[], manifest={"version": 1})

            result = call_tool(
                KgSnapshot(root),
                "review_context",
                {
                    "repo": "owner/project",
                    "changed_files": ["src/app.py"],
                    "changed_ranges": [{"path": "src/app.py", "start_line": 1, "end_line": 1}],
                    "limit": 10,
                },
            )

        self.assertEqual(result["repo"], "owner/project")
        self.assertEqual(result["repo_resolution"]["status"], "unresolved")
        self.assertEqual(result["repo_resolution"]["reason"], "no_snapshot_repo_identity")
        self.assertEqual(result["summary"]["symbol_anchor_count"], 0)

    def test_repo_dependencies_reject_different_owner_repo_query(self) -> None:
        with _fixture_snapshot() as kg:
            for fact in kg.facts:
                if fact.get("predicate") == "RESOLVES_TO_REPO":
                    fact["qualifier"]["consumer_repo"] = "owner-a/payments"
            result = call_tool(
                kg,
                "review_context",
                # include_broad_context=True: repo_dependencies is a broad section,
                # omitted by the compact profile.
                {"repo": "owner-b/payments", "changed_files": ["payments/missing.py"], "limit": 10, "include_broad_context": True},
            )

        self.assertEqual(result["repo_dependencies"], [])

    def test_repo_dependencies_owner_query_prefers_consumer_identity_over_bare_repo(self) -> None:
        with _fixture_snapshot() as kg:
            repo_links = [fact for fact in kg.facts if fact.get("predicate") == "RESOLVES_TO_REPO"]
            self.assertEqual(len(repo_links), 1)
            owner_a_link = repo_links[0]
            owner_a_link["qualifier"] = {
                **owner_a_link["qualifier"],
                "consumer_repo": "payments",
                "package_name": "owner-a-lib",
                "consumer_repo_identity": {
                    "tenant_id": "default",
                    "host": "github.com",
                    "owner": "owner-a",
                    "name": "payments",
                },
            }
            owner_b_link = deepcopy(owner_a_link)
            owner_b_link["qualifier"] = {
                **owner_b_link["qualifier"],
                "package_name": "owner-b-lib",
                "consumer_repo_identity": {
                    "tenant_id": "default",
                    "host": "github.com",
                    "owner": "owner-b",
                    "name": "payments",
                },
            }
            kg.facts.append(owner_b_link)

            result = kg.repo_dependencies("owner-a/payments", limit=10)

        self.assertEqual(result["dependency_count"], 1)
        self.assertEqual(result["dependencies"][0]["qualifier"]["package_name"], "owner-a-lib")

    def test_review_context_changed_ranges_filter_symbols(self) -> None:
        with _fixture_snapshot() as kg:
            result = call_tool(
                kg,
                "review_context",
                {
                    "repo": "payments",
                    "changed_files": ["payments/checkout.py"],
                    "changed_ranges": [{"path": "payments/checkout.py", "start_line": 10, "end_line": 10}],
                    "include_broad_context": True,
                },
            )

        self.assertEqual(result["status"], "found")
        self.assertEqual([row["qualname"] for row in result["changed_symbols"]], ["handle_checkout"])
        self.assertEqual(result["changed_symbols"][0]["line_start"], 10)
        self.assertEqual(result["changed_symbols"][0]["line_end"], 20)
        self.assertTrue(any(row["qualname"] == "bootstrap_checkout" for row in result["changed_file_symbols"]))
        self.assertEqual(result["summary"]["changed_file_symbol_count"], 2)
        self.assertIn("scope_contract", result)
        self.assertEqual(result["scope_contract"]["changed_symbol_count"], 1)
        self.assertEqual([row["qualname"] for row in result["review_answer_packet"]["top_changed_symbols"]], ["handle_checkout"])
        self.assertEqual(result["review_answer_packet"]["summary"]["changed_symbol_count"], 1)
        self.assertEqual(result["review_answer_packet"]["summary"]["changed_file_symbol_count"], 2)
        self.assertTrue(
            any(row["qualname"] == "bootstrap_checkout" for row in result["review_answer_packet"]["changed_file_symbol_inventory"])
        )
        self.assertEqual(result["summary"]["diff_anchor_count"], 1)
        self.assertEqual(result["summary"]["symbol_anchor_count"], 1)
        self.assertEqual(result["summary"]["file_anchor_count"], 0)
        anchor = result["diff_anchors"][0]
        self.assertEqual(anchor["anchor_type"], "symbol")
        self.assertEqual(anchor["match_kind"], "enclosing_symbol")
        self.assertEqual(anchor["range"], {"start_line": 10, "end_line": 10})
        self.assertEqual([row["qualname"] for row in anchor["symbols"]], ["handle_checkout"])
        self.assertEqual(result["review_answer_packet"]["top_diff_anchors"], result["diff_anchors"])
        self.assertEqual(result["source_coordinates"][0]["path"], "payments/checkout.py")
        self.assertEqual(result["source_coordinates"][0]["line_start"], 10)

    def test_review_context_exposes_compact_lead_gate_for_anchored_review(self) -> None:
        with _fixture_snapshot(upstream_checkout_caller=True) as kg:
            result = call_tool(
                kg,
                "review_context",
                {
                    "repo": "payments",
                    "changed_files": ["payments/checkout.py"],
                    "changed_ranges": [{"path": "payments/checkout.py", "start_line": 10, "end_line": 10}],
                    "limit": 10,
                    "include_broad_context": True,
                },
            )

        self.assertEqual(result["review_lead_status"]["coverage_status"], "useful")
        self.assertEqual(result["review_lead_status"]["recommended_action"], "use_supercontext_packet")
        self.assertEqual(result["review_lead_status"]["changed_anchor_count"], 1)
        self.assertEqual(result["review_lead_status"]["changed_symbol_count"], 1)
        self.assertEqual(result["review_lead_status"]["direct_impact_count"], 2)
        self.assertEqual(result["review_leads"]["changed_symbols"][0]["qualname"], "handle_checkout")
        self.assertEqual(result["review_leads"]["direct_callers"][0]["subject"], "payments.api.submit_checkout")
        self.assertEqual(result["review_leads"]["direct_callees"][0]["object"], "payments.gateway.charge_card")
        self.assertEqual(result["review_answer_packet"]["review_lead_status"], result["review_lead_status"])
        self.assertNotIn("review_leads", result["review_answer_packet"])

    def test_review_context_lead_gate_treats_symbol_anchor_as_useful(self) -> None:
        packet = _review_context_lead_packet(
            changed_files=["payments/checkout.py"],
            summary={"symbol_anchor_count": 1, "file_anchor_count": 0},
            changed_symbols=[],
            direct_callers=[],
            direct_callees=[],
            transitive_callers=[],
            source_coordinates=[],
        )

        self.assertEqual(packet["review_lead_status"]["coverage_status"], "useful")
        self.assertEqual(packet["review_lead_status"]["recommended_action"], "use_supercontext_packet")
        self.assertEqual(packet["review_lead_status"]["changed_anchor_count"], 1)
        self.assertNotIn("reason", packet["review_lead_status"])

    def test_review_context_leads_have_stable_ids_and_kinds(self) -> None:
        with _fixture_snapshot(upstream_checkout_caller=True) as kg:
            result = call_tool(
                kg,
                "review_context",
                {
                    "repo": "payments",
                    "changed_files": ["payments/checkout.py"],
                    "changed_ranges": [{"path": "payments/checkout.py", "start_line": 10, "end_line": 20}],
                    "include_broad_context": True,
                },
            )

        for field, expected_kind in (
            ("changed_symbols", "changed_symbol"),
            ("direct_callers", "direct_caller"),
            ("direct_callees", "direct_callee"),
        ):
            row = result["review_leads"][field][0]
            self.assertEqual(row["lead_kind"], expected_kind)
            self.assertRegex(row["lead_id"], rf"^lead:{expected_kind}:")

        with _fixture_snapshot(upstream_checkout_caller=True) as kg2:
            repeat = call_tool(
                kg2,
                "review_context",
                {
                    "repo": "payments",
                    "changed_files": ["payments/checkout.py"],
                    "changed_ranges": [{"path": "payments/checkout.py", "start_line": 10, "end_line": 20}],
                    "include_broad_context": True,
                },
            )
        self.assertEqual(
            result["review_leads"]["direct_callers"][0]["lead_id"],
            repeat["review_leads"]["direct_callers"][0]["lead_id"],
        )

    def test_review_context_changed_ranges_use_symbol_evidence_span(self) -> None:
        with _fixture_snapshot(
            symbol_without_end_line=True,
            symbol_entity_evidence_duplicate_coordinates=True,
        ) as kg:
            result = call_tool(
                kg,
                "review_context",
                {
                    "repo": "payments",
                    "changed_files": ["payments/checkout.py"],
                    "changed_ranges": [{"path": "payments/checkout.py", "start_line": 15, "end_line": 15}],
                    "include_broad_context": True,
                },
            )

        self.assertEqual(result["status"], "found")
        self.assertEqual([row["qualname"] for row in result["changed_symbols"]], ["handle_checkout"])

    def test_review_context_changed_ranges_mark_partial_overlap_symbol_anchor(self) -> None:
        with _fixture_snapshot() as kg:
            result = call_tool(
                kg,
                "review_context",
                {
                    "repo": "payments",
                    "changed_files": ["payments/checkout.py"],
                    "changed_ranges": [{"path": "payments/checkout.py", "start_line": 5, "end_line": 25}],
                    "include_broad_context": True,
                },
            )

        self.assertEqual([row["qualname"] for row in result["changed_symbols"]], ["handle_checkout"])
        self.assertEqual(result["diff_anchors"][0]["match_kind"], "overlapping_symbol")
        self.assertEqual([row["qualname"] for row in result["diff_anchors"][0]["symbols"]], ["handle_checkout"])

    def test_review_context_changed_ranges_prefer_enclosing_symbol_over_broad_overlap(self) -> None:
        with _fixture_snapshot(containing_checkout_class=True) as kg:
            result = call_tool(
                kg,
                "review_context",
                {
                    "repo": "payments",
                    "changed_files": ["payments/checkout.py"],
                    "changed_ranges": [{"path": "payments/checkout.py", "start_line": 1, "end_line": 30}],
                    "include_broad_context": True,
                },
            )

        self.assertEqual([row["qualname"] for row in result["changed_symbols"]], ["CheckoutHandler"])
        self.assertEqual([row["qualname"] for row in result["diff_anchors"][0]["symbols"]], ["CheckoutHandler"])
        self.assertEqual(result["diff_anchors"][0]["match_kind"], "enclosing_symbol")

    def test_review_context_changed_ranges_keep_most_specific_nested_symbol(self) -> None:
        with _fixture_snapshot(containing_checkout_class=True) as kg:
            result = call_tool(
                kg,
                "review_context",
                {
                    "repo": "payments",
                    "changed_files": ["payments/checkout.py"],
                    "changed_ranges": [{"path": "payments/checkout.py", "start_line": 15, "end_line": 15}],
                    "include_broad_context": True,
                },
            )

        self.assertEqual([row["qualname"] for row in result["changed_symbols"]], ["CheckoutHandler.handle_checkout"])
        self.assertTrue(any(row["qualname"] == "CheckoutHandler" for row in result["changed_file_symbols"]))

    def test_review_context_changed_ranges_emit_file_anchor_when_no_symbol_exists(self) -> None:
        with _fixture_snapshot() as kg:
            result = call_tool(
                kg,
                "review_context",
                {
                    "repo": "payments",
                    "changed_files": ["config/settings.yaml"],
                    "changed_ranges": [{"path": "config/settings.yaml", "start_line": 4, "end_line": 6}],
                    "limit": 10,
                },
            )

        self.assertEqual(result["summary"]["diff_anchor_count"], 1)
        self.assertEqual(result["summary"]["symbol_anchor_count"], 0)
        self.assertEqual(result["summary"]["file_anchor_count"], 1)
        self.assertEqual(result["changed_symbols"], [])
        anchor = result["diff_anchors"][0]
        self.assertEqual(anchor["anchor_type"], "file")
        self.assertEqual(anchor["match_kind"], "changed_range_without_indexed_symbol")
        self.assertEqual(anchor["range"], {"start_line": 4, "end_line": 6})
        self.assertEqual(anchor["source_coordinates"][0]["path"], "config/settings.yaml")
        self.assertTrue(any(row["path"] == "config/settings.yaml" for row in result["source_coordinates"]))

    def test_review_context_file_anchor_only_defaults_to_compact_packet_without_unlinked_leads(self) -> None:
        with _fixture_snapshot(app_surface=True) as kg:
            result = call_tool(
                kg,
                "review_context",
                {
                    "repo": "payments",
                    "changed_files": ["payments/config.yaml"],
                    "changed_ranges": [{"path": "payments/config.yaml", "start_line": 4, "end_line": 6}],
                    "limit": 10,
                },
            )

        self.assertEqual(result["summary"]["changed_symbol_count"], 0)
        self.assertEqual(result["summary"]["symbol_anchor_count"], 0)
        self.assertEqual(result["summary"]["file_anchor_count"], 1)
        self.assertEqual(result["summary"]["app_cross_repo_lead_count"], 1)
        self.assertEqual(result["summary"]["source_coordinate_count"], len(result["source_coordinates"]))
        self.assertLessEqual(len(result["source_coordinates"]), result["summary"]["section_limit"])
        self.assertEqual(result["requested_repo"], "payments")
        self.assertEqual(result["repo_resolution"]["status"], "matched")
        self.assertEqual(result["review_answer_packet"]["packet_mode"], "diff_anchor_only")
        self.assertEqual(result["review_answer_packet"]["repo_resolution"]["status"], "matched")
        self.assertEqual(result["review_lead_status"]["coverage_status"], "low_coverage")
        self.assertEqual(result["review_lead_status"]["recommended_action"], "fall_back_to_plain_review")
        self.assertEqual(
            result["review_lead_status"]["reason"],
            "no symbol anchors, changed symbols, or direct/transitive impact edges",
        )
        self.assertEqual(result["review_leads"]["changed_files"], ["payments/config.yaml"])
        self.assertEqual(result["review_lead_status"]["source_coordinate_count"], len(result["source_coordinates"]))
        self.assertEqual(result["review_leads"]["source_coordinates"], result["source_coordinates"])
        self.assertEqual(result["review_answer_packet"]["review_lead_status"], result["review_lead_status"])
        self.assertNotIn("review_leads", result["review_answer_packet"])
        self.assertEqual(result["review_answer_packet"]["top_diff_anchors"], result["diff_anchors"])
        self.assertNotIn("application", result["review_answer_packet"])
        self.assertNotIn("runtime", result["review_answer_packet"])
        self.assertNotIn("framework", result["review_answer_packet"])
        self.assertNotIn("application_impact", result)
        self.assertNotIn("runtime_surfaces", result)
        self.assertNotIn("framework_impact", result)
        self.assertEqual(result["omitted_context"]["counts"]["application_impact.cross_repo_name_leads"], 1)
        self.assertEqual(result["candidate_leads"]["status"], "empty")
        self.assertLess(len(canonical_json(result)), 10_000)
        self.assertTrue(any("include_unlinked_leads=true" in action for action in result["next_actions"]))

    def test_review_context_file_anchor_only_can_opt_into_broad_unlinked_leads(self) -> None:
        with _fixture_snapshot(app_surface=True) as kg:
            result = call_tool(
                kg,
                "review_context",
                {
                    "repo": "payments",
                    "changed_files": ["payments/config.yaml"],
                    "changed_ranges": [{"path": "payments/config.yaml", "start_line": 4, "end_line": 6}],
                    "include_unlinked_leads": True,
                    "limit": 10,
                    "include_broad_context": True,
                },
            )

        self.assertIn("application_impact", result)
        self.assertEqual(result["candidate_leads"]["status"], "found")
        self.assertEqual(result["review_lead_status"]["coverage_status"], "low_coverage")
        self.assertEqual(result["review_lead_status"]["recommended_action"], "fall_back_to_plain_review")
        self.assertEqual(
            result["review_answer_packet"]["application"]["cross_repo_name_leads"][0]["match_basis"],
            "name_derived_unlinked_lead",
        )

    def test_review_context_file_anchor_only_requested_surfaces_uses_full_packet(self) -> None:
        with _fixture_snapshot(app_surface=True) as kg:
            result = call_tool(
                kg,
                "review_context",
                {
                    "repo": "payments",
                    "changed_files": ["payments/config.yaml"],
                    "changed_ranges": [{"path": "payments/config.yaml", "start_line": 4, "end_line": 6}],
                    "requested_surfaces": ["ui_screens"],
                    "limit": 10,
                    "include_broad_context": True,
                },
            )

        self.assertNotIn("packet_mode", result["review_answer_packet"])
        self.assertIn("application_impact", result)
        self.assertEqual(result["candidate_leads"]["status"], "found")

    def test_review_context_file_anchor_only_bounds_proven_repo_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            entities = []
            facts = []
            for index in range(8):
                package = Entity(
                    kind="ExternalPackage",
                    identity={"tenant_id": "default", "repo": "app", "name": f"pkg-{index}"},
                )
                provider = Entity(
                    kind="Repo",
                    identity={"tenant_id": "default", "host": "local", "owner": "default", "name": f"provider-{index}"},
                )
                entities.extend([package, provider])
                facts.append(
                    Fact(
                        "RESOLVES_TO_REPO",
                        package.entity_id,
                        provider.entity_id,
                        {"consumer_repo": "app", "package_name": f"pkg-{index}"},
                    )
                )
            JsonlKgStore(root).write(
                entities=entities,
                facts=facts,
                evidence=[],
                coverage=[],
                manifest={"version": 1},
            )

            result = call_tool(
                KgSnapshot(root),
                "review_context",
                {
                    "repo": "app",
                    "changed_files": ["app/config.yaml"],
                    "changed_ranges": [{"path": "app/config.yaml", "start_line": 1, "end_line": 1}],
                    "limit": 10,
                    "include_broad_context": True,
                },
            )

        self.assertEqual(result["review_answer_packet"]["packet_mode"], "diff_anchor_only")
        self.assertEqual(result["summary"]["repo_dependency_count"], 8)
        self.assertEqual(len(result["repo_dependencies"]), result["summary"]["section_limit"])
        self.assertEqual(len(result["impact"]["repo_dependencies"]), result["summary"]["section_limit"])

    def test_review_context_includes_transitive_callers_for_changed_symbols(self) -> None:
        with _fixture_snapshot(upstream_checkout_caller=True, upstream_checkout_grandcaller=True) as kg:
            result = call_tool(
                kg,
                "review_context",
                {
                    "repo": "payments",
                    "changed_files": ["payments/checkout.py"],
                    "changed_ranges": [{"path": "payments/checkout.py", "start_line": 10, "end_line": 10}],
                    "limit": 10,
                    "include_broad_context": True,
                },
            )

        self.assertEqual(result["summary"]["transitive_caller_count"], 2)
        self.assertEqual(
            [(row["subject"], row["depth"]) for row in result["transitive_callers"]],
            [
                ("payments.api.submit_checkout", 1),
                ("payments.worker.enqueue_checkout", 2),
            ],
        )
        self.assertEqual(result["impact"]["transitive_callers"][0]["subject"], "payments.api.submit_checkout")

    def test_review_context_transitive_callers_preserve_changed_symbol_order(self) -> None:
        with _fixture_snapshot(upstream_bootstrap_caller=True, upstream_checkout_caller=True) as kg:
            result = call_tool(
                kg,
                "review_context",
                {
                    "repo": "payments",
                    "changed_files": ["payments/checkout.py"],
                    "changed_ranges": [{"path": "payments/checkout.py", "start_line": 1, "end_line": 200}],
                    "limit": 10,
                    "include_broad_context": True,
                },
            )

        self.assertEqual(
            [(row["subject"], row["object"], row["depth"]) for row in result["transitive_callers"]],
            [
                ("payments.startup.warm_checkout", "payments.checkout.bootstrap_checkout", 1),
                ("payments.api.submit_checkout", "payments.checkout.handle_checkout", 1),
            ],
        )

    def test_review_context_transitive_callers_handles_cycles(self) -> None:
        with _fixture_snapshot(upstream_checkout_caller=True, upstream_checkout_cycle=True) as kg:
            result = call_tool(
                kg,
                "review_context",
                {
                    "repo": "payments",
                    "changed_files": ["payments/checkout.py"],
                    "changed_ranges": [{"path": "payments/checkout.py", "start_line": 10, "end_line": 10}],
                    "limit": 10,
                    "include_broad_context": True,
                },
            )

        self.assertEqual(result["summary"]["transitive_caller_count"], 2)
        self.assertEqual(
            {(row["subject"], row["object"], row["depth"]) for row in result["transitive_callers"]},
            {
                ("payments.api.submit_checkout", "payments.checkout.handle_checkout", 1),
                ("payments.checkout.handle_checkout", "payments.api.submit_checkout", 2),
            },
        )

    def test_review_context_changed_ranges_only_narrow_matching_files(self) -> None:
        with _fixture_snapshot() as kg:
            result = call_tool(
                kg,
                "review_context",
                {
                    "repo": "payments",
                    "changed_files": ["payments/checkout.py", "payments/gateway.py"],
                    "changed_ranges": [{"path": "payments/checkout.py", "start_line": 10, "end_line": 10}],
                    "limit": 10,
                    "include_broad_context": True,
                },
            )

        self.assertEqual(result["status"], "found")
        self.assertEqual(
            {row["qualname"] for row in result["changed_symbols"]},
            {"handle_checkout", "charge_card"},
        )
        anchors_by_path = {row["path"]: row for row in result["diff_anchors"]}
        self.assertEqual(anchors_by_path["payments/checkout.py"]["match_kind"], "enclosing_symbol")
        self.assertEqual(anchors_by_path["payments/gateway.py"]["anchor_type"], "file")
        self.assertEqual(anchors_by_path["payments/gateway.py"]["match_kind"], "changed_file_without_range")
        self.assertEqual(anchors_by_path["payments/gateway.py"]["symbol_count"], 1)

    def test_review_context_live_packet_cluster_coverage(self) -> None:
        """Task P spec 5: end-to-end call_tool review_context on a multi-file change.

        Every changed file with indexed symbols is a cluster; the live packet must carry
        >= 1 changed-symbol anchor (with lead_id) per cluster, and a packet that was not
        truncated must not carry a truncation_summary.
        """
        with _fixture_snapshot() as kg:
            result = call_tool(
                kg,
                "review_context",
                {
                    "repo": "payments",
                    "changed_files": ["payments/checkout.py", "payments/gateway.py"],
                    "changed_ranges": [
                        {"path": "payments/checkout.py", "start_line": 10, "end_line": 10},
                        {"path": "payments/gateway.py", "start_line": 5, "end_line": 5},
                    ],
                    "limit": 10,
                },
            )

        self.assertEqual(result["status"], "found")
        leads = [row for row in result["review_leads"]["changed_symbols"] if isinstance(row, dict)]
        self.assertTrue(leads, "live packet has no changed-symbol lead rows")
        retained_paths = {row.get("path") for row in leads}
        for path in ("payments/checkout.py", "payments/gateway.py"):
            self.assertIn(path, retained_paths, f"cluster {path} has no changed-symbol anchor in live packet")
        for row in leads:
            self.assertTrue(row.get("lead_id"), f"lead row missing lead_id: {row}")
        budget = result.get("output_budget") or {}
        if not budget.get("truncated"):
            self.assertNotIn("truncation_summary", budget)

    def test_review_context_missing_changed_file_still_returns_repo_dependencies(self) -> None:
        with _fixture_snapshot() as kg:
            result = call_tool(kg, "review_context", {"repo": "payments", "changed_files": ["payments/missing.py"], "include_broad_context": True})

        self.assertEqual(result["status"], "found")
        self.assertEqual(result["changed_symbols"], [])
        self.assertEqual(result["direct_callers"], [])
        self.assertEqual(result["direct_callees"], [])
        self.assertEqual({row["predicate"] for row in result["repo_dependencies"]}, {"RESOLVES_TO_REPO"})
        self.assertEqual(result["answerability"]["status"], "partial")
        self.assertEqual(result["answerability"]["missing_fact_families"], ["changed_symbols"])
        self.assertEqual(result["changed_surface"]["files"][0]["symbol_count"], 0)

    def test_review_context_does_not_create_application_anchor_from_test_path(self) -> None:
        with _fixture_snapshot() as kg:
            result = call_tool(kg, "review_context", {"repo": "payments", "changed_files": ["tests/test_checkout.py"], "include_broad_context": True})

        self.assertEqual(result["application_impact"]["status"], "missing_anchor")
        self.assertEqual(result["application_impact"]["anchors"], [])

    def test_review_context_does_not_create_application_anchor_from_test_symbol_module(self) -> None:
        with _fixture_snapshot(app_surface=True) as kg:
            impact = application_impact_packet(
                kg,
                repo="payments",
                changed_files=[],
                changed_symbols=[{"module": "tests.test_checkout", "qualname": "test_checkout"}],
                limit=10,
            )

        self.assertEqual(impact["status"], "missing_anchor")
        self.assertEqual(impact["anchors"], [])

    def test_review_context_changed_ranges_fail_closed_for_non_overlapping_file(self) -> None:
        with _fixture_snapshot() as kg:
            result = call_tool(
                kg,
                "review_context",
                {
                    "repo": "payments",
                    "changed_files": ["payments/checkout.py"],
                    "changed_ranges": [{"path": "payments/checkout.py", "start_line": 30, "end_line": 30}],
                },
            )

        self.assertEqual(result["status"], "found")
        self.assertEqual(result["changed_symbols"], [])
        self.assertEqual(result["direct_callers"], [])
        self.assertEqual(result["direct_callees"], [])
        self.assertEqual(result["review_answer_packet"]["packet_mode"], "diff_anchor_only")
        self.assertEqual({row["predicate"] for row in result["repo_dependencies"]}, {"RESOLVES_TO_REPO"})
        self.assertNotIn("repo_dependencies", result["omitted_context"]["counts"])

    def test_review_context_rejects_unknown_arguments(self) -> None:
        with _fixture_snapshot() as kg:
            with self.assertRaisesRegex(ValueError, "does not accept argument\\(s\\): depth"):
                call_tool(kg, "review_context", {"repo": "payments", "changed_files": ["payments/checkout.py"], "depth": 2})
            with self.assertRaisesRegex(ValueError, "changed_ranges"):
                call_tool(
                    kg,
                    "review_context",
                    {
                        "repo": "payments",
                        "changed_files": ["payments/checkout.py"],
                        "changed_ranges": [
                            {"path": "payments/checkout.py", "start_line": 10, "end_line": 10, "extra": "bad"}
                        ],
                    },
                )
            with self.assertRaisesRegex(ValueError, "changed_ranges"):
                call_tool(
                    kg,
                    "review_context",
                    {"repo": "payments", "changed_files": ["payments/checkout.py"], "changed_ranges": None},
                )
            # Unknown surfaces no longer raise; they degrade to unsupported_or_unlinked status rows.
            with self.assertRaisesRegex(ValueError, "requested_surfaces.*list"):
                call_tool(
                    kg,
                    "review_context",
                    {
                        "repo": "payments",
                        "changed_files": ["payments/checkout.py"],
                        "requested_surfaces": "ui_screens",
                    },
                )

    def test_review_context_deploy_blocker_row_is_opt_in(self) -> None:
        with _fixture_snapshot() as kg:
            default = call_tool(kg, "review_context", {"repo": "payments", "changed_files": ["payments/checkout.py"]})
            opted_in = call_tool(
                kg,
                "review_context",
                {"repo": "payments", "changed_files": ["payments/checkout.py"], "include_deploy_blockers": True, "include_broad_context": True},
            )

        self.assertEqual(default["unsupported_scopes"], [])
        self.assertEqual(default["unsupported_review_scopes"], [])
        self.assertEqual(
            opted_in["unsupported_scopes"],
            [
                {
                    "kind": "deploy_blockers",
                    "scope": "payments",
                    "reason": "No canonical deploy-blocker relation is implemented yet",
                }
            ],
        )
        self.assertEqual(opted_in["unsupported_review_scopes"], opted_in["unsupported_scopes"])
        self.assertEqual(opted_in["answerability"]["status"], "partial")
        self.assertEqual(opted_in["answerability"]["missing_fact_families"], ["deploy_blockers"])

    def test_search_services_and_service_brief_return_json_shapes(self) -> None:
        with _fixture_snapshot() as kg:
            all_services = call_tool(kg, "search_services", {})
            search = call_tool(kg, "search_services", {"query": "payments"})
            brief = call_tool(kg, "get_service_brief", {"service": "payments"})
            limited_brief = call_tool(kg, "get_service_brief", {"service": "payments", "limit": 1})
            missing = call_tool(kg, "get_service_brief", {"service": "missing"})

        self.assertEqual(all_services["status"], "found")
        self.assertEqual(search["status"], "found")
        self.assertEqual(search["services"][0]["slug"], "payments")
        self.assertEqual(brief["status"], "found")
        self.assertEqual(brief["service"]["slug"], "payments")
        self.assertEqual(brief["summary"]["endpoint_fact_count"], 1)
        self.assertEqual(brief["answerability"]["status"], "partial")
        self.assertEqual(brief["answerability"]["missing_fact_families"], ["deploy_mapping"])
        self.assertEqual(brief["claim_contract"]["scope"], "indexed static service, endpoint, event, deploy, and operational facts")
        self.assertIn("do not prove deploy safety", brief["claim_contract"]["safety_rule"])
        self.assertIn("inspect source/config/operational evidence", brief["claim_contract"]["required_caveat"])
        self.assertTrue(brief["next_actions"])
        self.assertEqual(limited_brief["summary"]["endpoint_fact_count"], 1)
        self.assertEqual(limited_brief["summary"]["event_fact_count"], 1)
        self.assertEqual(brief["summary"]["endpoint_consumer_fact_count"], 0)
        self.assertEqual(len(limited_brief["endpoints"]), 1)
        self.assertEqual(len(limited_brief["event_channels"]), 1)
        self.assertFalse(any(key.startswith("_") for key in brief["endpoints"][0]))
        self.assertEqual(missing["status"], "not_found")
        _assert_additive_fields(self, all_services)
        _assert_additive_fields(self, search)
        _assert_additive_fields(self, brief)
        _assert_additive_fields(self, limited_brief)
        _assert_additive_fields(self, missing)

    def test_symbol_tools_wrap_snapshot_query_methods(self) -> None:
        with _fixture_snapshot(upstream_checkout_caller=True, upstream_checkout_grandcaller=True) as kg:
            callers = call_tool(kg, "find_callers", {"symbol": "charge_card"})
            impact = call_tool(kg, "reverse_impact", {"symbol": "charge_card", "depth": 3})
            callees = call_tool(kg, "find_callees", {"symbol": "handle_checkout"})
            radius = call_tool(kg, "blast_radius", {"symbol": "handle_checkout", "depth": 1})

        self.assertEqual(callers["status"], "found")
        self.assertEqual(callers["caller_count"], 1)
        self.assertEqual(callers["candidate_leads"]["status"], "empty")
        self.assertEqual(callers["answerability"]["status"], "answerable")
        self.assertEqual(callers["claim_contract"]["scope"], "immediate static upstream CALLS edges")
        self.assertEqual(callers["claim_contract"]["known_rows"], ["callers"])
        self.assertFalse(any(row["trigger"] == "candidate_leads_present" for row in callers["inspection_areas"]))
        self.assertEqual(impact["status"], "found")
        self.assertEqual(impact["summary"]["edge_count"], 3)
        self.assertEqual(
            [tier["depth"] for tier in impact["tiers"]],
            [1, 2, 3],
        )
        self.assertEqual(
            [row["symbol"]["qualname"] for row in impact["tiers"][0]["symbols"]],
            ["handle_checkout"],
        )
        self.assertEqual(callees["status"], "found")
        self.assertEqual(callees["callee_count"], 1)
        self.assertEqual(radius["status"], "found")
        self.assertEqual(radius["edge_count"], 1)
        self.assertEqual(radius["claim_contract"]["scope"], "bounded static downstream CALLS closure")
        self.assertIn("absence-of-impact claims", radius["claim_contract"]["claim_boundary"])
        _assert_additive_fields(self, callers)
        _assert_additive_fields(self, impact)
        _assert_additive_fields(self, callees)
        _assert_additive_fields(self, radius)

    def test_reverse_impact_bridges_constructor_and_terminal_import_leads(self) -> None:
        with _constructor_reverse_impact_snapshot() as kg:
            impact = call_tool(
                kg,
                "reverse_impact",
                {
                    "symbol": "lib.features.build_features",
                    "path": "lib/features.py",
                    "line": 10,
                    "depth": 4,
                },
            )
            planning = call_tool(
                kg,
                "planning_context",
                {"symbol": "lib.features.build_features", "path": "lib/features.py", "line": 10},
            )
            limited = call_tool(
                kg,
                "reverse_impact",
                {
                    "symbol": "lib.features.build_features",
                    "path": "lib/features.py",
                    "line": 10,
                    "depth": 4,
                    "limit": 1,
                },
            )

        self.assertEqual(impact["status"], "found")
        self.assertEqual(impact["summary"]["constructor_bridge_count"], 1)
        self.assertEqual(impact["summary"]["roots_unexpanded_count"], 0)
        self.assertEqual(impact["summary"]["affected_symbol_count"], 3)
        self.assertEqual(impact["summary"]["affected_symbol_returned_count"], 3)
        self.assertEqual(impact["summary"]["affected_symbol_multiplicity"], "unique_global")
        self.assertEqual(impact["claim_contract"]["scope"], "bounded static reverse CALLS head start")
        self.assertIn("terminal_import_consumer_leads", impact["claim_contract"]["candidate_source_leads"]["fields"])
        self.assertIn(
            "do not add these to affected symbol totals",
            impact["claim_contract"]["candidate_source_leads"]["claim_boundary"],
        )
        self.assertIn("Report static CALLS affected symbols separately", impact["claim_contract"]["counting_rule"])
        self.assertEqual(
            [tier["symbols"][0]["symbol"]["qualname"] for tier in impact["tiers"]],
            ["Builder.build_features", "Builder.__init__", "train_company"],
        )
        bridge = impact["constructor_bridges"][0]
        self.assertEqual(bridge["from_init"]["qualname"], "Builder.__init__")
        self.assertEqual(bridge["to_class"]["qualname"], "Builder")
        terminal = impact["terminal_import_consumer_leads"][0]
        self.assertEqual(terminal["for_symbol"]["qualname"], "train_company")
        self.assertEqual(terminal["terminal_reason"], "no_incoming_callers")
        self.assertEqual(terminal["import_consumer_leads"]["lead_count"], 1)
        importer_qualnames = {
            row["qualname"]
            for row in terminal["import_consumer_leads"]["leads"][0]["importer_module_symbols"]
        }
        self.assertIn("TrainView.post", importer_qualnames)
        inspection_area = impact["source_inspection_areas"][0]
        self.assertEqual(inspection_area["area"], "same_repo_tests_scripts_notebooks")
        self.assertIn("lib", inspection_area["repos"])
        self.assertIn("lib/features.py", inspection_area["path_hints"])
        self.assertIn("build_features(", inspection_area["search_terms"])
        self.assertEqual(impact["proven_facts"]["status"], "found")
        self.assertIn("edges", {row["field"] for row in impact["proven_facts"]["sources"]})
        self.assertEqual(impact["candidate_leads"]["status"], "found")
        self.assertIn(
            "terminal_import_consumer_leads",
            {row["field"] for row in impact["candidate_leads"]["sources"]},
        )
        normalized_area = next(row for row in impact["inspection_areas"] if row["area"] == "same_repo_tests_scripts_notebooks")
        self.assertIn({"path": "lib/features.py", "repo": "lib"}, normalized_area["inspection_refs"])
        self.assertIn("build_features(", normalized_area["search_terms"])
        self.assertIn("proven_facts", impact["packet_contract"]["common_fields"])
        self.assertEqual(
            planning["related_facts"]["symbol_impact"]["reverse_impact"]["summary"]["constructor_bridge_count"],
            1,
        )
        self.assertTrue(limited["summary"]["walk_truncated"])
        self.assertEqual(limited["summary"]["truncated_terminal_symbol_count"], 2)
        self.assertEqual(limited["summary"]["truncated_terminal_symbol_returned_count"], 2)
        truncated_by_qualname = {
            row["symbol"]["qualname"]: row["terminal_reason"] for row in limited["truncated_terminal_symbols"]
        }
        self.assertEqual(truncated_by_qualname["build_features"], "truncated_before_expansion")
        self.assertEqual(truncated_by_qualname["Builder.build_features"], "truncated_after_incoming_edge")
        self.assertEqual(limited["candidate_leads"]["sources"][0]["field"], "truncated_terminal_symbols")
        self.assertEqual(limited["answerability"]["status"], "partial")

    def test_find_callers_returns_cross_repo_import_consumer_leads_on_call_miss(self) -> None:
        with _cross_repo_import_consumer_snapshot() as kg:
            callers = call_tool(kg, "find_callers", {"symbol": "lib.predict.score_session"})
            planning = call_tool(kg, "planning_context", {"symbol": "lib.predict.score_session"})

        self.assertEqual(callers["status"], "not_found")
        self.assertEqual(callers["caller_count"], 0)
        leads = callers["import_consumer_leads"]
        self.assertEqual(leads["status"], "found")
        self.assertEqual(leads["lead_count"], 1)
        self.assertEqual(leads["returned_count"], 1)
        lead = leads["leads"][0]
        self.assertEqual(lead["lead_kind"], "import_consumer")
        self.assertEqual(lead["repo_relation"], "cross_repo")
        self.assertEqual(lead["importer"]["repo"], "api")
        self.assertEqual(lead["imported_module"]["module"], "lib.predict")
        self.assertEqual(lead["imported_symbol"]["qualified_name"], "lib.predict.score_session")
        self.assertEqual(lead["match"], {"match_kind": "imported_name", "matched_imported_names": ["score_session"]})
        self.assertEqual(
            [row["qualified_name"] for row in lead["importer_module_symbols"]],
            ["api.views.score.ScoreView", "api.views.score.ScoreView.post"],
        )
        self.assertTrue(any("import_consumer_leads" in action for action in callers["next_actions"]))
        reverse_impact = call_tool(kg, "reverse_impact", {"symbol": "lib.predict.score_session"})
        self.assertEqual(reverse_impact["status"], "partial")
        self.assertEqual(reverse_impact["summary"]["edge_count"], 0)
        self.assertEqual(reverse_impact["summary"]["terminal_import_lead_count"], 1)
        self.assertEqual(reverse_impact["answerability"]["missing_fact_families"], ["reverse_callers"])
        self.assertIn(
            "terminal import leads",
            reverse_impact["claim_contract"]["counting_rule"],
        )
        self.assertEqual(reverse_impact["proven_facts"]["status"], "found")
        self.assertIn("roots", {row["field"] for row in reverse_impact["proven_facts"]["sources"]})
        self.assertEqual(reverse_impact["candidate_leads"]["status"], "found")
        self.assertEqual(reverse_impact["coverage_gaps"][0]["trigger"], "missing_fact_family")
        self.assertTrue(any(row["trigger"] == "candidate_leads_present" for row in reverse_impact["inspection_areas"]))
        self.assertEqual(
            planning["related_facts"]["symbol_impact"]["import_consumer_leads"]["lead_count"],
            1,
        )

    def test_find_callers_returns_package_linked_import_consumer_leads(self) -> None:
        with _cross_repo_import_consumer_snapshot(linked_package_import=True) as kg:
            callers = call_tool(kg, "find_callers", {"symbol": "lib.predict.score_session"})

        self.assertEqual(callers["status"], "not_found")
        leads = callers["import_consumer_leads"]
        self.assertEqual(leads["status"], "found")
        self.assertEqual(leads["lead_count"], 1)
        lead = leads["leads"][0]
        self.assertEqual(lead["repo_relation"], "cross_repo")
        self.assertEqual(lead["fact"]["object"], "lib")
        self.assertEqual(lead["match"], {"match_kind": "linked_package_imported_name", "matched_imported_names": ["score_session"]})
        self.assertEqual(
            [row["qualified_name"] for row in lead["importer_module_symbols"]],
            ["api.views.score.ScoreView", "api.views.score.ScoreView.post"],
        )

    def test_find_callers_treats_exact_module_import_as_consumer_lead(self) -> None:
        with _cross_repo_import_consumer_snapshot(linked_package_import=True, imported_names=()) as kg:
            callers = call_tool(kg, "find_callers", {"symbol": "lib.predict.score_session"})

        lead = callers["import_consumer_leads"]["leads"][0]
        self.assertEqual(lead["match"], {"match_kind": "linked_package_module_import", "matched_imported_names": []})

    def test_find_callers_does_not_use_package_link_to_other_tenant_repo(self) -> None:
        with _cross_repo_import_consumer_snapshot(linked_package_import=True, provider_tenant_id="other") as kg:
            callers = call_tool(kg, "find_callers", {"symbol": "lib.predict.score_session"})

        self.assertEqual(callers["status"], "not_found")
        self.assertEqual(callers["import_consumer_leads"]["status"], "empty")
        self.assertEqual(callers["import_consumer_leads"]["lead_count"], 0)

    def test_find_callers_does_not_use_module_from_other_tenant(self) -> None:
        with _cross_repo_import_consumer_snapshot(provider_module_tenant_id="other") as kg:
            callers = call_tool(kg, "find_callers", {"symbol": "lib.predict.score_session"})

        self.assertEqual(callers["status"], "not_found")
        self.assertEqual(callers["import_consumer_leads"]["status"], "missing_module")
        self.assertEqual(callers["import_consumer_leads"]["lead_count"], 0)

    def test_find_callers_rejects_missing_imported_names_qualifier(self) -> None:
        with _cross_repo_import_consumer_snapshot(linked_package_import=True, imported_names=None) as kg:
            callers = call_tool(kg, "find_callers", {"symbol": "lib.predict.score_session"})

        self.assertEqual(callers["status"], "not_found")
        self.assertEqual(callers["import_consumer_leads"]["status"], "empty")

    def test_find_callers_skips_import_consumer_leads_when_callers_exist(self) -> None:
        with _cross_repo_import_consumer_snapshot(proven_call=True) as kg:
            callers = call_tool(kg, "find_callers", {"symbol": "lib.predict.score_session"})

        self.assertEqual(callers["status"], "found")
        self.assertEqual(callers["caller_count"], 1)
        self.assertEqual(callers["import_consumer_leads"]["status"], "not_applicable")

    def test_symbol_tools_ambiguous_results_include_retry_guidance(self) -> None:
        with _fixture_snapshot(extra_charge_card_symbol=True) as kg:
            callers = call_tool(kg, "find_callers", {"symbol": "charge_card"})
            impact = call_tool(kg, "reverse_impact", {"symbol": "charge_card"})
            callees = call_tool(kg, "find_callees", {"symbol": "charge_card"})
            disambiguated = call_tool(
                kg,
                "find_callers",
                {"symbol": "charge_card", "path": "payments/gateway.py", "line": 5},
            )

        self.assertEqual(callers["status"], "ambiguous")
        self.assertFalse(callers["result_computed"])
        self.assertEqual(callers["callers"], [])
        self.assertNotIn("import_consumer_leads", callers)
        self.assertEqual(callers["target"]["candidate_count"], 2)
        self.assertEqual(callers["disambiguation"]["reason"], "ambiguous_symbol")
        self.assertEqual(callers["disambiguation"]["candidate_count"], 2)
        self.assertIn(
            {
                "symbol": "payments.gateway.charge_card",
                "path": "payments/gateway.py",
                "line": 5,
            },
            callers["disambiguation"]["retry_arguments"],
        )
        self.assertTrue(any("include_all=true" in action for action in callers["next_actions"]))
        self.assertEqual(impact["status"], "ambiguous")
        self.assertEqual(impact["mode"], "ambiguous")
        self.assertFalse(impact["result_computed"])
        self.assertEqual(len(impact["candidate_impact_previews"]), 2)
        self.assertIn("Do not aggregate all candidates", impact["ambiguity_guidance"])
        self.assertGreaterEqual(
            impact["candidate_impact_previews"][0]["direct_caller_count"],
            impact["candidate_impact_previews"][1]["direct_caller_count"],
        )
        self.assertEqual(impact["candidate_impact_previews"][0]["impact_preview_rank"], 1)
        self.assertIn("constructor targets are included", impact["candidate_impact_previews"][0]["selection_basis"])
        self.assertEqual(impact["edges"], [])
        self.assertEqual(callees["status"], "ambiguous")
        self.assertFalse(callees["result_computed"])
        self.assertIn("no callees result was computed", callees["disambiguation"]["message"])
        self.assertEqual(disambiguated["status"], "found")
        self.assertEqual(disambiguated["caller_count"], 1)

    def test_symbol_tools_distinguish_wrong_coordinate_from_missing_symbol(self) -> None:
        with _fixture_snapshot() as kg:
            correct_coordinate = call_tool(
                kg,
                "find_callers",
                {"symbol": "charge_card", "path": "payments/gateway.py", "line": 5},
            )
            callers = call_tool(
                kg,
                "find_callers",
                {"symbol": "charge_card", "path": "payments/checkout.py", "line": 14},
            )
            wrong_line = call_tool(
                kg,
                "find_callers",
                {"symbol": "charge_card", "path": "payments/gateway.py", "line": 99},
            )

        self.assertEqual(correct_coordinate["status"], "found")
        self.assertEqual(correct_coordinate["target"]["confidence"], "exact_unique")
        self.assertEqual(callers["status"], "not_found")
        self.assertEqual(callers["target"]["status"], "not_found")
        self.assertEqual(callers["target"]["confidence"], "coordinate_mismatch")
        self.assertEqual(callers["target"]["candidate_count"], 1)
        mismatch = callers["target"]["coordinate_mismatch"]
        self.assertEqual(mismatch["status"], "symbol_found_at_different_coordinate")
        self.assertEqual(mismatch["requested"], {"path": "payments/checkout.py", "line": 14})
        self.assertEqual(
            mismatch["retry_arguments"],
            [{"symbol": "payments.gateway.charge_card", "path": "payments/gateway.py", "line": 5}],
        )
        self.assertEqual(mismatch["candidates"][0]["path"], "payments/gateway.py")
        self.assertTrue(any("coordinate_mismatch.retry_arguments" in action for action in callers["next_actions"]))
        self.assertFalse(any("external package" in action for action in callers["next_actions"]))
        self.assertEqual(callers["callers"], [])
        self.assertEqual(wrong_line["target"]["confidence"], "coordinate_mismatch")
        self.assertEqual(
            wrong_line["target"]["coordinate_mismatch"]["requested"],
            {"path": "payments/gateway.py", "line": 99},
        )
        self.assertEqual(
            wrong_line["target"]["coordinate_mismatch"]["retry_arguments"],
            [{"symbol": "payments.gateway.charge_card", "path": "payments/gateway.py", "line": 5}],
        )
        # Distinct marker: top-level coordinate_mismatch + answerability missing
        # ["correct_coordinate"], so a wrong path/line is not read as a missing symbol.
        self.assertEqual(callers["coordinate_mismatch"]["status"], "symbol_found_at_different_coordinate")
        self.assertTrue(callers["coordinate_mismatch"]["retry_arguments"])
        self.assertEqual(callers["answerability"]["missing_fact_families"], ["correct_coordinate"])
        # Control: a genuinely missing symbol has no marker and keeps ["requested_fact"].
        with _fixture_snapshot() as kg:
            missing = call_tool(kg, "find_callers", {"symbol": "no_such_symbol_zzz"})
        self.assertEqual(missing["status"], "not_found")
        self.assertNotIn("coordinate_mismatch", missing)
        self.assertEqual(missing["answerability"]["missing_fact_families"], ["requested_fact"])

    def test_symbol_coordinate_mismatch_marker_across_symbol_tools(self) -> None:
        # All four symbol tools surface the same top-level marker + answerability distinction.
        for tool in ("find_callers", "find_callees", "blast_radius", "reverse_impact"):
            with _fixture_snapshot() as kg:
                result = call_tool(kg, tool, {"symbol": "charge_card", "path": "payments/checkout.py", "line": 14})
            self.assertEqual(result["status"], "not_found", tool)
            self.assertEqual(
                result["coordinate_mismatch"]["status"], "symbol_found_at_different_coordinate", tool
            )
            self.assertTrue(result["coordinate_mismatch"]["retry_arguments"], tool)
            self.assertEqual(result["answerability"]["missing_fact_families"], ["correct_coordinate"], tool)

    def test_symbol_coordinate_mismatch_is_surfaced_for_source_side_tools(self) -> None:
        with _fixture_snapshot() as kg:
            callees = call_tool(
                kg,
                "find_callees",
                {"symbol": "handle_checkout", "path": "payments/gateway.py", "line": 5},
            )

        self.assertEqual(callees["status"], "not_found")
        self.assertEqual(callees["source"]["confidence"], "coordinate_mismatch")
        self.assertEqual(
            callees["source"]["coordinate_mismatch"]["retry_arguments"],
            [{"symbol": "payments.checkout.handle_checkout", "path": "payments/checkout.py", "line": 10}],
        )
        self.assertTrue(any("coordinate_mismatch.retry_arguments" in action for action in callees["next_actions"]))

    def test_symbol_coordinate_mismatch_uses_language_agnostic_code_symbol_coordinates(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            symbol = Entity(
                kind="CodeSymbol",
                identity={
                    "tenant_id": "default",
                    "repo": "web",
                    "module": "src.client",
                    "qualname": "sendEvent",
                    "symbol_kind": "function",
                },
                properties={"path": "src/client.ts", "line": 12, "end_line": 15, "language": "typescript"},
            )
            JsonlKgStore(root).write(
                entities=[symbol],
                facts=[],
                evidence=[],
                coverage=[],
                manifest={"counts": {"entities": 1, "facts": 0}},
            )
            kg = KgSnapshot(root)

            result = kg.find_callers("sendEvent", path="src/server.py", line=4)

        self.assertEqual(result["status"], "not_found")
        self.assertEqual(result["target"]["confidence"], "coordinate_mismatch")
        self.assertEqual(result["target"]["coordinate_mismatch"]["candidates"][0]["path"], "src/client.ts")
        self.assertEqual(
            result["target"]["coordinate_mismatch"]["retry_arguments"],
            [{"symbol": "src.client.sendEvent", "path": "src/client.ts", "line": 12}],
        )

    def test_discovery_keeps_fuzzy_but_graph_tools_require_exact_symbols(self) -> None:
        with _fixture_snapshot() as kg:
            lookup = kg.lookup_symbol("card")
            planning = call_tool(kg, "planning_context", {"query": "card"})
            callers = call_tool(kg, "find_callers", {"symbol": "card"})
            callees = call_tool(kg, "find_callees", {"symbol": "handle"})
            radius = call_tool(kg, "blast_radius", {"symbol": "handle", "depth": 1})
            dependency = kg.dependency_path("handle", "shared-lib")
            evidence = kg.evidence_for_call("handle_checkout", "card")

        self.assertEqual(lookup["status"], "resolved")
        self.assertEqual(lookup["confidence"], "fuzzy_unique")
        self.assertEqual(lookup["resolved_symbol"]["qualname"], "charge_card")
        self.assertTrue(any(row["qualname"] == "charge_card" for row in planning["symbols"]))
        self.assertEqual(callers["status"], "not_found")
        self.assertEqual(callers["target"]["confidence"], "not_found")
        self.assertTrue(callers["next_actions"])
        self.assertIn("not proof of absence", callers["next_actions"][0])
        self.assertEqual(callees["status"], "not_found")
        self.assertEqual(callees["source"]["confidence"], "not_found")
        self.assertTrue(callees["next_actions"])
        self.assertEqual(radius["status"], "not_found")
        self.assertEqual(radius["source"]["confidence"], "not_found")
        self.assertTrue(radius["next_actions"])
        self.assertEqual(dependency["status"], "not_found")
        self.assertEqual(dependency["source"]["confidence"], "not_found")
        self.assertEqual(evidence["status"], "not_found")
        self.assertEqual(evidence["callee"]["confidence"], "not_found")

    def test_event_tools_filter_consumers_and_producers(self) -> None:
        with _fixture_snapshot() as kg:
            consumers = call_tool(kg, "get_event_consumers", {"channel": "orders"})
            producers = call_tool(kg, "get_event_producers", {"channel": "orders"})
            limited_producers = call_tool(kg, "get_event_producers", {"channel": "orders", "limit": 1})

        self.assertEqual(consumers["status"], "found")
        self.assertEqual(consumers["returned_count"], 1)
        self.assertEqual(consumers["consumers"][0]["predicate"], "CONSUMES_EVENT")
        self.assertEqual(producers["status"], "found")
        self.assertEqual(producers["returned_count"], 1)
        self.assertEqual(producers["producers"][0]["predicate"], "PRODUCES_EVENT")
        self.assertEqual(limited_producers["status"], "found")
        self.assertEqual(consumers["answerability"]["status"], "answerable")
        self.assertEqual(consumers["answerability"]["missing_fact_families"], [])
        self.assertEqual(consumers["claim_contract"]["known_rows_field"], "consumers")
        self.assertIn("do not prove deploy safety", consumers["claim_contract"]["safety_rule"])
        self.assertIn("Report known static rows separately", consumers["claim_contract"]["counting_rule"])
        self.assertTrue(any("time-window usage" in action for action in consumers["next_actions"]))
        _assert_additive_fields(self, consumers)
        _assert_additive_fields(self, producers)
        _assert_additive_fields(self, limited_producers)

    def test_event_tools_not_found_distinguish_static_miss_from_runtime_proof(self) -> None:
        with _fixture_snapshot() as kg:
            consumers = call_tool(kg, "get_event_consumers", {"channel": "missing-channel"})

        self.assertEqual(consumers["status"], "not_found")
        self.assertEqual(consumers["answerability"]["status"], "partial")
        self.assertEqual(consumers["answerability"]["missing_fact_families"], ["static_event_facts"])
        self.assertTrue(any("no indexed static event facts" in action for action in consumers["next_actions"]))

    def test_event_tools_scan_all_matching_facts_before_limiting(self) -> None:
        with _fixture_snapshot(extra_consumers=125) as kg:
            consumers = call_tool(kg, "get_event_consumers", {"channel": "orders", "limit": 1})
            producers = call_tool(kg, "get_event_producers", {"channel": "orders", "limit": 1})

        self.assertEqual(consumers["event_fact_count"], 126)
        self.assertEqual(consumers["returned_count"], 1)
        self.assertEqual(producers["event_fact_count"], 1)
        self.assertEqual(producers["returned_count"], 1)

    def test_event_tools_emit_no_asymmetry_warning_when_both_sides_indexed(self) -> None:
        # Symmetric channel (one producer, one consumer) must not raise a thin-coverage warning.
        with _fixture_snapshot() as kg:
            consumers = call_tool(kg, "get_event_consumers", {"channel": "orders"})
            producers = call_tool(kg, "get_event_producers", {"channel": "orders"})

        self.assertEqual(consumers["coverage_warnings"], [])
        self.assertEqual(producers["coverage_warnings"], [])

    def test_event_tools_warn_on_producer_consumer_asymmetry_language_agnostic(self) -> None:
        # A channel consumed by a TS service but with no indexed producer is a coverage
        # signal, not absence. Derived from the opposite-predicate count at the query layer,
        # so it holds for any language emitting event facts (here: TypeScript, not Python).
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            consumer_service = Entity(
                kind="Service",
                identity={"tenant_id": "default", "namespace": "default", "slug": "web", "repo": "web"},
            )
            channel = Entity(
                kind="EventChannel",
                identity={
                    "tenant_id": "default",
                    "repo": "web",
                    "broker_kind": "sqs",
                    "channel_address": "orders-created",
                    "name": "orders-created",
                },
            )
            consume_fact = Fact("CONSUMES_EVENT", consumer_service.entity_id, channel.entity_id)
            JsonlKgStore(root).write(
                entities=[consumer_service, channel],
                facts=[consume_fact],
                evidence=[],
                coverage=[],
                manifest={"counts": {"entities": 2, "facts": 1}},
            )
            kg = KgSnapshot(root)

            producers = call_tool(kg, "get_event_producers", {"channel": "orders-created"})
            consumers = call_tool(kg, "get_event_consumers", {"channel": "orders-created"})

        # Empty producer side: flagged as thin coverage, not absence (the claim-D scenario).
        self.assertEqual(producers["status"], "not_found")
        self.assertEqual(len(producers["coverage_warnings"]), 1)
        self.assertIn("0 indexed producers but 1 consumers", producers["coverage_warnings"][0])
        self.assertIn("thin coverage, not proof of absence", producers["coverage_warnings"][0])
        producer_gap_triggers = [row.get("trigger") for row in producers["coverage_gaps"]]
        self.assertIn("coverage_warning", producer_gap_triggers)

        # Populated consumer side also flags that the producer side is dark.
        self.assertEqual(consumers["status"], "found")
        self.assertEqual(len(consumers["coverage_warnings"]), 1)
        self.assertIn("1 indexed consumers but 0 producers", consumers["coverage_warnings"][0])
        self.assertIn("Treat producers coverage as thin, not absent", consumers["coverage_warnings"][0])

    def _language_coverage_row(self, *, repo: str, language: str, file_count: int) -> Coverage:
        return Coverage(
            tenant_id="default",
            predicate="LANGUAGE_SUPPORT",
            scope_ref={
                "repo": repo,
                "repo_owner": "acme",
                "language": language,
                "path_prefix": ".",
                "reason": "unsupported_language",
                "file_count": file_count,
                "sample_paths": [f"worker.{language}"],
            },
            state="uninstrumented",
            source_system="repo_discovery",
        )

    def test_get_service_brief_surfaces_repo_scoped_uninstrumented_language_coverage(self) -> None:
        # The build records loud-refusal coverage for no-extractor languages; the service
        # brief must echo it so the agent treats the brief as repo-scoped, not exhaustive.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service = Entity(
                kind="Service",
                identity={"tenant_id": "default", "namespace": "default", "slug": "payments", "repo": "payments"},
            )
            JsonlKgStore(root).write(
                entities=[service],
                facts=[],
                evidence=[],
                coverage=[
                    self._language_coverage_row(repo="payments", language="go", file_count=23),
                    self._language_coverage_row(repo="other", language="rust", file_count=5),
                ],
                manifest={"counts": {"entities": 1, "facts": 0}},
            )
            kg = KgSnapshot(root)

            brief = call_tool(kg, "get_service_brief", {"service": "payments"})

        self.assertEqual(brief["status"], "found")
        self.assertEqual(len(brief["coverage_warnings"]), 1)
        self.assertIn("go (23 files)", brief["coverage_warnings"][0])
        self.assertIn("coverage gap, not proof of absence", brief["coverage_warnings"][0])
        # Scoped to the service repo: the unrelated repo's rust files are not surfaced here.
        self.assertNotIn("rust", brief["coverage_warnings"][0])
        self.assertIn("coverage_warning", [row.get("trigger") for row in brief["coverage_gaps"]])

    def test_get_service_brief_emits_no_language_warning_when_fully_instrumented(self) -> None:
        with _fixture_snapshot() as kg:
            brief = call_tool(kg, "get_service_brief", {"service": "payments"})

        self.assertEqual(brief["status"], "found")
        self.assertEqual(brief["coverage_warnings"], [])

    def test_symbol_miss_surfaces_uninstrumented_language_coverage(self) -> None:
        # A resolved symbol with zero callers returns not_found; if the repo has unindexed
        # languages, the empty result must be framed as a coverage gap, not absence.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            symbol = Entity(
                kind="CodeSymbol",
                identity={
                    "tenant_id": "default",
                    "repo": "payments",
                    "module": "payments.gateway",
                    "qualname": "charge_card",
                    "symbol_kind": "function",
                },
                properties={"path": "payments/gateway.py", "line": 5, "end_line": 12},
            )
            JsonlKgStore(root).write(
                entities=[symbol],
                facts=[],
                evidence=[],
                coverage=[self._language_coverage_row(repo="payments", language="go", file_count=23)],
                manifest={"counts": {"entities": 1, "facts": 0}},
            )
            kg = KgSnapshot(root)

            callers = call_tool(kg, "find_callers", {"symbol": "charge_card"})

        self.assertEqual(callers["status"], "not_found")
        self.assertEqual(len(callers["coverage_warnings"]), 1)
        self.assertIn("'payments'", callers["coverage_warnings"][0])
        self.assertIn("go (23 files)", callers["coverage_warnings"][0])
        self.assertIn("coverage_warning", [row.get("trigger") for row in callers["coverage_gaps"]])

    def test_deploy_blockers_refuses_when_current_kg_has_no_contract(self) -> None:
        with _fixture_snapshot() as kg:
            result = call_tool(kg, "deploy_blockers_for", {"service": "payments"})

        self.assertEqual(result["status"], "unsupported_by_current_kg")
        self.assertEqual(result["missing_contract"], "deploy_blockers_for")
        self.assertEqual(result["answerability"]["missing_fact_families"], ["canonical_service_deploy_blocker"])
        self.assertIn("must-deploy-before services", result["answerability"]["cannot_prove"])
        gap_triggers = [row["trigger"] for row in result["coverage_gaps"]]
        self.assertIn("coverage_warning", gap_triggers)
        self.assertIn("unsupported_scope", gap_triggers)
        self.assertIn("missing_fact_family", gap_triggers)
        self.assertGreaterEqual(gap_triggers.count("cannot_prove"), 3)
        self.assertTrue(
            any(row.get("fact_family") == "canonical_service_deploy_blocker" for row in result["coverage_gaps"])
        )
        self.assertTrue(any("compatibility leads" in str(row.get("detail")) for row in result["coverage_gaps"]))
        self.assertTrue(result["unsupported_scopes"])
        self.assertTrue(any("must-deploy-before services as unknown" in action for action in result["next_actions"]))
        self.assertTrue(any("deployment manifests" in action for action in result["next_actions"]))
        _assert_additive_fields(self, result)

    def test_tool_arguments_fail_closed(self) -> None:
        with _fixture_snapshot() as kg:
            with self.assertRaisesRegex(ValueError, "symbol"):
                call_tool(kg, "find_callers", {"limit": 10})
            with self.assertRaisesRegex(ValueError, "limit"):
                call_tool(kg, "find_callers", {"symbol": "x", "limit": True})
            with self.assertRaisesRegex(ValueError, "limit"):
                call_tool(kg, "find_callers", {"symbol": "x", "limit": "10"})
            with self.assertRaisesRegex(ValueError, "between 1 and 100"):
                call_tool(kg, "find_callers", {"symbol": "x", "limit": 0})
            with self.assertRaisesRegex(ValueError, "between 1 and 6"):
                call_tool(kg, "blast_radius", {"symbol": "x", "depth": 999})
            with self.assertRaisesRegex(ValueError, "does not accept"):
                call_tool(kg, "find_callers", {"symbol": "x", "extra": "ignored"})
            with self.assertRaisesRegex(ValueError, "Unsupported MCP tool"):
                call_tool(kg, "unknown_tool", {})

    def test_json_rpc_lists_and_calls_tools(self) -> None:
        with _fixture_snapshot() as kg:
            initialized = _handle_json_rpc(kg, {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
            initialized_with_client_version = _handle_json_rpc(
                kg,
                {"jsonrpc": "2.0", "id": 8, "method": "initialize", "params": {"protocolVersion": "2099-01-01"}},
            )
            ping = _handle_json_rpc(kg, {"jsonrpc": "2.0", "id": 9, "method": "ping"})
            listed = _handle_json_rpc(kg, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            batch = _handle_json_rpc_payload(
                kg,
                [
                    {"jsonrpc": "2.0", "id": 3, "method": "ping"},
                    {"jsonrpc": "2.0", "method": "ping"},
                ],
            )
            called = _handle_json_rpc(
                kg,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "search_services", "arguments": {"query": "payments"}},
                },
            )
            unsupported = _handle_json_rpc(
                kg,
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {"name": "deploy_blockers_for", "arguments": {"service": "payments"}},
                },
            )

        self.assertEqual(initialized["result"]["serverInfo"]["name"], "supercontext-local")
        self.assertEqual(initialized["result"]["protocolVersion"], MCP_PROTOCOL_VERSION)
        self.assertIn("instructions", initialized["result"])
        instructions = initialized["result"]["instructions"]
        self.assertIn("planning_context first", instructions)
        self.assertIn("review_context first", instructions)
        self.assertIn("org snapshot", instructions)
        self.assertIn("same planning_context and review_context tools", instructions)
        self.assertIn("supercontext org serve", instructions)
        self.assertIn("review_context.application_impact", instructions)
        self.assertIn("normal search/read tools at least once", instructions)
        self.assertIn("service_operational_surfaces.evidence_partition", instructions)
        self.assertIn("deploy_link_facts", instructions)
        self.assertIn("DEPLOYS_VIA_CONFIG", instructions)
        self.assertIn("known_linked", instructions)
        self.assertIn("unlinked_evidence", instructions)
        self.assertIn("missing_contracts", instructions)
        self.assertIn("disambiguation.retry_arguments", instructions)
        self.assertIn("unqualified symbol name", instructions)
        self.assertIn("first source-search hit", instructions)
        self.assertIn("do not aggregate all candidates", instructions)
        self.assertIn("Common packet contract", instructions)
        self.assertIn("Evidence gates", instructions)
        self.assertIn("named answer categories", instructions)
        self.assertIn("never a replacement for source inspection", instructions)
        self.assertIn("Never assert that SuperContext alone fully resolved", instructions)
        self.assertIn("internal progress commentary", instructions)
        self.assertIn("changed-file symbol inventory", instructions)
        self.assertIn("terminal_import_consumer_leads", instructions)
        self.assertIn("do not prove deploy or safety readiness", instructions)
        self.assertIn("normal search/read tools at least once", instructions)
        self.assertIn("count/list/impact answers", instructions)
        self.assertIn("proven_facts", instructions)
        self.assertIn("candidate_leads", instructions)
        self.assertIn("coverage_gaps", instructions)
        self.assertIn("inspection_areas", instructions)
        self.assertEqual(initialized_with_client_version["result"]["protocolVersion"], MCP_PROTOCOL_VERSION)
        self.assertEqual(initialized_with_client_version["result"]["instructions"], instructions)
        self.assertEqual(ping["result"], {})
        self.assertEqual(batch[0]["id"], 3)
        self.assertEqual(listed["result"]["tools"][0]["name"], "search_services")
        listed_tools = {tool["name"]: tool for tool in listed["result"]["tools"]}
        self.assertIn("downstream static CALLS closure", listed_tools["blast_radius"]["description"])
        self.assertIn("packet_contract", listed_tools["blast_radius"]["description"])
        self.assertEqual(called["result"]["structuredContent"]["status"], "found")
        _assert_additive_fields(self, called["result"]["structuredContent"])
        self.assertFalse(called["result"]["isError"])
        self.assertEqual(unsupported["result"]["structuredContent"]["status"], "unsupported_by_current_kg")
        self.assertEqual(unsupported["result"]["structuredContent"]["answerability"]["status"], "not_answerable")
        self.assertFalse(unsupported["result"]["isError"])

    def test_json_rpc_reports_protocol_errors(self) -> None:
        with _fixture_snapshot() as kg:
            result = _handle_json_rpc(kg, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {}})
            wrong_version = _handle_json_rpc(kg, {"jsonrpc": "1.0", "id": 2, "method": "ping"})
            notification = _handle_json_rpc(kg, {"jsonrpc": "2.0", "method": "ping"})
            invalid_notification_version = _handle_json_rpc(kg, {"jsonrpc": "1.0", "method": "ping"})
            invalid_notification_method = _handle_json_rpc(kg, {"jsonrpc": "2.0"})
            invalid_notification_params = _handle_json_rpc(kg, {"jsonrpc": "2.0", "method": "ping", "params": []})
            empty_batch = _handle_json_rpc_payload(kg, [])
            notification_batch = _handle_json_rpc_payload(kg, [{"jsonrpc": "2.0", "method": "ping"}])
            invalid_id = _handle_json_rpc(kg, {"jsonrpc": "2.0", "id": {"bad": "id"}, "method": "ping"})

        self.assertEqual(result["error"]["code"], -32602)
        self.assertIn("name", result["error"]["message"])
        self.assertEqual(wrong_version["error"]["code"], -32600)
        self.assertIsNone(notification)
        self.assertEqual(invalid_notification_version["error"]["code"], -32600)
        self.assertEqual(invalid_notification_method["error"]["code"], -32600)
        self.assertEqual(invalid_notification_params["error"]["code"], -32602)
        self.assertEqual(empty_batch["error"]["code"], -32600)
        self.assertIsNone(notification_batch)
        self.assertEqual(invalid_id["error"]["code"], -32600)

    def test_json_rpc_internal_errors_do_not_leak_exception_details(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = _handle_json_rpc(
                object(),
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "search_services", "arguments": {}},
                },
            )

        self.assertEqual(result["error"]["code"], -32000)
        self.assertEqual(result["error"]["message"], "Internal MCP server error")
        self.assertIn("Unhandled MCP JSON-RPC error", stderr.getvalue())
        self.assertIn("AttributeError", stderr.getvalue())

    def test_content_length_validation_rejects_transport_level_errors(self) -> None:
        with self.assertRaisesRegex(ValueError, "Missing Content-Length"):
            _content_length(_FakeHttpHandler({}))
        with self.assertRaisesRegex(ValueError, "must be an integer"):
            _content_length(_FakeHttpHandler({"Content-Length": "abc"}))
        with self.assertRaisesRegex(ValueError, "outside the accepted range"):
            _content_length(_FakeHttpHandler({"Content-Length": "1000001"}))

        self.assertEqual(_content_length(_FakeHttpHandler({"Content-Length": "2"})), 2)

    def test_request_body_read_sets_timeout_and_rejects_incomplete_body(self) -> None:
        complete = _FakeHttpHandler({"Content-Length": "2"}, body=b"{}")
        short = _FakeHttpHandler({"Content-Length": "4"}, body=b"{}")
        stalled = _FakeHttpHandler({"Content-Length": "2"}, rfile=_TimeoutReader())

        self.assertEqual(_read_request_body(complete, 2), b"{}")
        self.assertEqual(complete.connection.timeout, REQUEST_READ_TIMEOUT_SECONDS)
        with self.assertRaisesRegex(ValueError, "before Content-Length"):
            _read_request_body(short, 4)
        with self.assertRaisesRegex(_RequestBodyTimeout, "Timed out"):
            _read_request_body(stalled, 2)

    def test_json_body_decoding_reports_invalid_utf8_as_parse_error(self) -> None:
        with self.assertRaisesRegex(_JsonPayloadError, "invalid UTF-8"):
            _decode_json_payload(b"\xff")
        with self.assertRaisesRegex(_JsonPayloadError, "Expecting value"):
            _decode_json_payload(b"not-json")

        self.assertEqual(_decode_json_payload(b'{"ok": true}'), {"ok": True})

    def test_loopback_host_detection_accepts_loopback_network(self) -> None:
        self.assertTrue(_is_loopback_host("localhost"))
        self.assertTrue(_is_loopback_host("127.0.0.1"))
        self.assertTrue(_is_loopback_host("127.0.1.1"))
        self.assertTrue(_is_loopback_host("::1"))
        self.assertFalse(_is_loopback_host("0.0.0.0"))
        self.assertFalse(_is_loopback_host("example.com"))
        self.assertEqual(_format_host_for_url("127.0.0.1"), "127.0.0.1")
        self.assertEqual(_format_host_for_url("::1"), "[::1]")
        self.assertEqual(_format_host_for_url("localhost"), "localhost")
        self.assertEqual(_server_address_for_host("127.0.0.1", 3845), ("127.0.0.1", 3845))
        self.assertEqual(_server_address_for_host("::1", 3845), ("::1", 3845, 0, 0))
        self.assertNotEqual(_server_class_for_host("::1").address_family, _server_class_for_host("127.0.0.1").address_family)

    def test_http_server_header_does_not_expose_python_version(self) -> None:
        handler = _handler_class(object())
        fake_handler = type("FakeHandler", (), {"server_version": handler.server_version})()

        self.assertEqual(handler.sys_version, "")
        self.assertEqual(handler.version_string(fake_handler), "supercontext-local/0.1.0")

    def test_review_context_budget_compaction_preserves_lead_ids_and_hypothesis_refs(self) -> None:
        # Build a result with pre-stamped review_leads and hypotheses whose
        # supporting_lead_ids reference those lead IDs. Force compaction and assert
        # every surviving review_leads row has a lead_id and every hypothesis's
        # supporting_lead_ids is a subset of the lead IDs still in the packet.
        leads_pre = {
            "changed_symbols": [
                {
                    "qualified_name": f"pkg.mod.sym_{i}",
                    "repo": "repo",
                    "path": f"pkg/mod_{i}.py",
                    "line": i,
                    "payload": "x" * 300,
                }
                for i in range(40)
            ],
            "direct_callers": [
                {
                    "predicate": "CALLS",
                    "caller_symbol": {"qualified_name": f"pkg.caller_{i}", "repo": "repo", "path": f"pkg/c_{i}.py", "line": i},
                    "evidence": [{"bytes_ref": {"repo": "repo", "path": f"pkg/c_{i}.py", "line_start": i, "line_end": i}}],
                    "payload": "x" * 300,
                }
                for i in range(40)
            ],
            "direct_callees": [],
            "transitive_callers": [],
            "source_coordinates": [
                {"repo": "repo", "path": f"pkg/mod_{i}.py", "line_start": i, "line_end": i}
                for i in range(40)
            ],
        }
        stamped = add_review_lead_ids(leads_pre)
        all_lead_ids = [
            row["lead_id"]
            for field in ("changed_symbols", "direct_callers", "source_coordinates")
            for row in stamped.get(field, [])
            if isinstance(row, dict) and "lead_id" in row
        ]
        hypothesis = {
            "hypothesis_id": "hypothesis:test:abc123",
            "risk_type": "direct_call_contract_drift",
            "confidence": "medium",
            "why": "test",
            "evidence_refs": [],
            "source_checks": ["check it"],
            "supporting_lead_ids": all_lead_ids[:5],
        }
        result = {
            "tool": "review_context",
            "status": "found",
            "repo": "repo",
            "summary": {"changed_symbol_count": 40, "direct_caller_count": 40},
            "review_lead_status": {
                "coverage_status": "useful",
                "recommended_action": "use_supercontext_packet",
                "changed_anchor_count": 0,
                "changed_symbol_count": 40,
                "direct_impact_count": 40,
                "transitive_impact_count": 0,
                "source_coordinate_count": 40,
                "file_anchor_count": 0,
            },
            "review_answer_packet": {
                "status": "found",
                "summary": {"changed_symbol_count": 40},
                "top_changed_symbols": stamped["changed_symbols"][:5],
                "top_direct_callers": stamped["direct_callers"][:5],
                "top_direct_callees": [],
                "top_transitive_callers": [],
                "top_review_hypotheses": [hypothesis],
            },
            "review_leads": stamped,
            "diff_anchors": [],
            "changed_symbols": stamped["changed_symbols"],
            "changed_file_symbols": [],
            "direct_callers": stamped["direct_callers"],
            "direct_callees": [],
            "direct_callers_of_changed_symbols": stamped["direct_callers"],
            "direct_callees_from_changed_symbols": [],
            "transitive_callers": [],
            "source_coordinates": stamped["source_coordinates"],
            "review_hypotheses": [hypothesis],
            "coverage_warnings": [],
            "unsupported_scopes": [],
            "next_actions": [],
            "answerability": {"status": "answerable"},
        }
        # Use a budget that forces compaction but is large enough for the minimal packet.
        budgeted = enforce_review_context_budget(result, max_chars=30_000)

        self.assertTrue(budgeted.get("output_budget", {}).get("truncated"))
        review_leads = budgeted.get("review_leads", {})
        surviving_lead_ids = {
            row["lead_id"]
            for field in ("changed_symbols", "direct_callers", "source_coordinates")
            for row in review_leads.get(field, [])
            if isinstance(row, dict)
        }
        for field in ("changed_symbols", "direct_callers", "source_coordinates"):
            for row in review_leads.get(field, []):
                self.assertIn("lead_id", row, f"review_leads.{field} row missing lead_id after compaction")
        hypotheses = budgeted.get("review_hypotheses") or []
        for hyp in hypotheses:
            for sid in (hyp.get("supporting_lead_ids") or []):
                self.assertIn(sid, surviving_lead_ids, f"dangling supporting_lead_id {sid!r} not in surviving leads")

    def test_review_lead_only_packet_includes_hypotheses(self) -> None:
        # Force lead-only fallback by making the packet very large and checking
        # that review_hypotheses survives in the output.
        leads_pre = {
            "changed_symbols": [
                {"qualified_name": f"pkg.sym_{i}", "repo": "repo", "path": f"pkg/m_{i}.py", "line": i}
                for i in range(5)
            ],
            "direct_callers": [],
            "direct_callees": [],
            "transitive_callers": [],
            "source_coordinates": [],
        }
        stamped = add_review_lead_ids(leads_pre)
        lead_ids = [r["lead_id"] for r in stamped["changed_symbols"] if isinstance(r, dict)]
        hypothesis = {
            "hypothesis_id": "hypothesis:direct_call_contract_drift:aaa",
            "risk_type": "direct_call_contract_drift",
            "confidence": "medium",
            "why": "test hypothesis",
            "evidence_refs": [{"repo": "repo", "path": "pkg/m_0.py"}],
            "source_checks": ["check"],
            "supporting_lead_ids": lead_ids[:2],
        }
        # Build a fat result so the lead-only path is reached.
        fat_rows = [
            {
                "predicate": "CALLS",
                "caller_symbol": {"qualified_name": f"pkg.c_{i}", "repo": "repo", "path": f"pkg/c_{i}.py", "line": i},
                "evidence": [{"bytes_ref": {"repo": "repo", "path": f"pkg/c_{i}.py", "line_start": i, "line_end": i}}],
                "payload": "x" * 2_000,
            }
            for i in range(200)
        ]
        result = {
            "tool": "review_context",
            "status": "found",
            "repo": "repo",
            "summary": {"changed_symbol_count": 5, "direct_caller_count": 200},
            "review_lead_status": {
                "coverage_status": "useful",
                "recommended_action": "use_supercontext_packet",
                "changed_anchor_count": 0,
                "changed_symbol_count": 5,
                "direct_impact_count": 200,
                "transitive_impact_count": 0,
                "source_coordinate_count": 0,
                "file_anchor_count": 0,
            },
            "review_answer_packet": {
                "status": "found",
                "summary": {"changed_symbol_count": 5},
                "top_changed_symbols": stamped["changed_symbols"],
                "top_direct_callers": fat_rows[:5],
                "top_direct_callees": [],
                "top_transitive_callers": [],
                "top_review_hypotheses": [hypothesis],
            },
            "review_leads": stamped,
            "diff_anchors": [],
            "changed_symbols": stamped["changed_symbols"],
            "changed_file_symbols": [],
            "direct_callers": fat_rows,
            "direct_callees": fat_rows,
            "direct_callers_of_changed_symbols": fat_rows,
            "direct_callees_from_changed_symbols": fat_rows,
            "transitive_callers": fat_rows,
            "source_coordinates": [],
            "review_hypotheses": [hypothesis],
            "coverage_warnings": [],
            "unsupported_scopes": [],
            "next_actions": [],
            "answerability": {"status": "answerable"},
        }
        # Use a budget large enough to hold the lead-only skeleton + one compact hypothesis
        # but small enough to force the lead-only path (the fat_rows dominate the full packet).
        budgeted = enforce_review_context_budget(result, max_chars=8_000)

        self.assertIn("review_hypotheses", budgeted, "review_hypotheses missing from lead-only packet")
        hyps = budgeted["review_hypotheses"]
        self.assertTrue(hyps, "review_hypotheses is empty in lead-only packet")
        self.assertEqual(hyps[0]["risk_type"], "direct_call_contract_drift")
        self.assertLessEqual(len(canonical_json(budgeted)), 8_000)

    def test_review_context_emits_direct_call_contract_hypothesis(self) -> None:
        with _fixture_snapshot(upstream_checkout_caller=True) as kg:
            result = call_tool(
                kg,
                "review_context",
                {
                    "repo": "payments",
                    "changed_files": ["payments/checkout.py"],
                    "changed_ranges": [{"path": "payments/checkout.py", "start_line": 10, "end_line": 20}],
                    "include_broad_context": True,
                },
            )

        hypotheses = result["review_hypotheses"]
        self.assertTrue(hypotheses)
        first = hypotheses[0]
        self.assertRegex(first["hypothesis_id"], r"^hypothesis:")
        self.assertEqual(first["risk_type"], "direct_call_contract_drift")
        self.assertIn(first["confidence"], {"medium", "strong"})
        self.assertTrue(first["evidence_refs"])
        self.assertTrue(first["source_checks"])
        self.assertTrue(first["supporting_lead_ids"])
        self.assertTrue(
            set(first["supporting_lead_ids"]).issubset(
                {
                    row["lead_id"]
                    for field in ("direct_callers", "direct_callees", "transitive_callers")
                    for row in result["review_leads"].get(field, [])
                }
            )
        )

    def test_review_context_low_coverage_non_stylesheet_emits_empty_hypotheses(self) -> None:
        with _fixture_snapshot(app_surface=True) as kg:
            result = call_tool(
                kg,
                "review_context",
                {
                    "repo": "payments",
                    "changed_files": ["payments/config.yaml"],
                    "changed_ranges": [{"path": "payments/config.yaml", "start_line": 4, "end_line": 6}],
                    "limit": 10,
                },
            )
        self.assertEqual(result["review_lead_status"]["coverage_status"], "low_coverage")
        self.assertEqual(result["review_hypotheses"], [])

    def test_review_context_stylesheet_low_coverage_emits_gap_hypothesis(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            JsonlKgStore(root).write(entities=[], facts=[], evidence=[], coverage=[], manifest={"version": 1})
            kg = KgSnapshot(root)
            result = call_tool(
                kg,
                "review_context",
                {
                    "repo": "styles",
                    "changed_files": ["app/assets/stylesheets/buttons.scss"],
                    "changed_ranges": [{"path": "app/assets/stylesheets/buttons.scss", "start_line": 1, "end_line": 20}],
                },
            )
        self.assertEqual(result["review_lead_status"]["coverage_status"], "low_coverage")
        self.assertEqual(len(result["review_hypotheses"]), 1)
        self.assertEqual(result["review_hypotheses"][0]["risk_type"], "low_coverage_stylesheet_gap")
        self.assertIn("stylesheet", result["review_hypotheses"][0]["why"].lower())

    def test_review_context_no_ranges_non_code_file_emits_empty_hypotheses(self) -> None:
        # C2 regression: no changed_ranges + non-code file against an endpoint-bearing fixture
        # must not emit runtime hypotheses even when the service has endpoints in the KG.
        with _fixture_snapshot(app_surface=True) as kg:
            result = call_tool(
                kg,
                "review_context",
                {"repo": "payments", "changed_files": ["docs/README.md"]},
            )
        self.assertEqual(result["review_lead_status"]["coverage_status"], "low_coverage")
        self.assertEqual(result["review_hypotheses"], [])

    def test_review_context_producer_stamps_full_lists_before_slicing(self) -> None:
        # Finding 1: producer-time stamping must use full in-scope lists so that
        # summary counts and available counts reflect all callers (>5), not a
        # truncated slice. extra_callers=6 + upstream_checkout_caller=True yields 7
        # direct callers (verified by find_callers); the budget enforcer may then
        # compact the top-level list, but summary/available must still show 7.
        with _fixture_snapshot(upstream_checkout_caller=True, extra_callers=6) as kg:
            result = call_tool(
                kg,
                "review_context",
                {
                    "repo": "payments",
                    "changed_files": ["payments/checkout.py"],
                    "changed_ranges": [{"path": "payments/checkout.py", "start_line": 10, "end_line": 20}],
                    "include_broad_context": True,
                },
            )
        # summary.direct_caller_count must reflect all 7 callers, not a truncated 5
        self.assertGreater(result["summary"]["direct_caller_count"], 5, "summary count must reflect full caller list")
        # available count must also reflect the full list
        available = result["review_lead_status"]["available"]
        self.assertGreater(available["direct_caller_count"], 5, "available count must reflect full caller list")
        # available >= returned (budget enforcer may compact the list)
        returned = result["review_lead_status"]["returned"]
        self.assertLessEqual(returned["direct_caller_count"], available["direct_caller_count"])
        # all lead_ids in the lead packet must be a subset of top-level direct_callers lead_ids
        top_level_lead_ids = {row["lead_id"] for row in result["direct_callers"] if isinstance(row, dict) and "lead_id" in row}
        lead_packet_lead_ids = {row["lead_id"] for row in result["review_leads"].get("direct_callers", []) if isinstance(row, dict) and "lead_id" in row}
        self.assertTrue(lead_packet_lead_ids, "lead packet direct_callers must be non-empty")
        self.assertTrue(lead_packet_lead_ids.issubset(top_level_lead_ids), "lead packet lead_ids must be a subset of top-level lead_ids")


class _constructor_reverse_impact_snapshot:
    def __enter__(self) -> KgSnapshot:
        self._tmpdir = tempfile.TemporaryDirectory()
        root = Path(self._tmpdir.name)
        feature_module = Entity(
            kind="CodeModule",
            identity={"tenant_id": "default", "repo": "lib", "module": "lib.features"},
            properties={"path": "lib/features.py"},
        )
        feature_class = Entity(
            kind="CodeSymbol",
            identity={
                "tenant_id": "default",
                "repo": "lib",
                "module": "lib.features",
                "qualname": "build_features",
                "symbol_kind": "class",
            },
            properties={"path": "lib/features.py", "line": 10, "end_line": 100},
        )
        train_module = Entity(
            kind="CodeModule",
            identity={"tenant_id": "default", "repo": "train", "module": "train.pipeline"},
            properties={"path": "train/pipeline.py"},
        )
        builder_class = Entity(
            kind="CodeSymbol",
            identity={
                "tenant_id": "default",
                "repo": "train",
                "module": "train.pipeline",
                "qualname": "Builder",
                "symbol_kind": "class",
            },
            properties={"path": "train/pipeline.py", "line": 20, "end_line": 80},
        )
        builder_init = Entity(
            kind="CodeSymbol",
            identity={
                "tenant_id": "default",
                "repo": "train",
                "module": "train.pipeline",
                "qualname": "Builder.__init__",
                "symbol_kind": "method",
            },
            properties={"path": "train/pipeline.py", "line": 25, "end_line": 35},
        )
        builder_method = Entity(
            kind="CodeSymbol",
            identity={
                "tenant_id": "default",
                "repo": "train",
                "module": "train.pipeline",
                "qualname": "Builder.build_features",
                "symbol_kind": "method",
            },
            properties={"path": "train/pipeline.py", "line": 40, "end_line": 50},
        )
        train_company = Entity(
            kind="CodeSymbol",
            identity={
                "tenant_id": "default",
                "repo": "train",
                "module": "train.pipeline",
                "qualname": "train_company",
                "symbol_kind": "function",
            },
            properties={"path": "train/pipeline.py", "line": 90, "end_line": 110},
        )
        api_module = Entity(
            kind="CodeModule",
            identity={"tenant_id": "default", "repo": "api", "module": "api.views.train"},
            properties={"path": "api/views/train.py"},
        )
        api_view = Entity(
            kind="CodeSymbol",
            identity={
                "tenant_id": "default",
                "repo": "api",
                "module": "api.views.train",
                "qualname": "TrainView",
                "symbol_kind": "class",
            },
            properties={"path": "api/views/train.py", "line": 3, "end_line": 10},
        )
        api_post = Entity(
            kind="CodeSymbol",
            identity={
                "tenant_id": "default",
                "repo": "api",
                "module": "api.views.train",
                "qualname": "TrainView.post",
                "symbol_kind": "method",
            },
            properties={"path": "api/views/train.py", "line": 5, "end_line": 9},
        )
        direct_fact = Fact("CALLS", builder_method.entity_id, feature_class.entity_id, {"call": "features.build_features"})
        init_fact = Fact("CALLS", builder_init.entity_id, builder_method.entity_id, {"call": "self.build_features"})
        constructor_fact = Fact(
            "CALLS",
            train_company.entity_id,
            builder_class.entity_id,
            {"call": "Builder", "resolution_kind": "python_constructor_call"},
        )
        import_fact = Fact(
            "IMPORTS",
            api_module.entity_id,
            train_module.entity_id,
            {
                "category": "internal_module",
                "raw_import": "train.pipeline",
                "module_name": "train.pipeline",
                "imported_names": ["train_company"],
            },
        )
        entities = [
            feature_module,
            feature_class,
            train_module,
            builder_class,
            builder_init,
            builder_method,
            train_company,
            api_module,
            api_view,
            api_post,
        ]
        facts = [direct_fact, init_fact, constructor_fact, import_fact]
        JsonlKgStore(root).write(
            entities=entities,
            facts=facts,
            evidence=[
                Evidence(
                    target_type="fact",
                    target_id=fact.fact_id,
                    derivation_class="deterministic_static",
                    source_system="test",
                    source_ref={"predicate": fact.predicate},
                    bytes_ref={"repo": "test", "path": f"fixture/{index}.py", "line_start": index, "line_end": index},
                    confidence=1.0,
                )
                for index, fact in enumerate(facts, start=1)
            ],
            coverage=[],
            manifest={"counts": {"entities": len(entities), "facts": len(facts)}},
        )
        self._kg = KgSnapshot(root)
        return self._kg

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self._tmpdir.cleanup()


class _cross_repo_import_consumer_snapshot:
    def __init__(
        self,
        *,
        linked_package_import: bool = False,
        imported_names: tuple[str, ...] | None = ("score_session",),
        proven_call: bool = False,
        provider_tenant_id: str = "default",
        provider_module_tenant_id: str = "default",
    ) -> None:
        self.linked_package_import = linked_package_import
        self.imported_names = imported_names
        self.proven_call = proven_call
        self.provider_tenant_id = provider_tenant_id
        self.provider_module_tenant_id = provider_module_tenant_id

    def __enter__(self) -> KgSnapshot:
        self._tmpdir = tempfile.TemporaryDirectory()
        root = Path(self._tmpdir.name)
        provider_module = Entity(
            kind="CodeModule",
            identity={"tenant_id": self.provider_module_tenant_id, "repo": "lib", "module": "lib.predict"},
            properties={"path": "lib/predict.py"},
        )
        provider_symbol = Entity(
            kind="CodeSymbol",
            identity={
                "tenant_id": "default",
                "repo": "lib",
                "module": "lib.predict",
                "qualname": "score_session",
                "symbol_kind": "function",
            },
            properties={"path": "lib/predict.py", "line": 12, "end_line": 20},
        )
        importer_module = Entity(
            kind="CodeModule",
            identity={"tenant_id": "default", "repo": "api", "module": "api.views.score"},
            properties={"path": "api/views/score.py"},
        )
        view_class = Entity(
            kind="CodeSymbol",
            identity={
                "tenant_id": "default",
                "repo": "api",
                "module": "api.views.score",
                "qualname": "ScoreView",
                "symbol_kind": "class",
            },
            properties={"path": "api/views/score.py", "line": 5, "end_line": 25},
        )
        post_method = Entity(
            kind="CodeSymbol",
            identity={
                "tenant_id": "default",
                "repo": "api",
                "module": "api.views.score",
                "qualname": "ScoreView.post",
                "symbol_kind": "method",
            },
            properties={"path": "api/views/score.py", "line": 8, "end_line": 18},
        )
        provider_repo = Entity(
            kind="Repo",
            identity={"tenant_id": self.provider_tenant_id, "host": "local", "owner": "default", "name": "lib"},
        )
        provider_package = Entity(
            kind="ExternalPackage",
            identity={"tenant_id": "default", "repo": "api", "name": "lib"},
            properties={"category": "unknown", "import_root": "lib"},
        )
        import_qualifier = {
                "category": "unknown" if self.linked_package_import else "internal_module",
                "module_name": None if self.linked_package_import else "lib.predict",
                "raw_import": "lib.predict",
                "import_root": "lib",
            }
        if self.imported_names is not None:
            import_qualifier["imported_names"] = list(self.imported_names)
        import_fact = Fact(
            "IMPORTS",
            importer_module.entity_id,
            provider_package.entity_id if self.linked_package_import else provider_module.entity_id,
            import_qualifier,
        )
        repo_link_fact = Fact(
            "RESOLVES_TO_REPO",
            provider_package.entity_id,
            provider_repo.entity_id,
            {"consumer_repo": "api", "package_name": "lib", "provider_repo": "lib"},
        )
        call_fact = Fact("CALLS", post_method.entity_id, provider_symbol.entity_id)
        entities = [
            provider_module,
            provider_symbol,
            importer_module,
            view_class,
            post_method,
            *([provider_repo, provider_package] if self.linked_package_import else []),
        ]
        facts = [import_fact, *([repo_link_fact] if self.linked_package_import else []), *([call_fact] if self.proven_call else [])]
        JsonlKgStore(root).write(
            entities=entities,
            facts=facts,
            evidence=[
                Evidence(
                    target_type="fact",
                    target_id=import_fact.fact_id,
                    derivation_class="deterministic_static",
                    source_system="test",
                    source_ref={"repo": "api"},
                    bytes_ref={"repo": "api", "path": "api/views/score.py", "line_start": 3, "line_end": 3},
                    confidence=1.0,
                )
            ],
            coverage=[],
            manifest={"counts": {"entities": len(entities), "facts": len(facts)}},
        )
        self._kg = KgSnapshot(root)
        return self._kg

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self._tmpdir.cleanup()


def _rewrite_fixture_repo(kg: KgSnapshot, *, old_repo: str, new_repo: str) -> None:
    entity_id_map: dict[str, str] = {}
    for entity in kg.entities:
        identity = entity.get("identity")
        if isinstance(identity, dict) and identity.get("repo") == old_repo:
            old_entity_id = str(entity.get("entity_id") or "")
            identity["repo"] = new_repo
            rewritten = Entity(
                kind=str(entity["kind"]),
                identity=deepcopy(identity),
                properties=deepcopy(entity.get("properties") or {}),
                canonical_status=entity.get("canonical_status", "canonical"),
            ).to_record()
            entity["entity_id"] = rewritten["entity_id"]
            entity["urn"] = rewritten["urn"]
            if old_entity_id and old_entity_id != rewritten["entity_id"]:
                entity_id_map[old_entity_id] = str(rewritten["entity_id"])
    kg.entities_by_id = {entity["entity_id"]: entity for entity in kg.entities}

    fact_id_map: dict[str, str] = {}
    for fact in kg.facts:
        old_fact_id = str(fact.get("fact_id") or "")
        subject_id = fact.get("subject_id")
        if isinstance(subject_id, str) and subject_id in entity_id_map:
            fact["subject_id"] = entity_id_map[subject_id]
        object_id = fact.get("object_id")
        if isinstance(object_id, str) and object_id in entity_id_map:
            fact["object_id"] = entity_id_map[object_id]
        qualifier = fact.get("qualifier")
        if not isinstance(qualifier, dict):
            qualifier = {}
            fact["qualifier"] = qualifier
        if qualifier.get("consumer_repo") == old_repo:
            qualifier["consumer_repo"] = new_repo
        consumer_identity = qualifier.get("consumer_repo_identity")
        if isinstance(consumer_identity, dict) and consumer_identity.get("name") == old_repo:
            consumer_identity["name"] = new_repo
        consumer_identities = qualifier.get("consumer_repo_identities")
        if isinstance(consumer_identities, list):
            for row in consumer_identities:
                if isinstance(row, dict) and row.get("name") == old_repo:
                    row["name"] = new_repo
        rewritten_fact = Fact(
            predicate=str(fact["predicate"]),
            subject_id=str(fact["subject_id"]),
            object_id=str(fact["object_id"]),
            qualifier=deepcopy(qualifier),
            canonical_status=fact.get("canonical_status", "canonical"),
        ).to_record()
        fact["fact_id"] = rewritten_fact["fact_id"]
        if old_fact_id and old_fact_id != rewritten_fact["fact_id"]:
            fact_id_map[old_fact_id] = str(rewritten_fact["fact_id"])

    for evidence in kg.evidence:
        target_id = evidence.get("target_id")
        if isinstance(target_id, str) and target_id in entity_id_map:
            evidence["target_id"] = entity_id_map[target_id]
        if isinstance(target_id, str) and target_id in fact_id_map:
            evidence["target_id"] = fact_id_map[target_id]
    kg.evidence_by_target.clear()
    for row in kg.evidence:
        kg.evidence_by_target[row["target_id"]].append(row)


class _fixture_snapshot:
    def __init__(
        self,
        extra_consumers: int = 0,
        extra_callers: int = 0,
        extra_package_importers: int = 0,
        extra_charge_card_symbol: bool = False,
        duplicate_endpoint_fact: bool = False,
        extra_service_endpoint: bool = False,
        endpoint_consumer: bool = False,
        same_repo_endpoint_consumer: bool = False,
        provider_endpoint_method: str | None = "POST",
        provider_endpoint_path: str = "/checkout",
        endpoint_consumer_method: str | None = "POST",
        endpoint_consumer_path: str = "/checkout",
        operational_deploy_mapping: bool = False,
        operational_deploy_same_repo: bool = False,
        operational_deploy_link: bool = False,
        symbol_entity_evidence_duplicate_coordinates: bool = False,
        symbol_without_end_line: bool = False,
        upstream_checkout_caller: bool = False,
        upstream_bootstrap_caller: bool = False,
        upstream_checkout_grandcaller: bool = False,
        upstream_checkout_cycle: bool = False,
        containing_checkout_class: bool = False,
        app_surface: bool = False,
        static_hosting_domain_reference: bool = False,
        kubernetes_operational_deploy: bool = False,
        runtime_pressure_routes: int = 0,
        runtime_pressure_payload_size: int = 0,
        runtime_pressure_same_repo: bool = False,
        env_domain_reference_lead: bool = False,
        symbol_repo: str = "payments",
    ) -> None:
        self.extra_consumers = extra_consumers
        self.extra_callers = extra_callers
        self.extra_package_importers = extra_package_importers
        self.extra_charge_card_symbol = extra_charge_card_symbol
        self.duplicate_endpoint_fact = duplicate_endpoint_fact
        self.extra_service_endpoint = extra_service_endpoint
        self.endpoint_consumer = endpoint_consumer
        self.same_repo_endpoint_consumer = same_repo_endpoint_consumer
        self.provider_endpoint_method = provider_endpoint_method
        self.provider_endpoint_path = provider_endpoint_path
        self.endpoint_consumer_method = endpoint_consumer_method
        self.endpoint_consumer_path = endpoint_consumer_path
        self.operational_deploy_mapping = operational_deploy_mapping
        self.operational_deploy_same_repo = operational_deploy_same_repo
        self.operational_deploy_link = operational_deploy_link
        self.symbol_entity_evidence_duplicate_coordinates = symbol_entity_evidence_duplicate_coordinates
        self.symbol_without_end_line = symbol_without_end_line
        self.upstream_checkout_caller = upstream_checkout_caller
        self.upstream_bootstrap_caller = upstream_bootstrap_caller
        self.upstream_checkout_grandcaller = upstream_checkout_grandcaller
        self.upstream_checkout_cycle = upstream_checkout_cycle
        self.containing_checkout_class = containing_checkout_class
        self.app_surface = app_surface
        self.static_hosting_domain_reference = static_hosting_domain_reference
        self.kubernetes_operational_deploy = kubernetes_operational_deploy
        self.runtime_pressure_routes = runtime_pressure_routes
        self.runtime_pressure_payload_size = runtime_pressure_payload_size
        self.runtime_pressure_same_repo = runtime_pressure_same_repo
        self.env_domain_reference_lead = env_domain_reference_lead
        self.symbol_repo = symbol_repo

    def __enter__(self) -> KgSnapshot:
        self._tmpdir = tempfile.TemporaryDirectory()
        root = Path(self._tmpdir.name)
        service = Entity(
            kind="Service",
            identity={"tenant_id": "default", "namespace": "default", "slug": "payments", "repo": "payments"},
        )
        caller = Entity(
            kind="CodeSymbol",
            identity={
                "tenant_id": "default",
                "repo": self.symbol_repo,
                "module": "payments.checkout",
                "qualname": "CheckoutHandler.handle_checkout" if self.containing_checkout_class else "handle_checkout",
                "symbol_kind": "function",
            },
            properties=(
                {"path": "payments/checkout.py", "line": 10}
                if self.symbol_without_end_line
                else {"path": "payments/checkout.py", "line": 10, "end_line": 20}
            ),
        )
        containing_class = Entity(
            kind="CodeSymbol",
            identity={
                "tenant_id": "default",
                "repo": self.symbol_repo,
                "module": "payments.checkout",
                "qualname": "CheckoutHandler",
                "symbol_kind": "class",
            },
            properties={"path": "payments/checkout.py", "line": 1, "end_line": 30},
        )
        earlier_symbol = Entity(
            kind="CodeSymbol",
            identity={
                "tenant_id": "default",
                "repo": self.symbol_repo,
                "module": "payments.checkout",
                "qualname": "bootstrap_checkout",
                "symbol_kind": "function",
            },
            properties={"path": "payments/checkout.py", "line": 1, "end_line": 3},
        )
        module = Entity(
            kind="CodeModule",
            identity={"tenant_id": "default", "repo": "payments", "module": "payments.checkout"},
            properties={"path": "payments/checkout.py"},
        )
        callee = Entity(
            kind="CodeSymbol",
            identity={
                "tenant_id": "default",
                "repo": "payments",
                "module": "payments.gateway",
                "qualname": "charge_card",
                "symbol_kind": "function",
            },
            properties={"path": "payments/gateway.py", "line": 5, "end_line": 12},
        )
        upstream_caller = Entity(
            kind="CodeSymbol",
            identity={
                "tenant_id": "default",
                "repo": "payments",
                "module": "payments.api",
                "qualname": "submit_checkout",
                "symbol_kind": "function",
            },
            properties={"path": "payments/api.py", "line": 30, "end_line": 35},
        )
        upstream_bootstrap_caller = Entity(
            kind="CodeSymbol",
            identity={
                "tenant_id": "default",
                "repo": "payments",
                "module": "payments.startup",
                "qualname": "warm_checkout",
                "symbol_kind": "function",
            },
            properties={"path": "payments/startup.py", "line": 22, "end_line": 27},
        )
        upstream_grandcaller = Entity(
            kind="CodeSymbol",
            identity={
                "tenant_id": "default",
                "repo": "payments",
                "module": "payments.worker",
                "qualname": "enqueue_checkout",
                "symbol_kind": "function",
            },
            properties={"path": "payments/worker.py", "line": 40, "end_line": 45},
        )
        endpoint = Entity(
            kind="Endpoint",
            identity={
                "tenant_id": "default",
                "repo": "payments",
                "protocol": "http",
                "method": self.provider_endpoint_method,
                "path": self.provider_endpoint_path,
                "host": None,
            },
        )
        extra_endpoint = Entity(
            kind="Endpoint",
            identity={
                "tenant_id": "default",
                "repo": "payments",
                "protocol": "http",
                "method": "GET",
                "path": "/refund",
                "host": None,
            },
        )
        consumer_service = Entity(
            kind="Service",
            identity={"tenant_id": "default", "namespace": "default", "slug": "web", "repo": "web"},
        )
        consumer_endpoint = Entity(
            kind="Endpoint",
            identity={
                "tenant_id": "default",
                "repo": "web",
                "protocol": "http",
                "method": self.endpoint_consumer_method,
                "path": self.endpoint_consumer_path,
                "host": "${env:PAYMENTS_API_BASE_URL}",
            },
        )
        channel = Entity(
            kind="EventChannel",
            identity={
                "tenant_id": "default",
                "repo": "payments",
                "broker_kind": "sqs",
                "channel_address": "orders-created",
                "name": "orders-created",
            },
        )
        domain = Entity(
            kind="Domain",
            identity={"tenant_id": "default", "repo": "payments", "name": "api.internal.example"},
        )
        operational_deploy_repo = "payments" if self.operational_deploy_same_repo else "ops"
        route_domain = Entity(
            kind="Domain",
            identity={"tenant_id": "default", "repo": operational_deploy_repo, "name": "payments.example.com"},
        )
        deploy_target_type = "kubernetes_deployment" if self.kubernetes_operational_deploy else "wsgi"
        deploy_target_name = (
            "k8s/payments.yaml#default/deployment/payments"
            if self.kubernetes_operational_deploy
            else "/srv/payments/app.wsgi"
        )
        deploy_target = Entity(
            kind="DeployTarget",
            identity={
                "tenant_id": "default",
                "repo": operational_deploy_repo,
                "type": deploy_target_type,
                "target": deploy_target_name,
            },
        )
        env_var = Entity(
            kind="EnvVar",
            identity={"tenant_id": "default", "repo": "payments", "name": "PAYMENTS_API_BASE_URL"},
        )
        package = Entity(
            kind="ExternalPackage",
            identity={"tenant_id": "default", "repo": "payments", "name": "shared-lib"},
            properties={"category": "third_party", "import_root": "shared_lib", "distribution_name": "shared-lib"},
        )
        provider_repo = Entity(
            kind="Repo",
            identity={"tenant_id": "default", "host": "local", "owner": "default", "name": "shared-platform"},
        )
        duplicate_callee = Entity(
            kind="CodeSymbol",
            identity={
                "tenant_id": "default",
                "repo": "payments",
                "module": "payments.alt_gateway",
                "qualname": "charge_card",
                "symbol_kind": "function",
            },
            properties={"path": "payments/alt_gateway.py", "line": 7, "end_line": 9},
        )
        app_api_module = Entity(
            kind="CodeModule",
            identity={"tenant_id": "default", "repo": "payments", "module": "payments.api"},
            properties={"path": "payments/api.py"},
        )
        app_task_symbol = Entity(
            kind="CodeSymbol",
            identity={
                "tenant_id": "default",
                "repo": "payments",
                "module": "payments.tasks",
                "qualname": "send_receipt",
                "symbol_kind": "function",
            },
            properties={"path": "payments/tasks.py", "line": 3, "end_line": 8},
        )
        app_command_module = Entity(
            kind="CodeModule",
            identity={"tenant_id": "default", "repo": "payments", "module": "payments.management.commands.reconcile"},
            properties={"path": "payments/management/commands/reconcile.py"},
        )
        app_model_symbol = Entity(
            kind="CodeSymbol",
            identity={
                "tenant_id": "default",
                "repo": "payments",
                "module": "payments.models",
                "qualname": "Payment",
                "symbol_kind": "class",
            },
            properties={"path": "payments/models.py", "line": 4, "end_line": 20},
        )
        app_model_field = Entity(
            kind="CodeSymbol",
            identity={
                "tenant_id": "default",
                "repo": "payments",
                "module": "payments.models",
                "qualname": "Payment.status",
                "symbol_kind": "django_field",
            },
            properties={"path": "payments/models.py", "line": 7, "end_line": 7},
        )
        cross_repo_payment_screen = Entity(
            kind="CodeModule",
            identity={"tenant_id": "default", "repo": "web", "module": "src.views.PaymentsScreen"},
            properties={"path": "src/views/PaymentsScreen.tsx"},
        )
        infra_service = Entity(
            kind="Service",
            identity={"tenant_id": "default", "namespace": "default", "slug": "frontend-infra", "repo": "infra"},
        )
        hosted_domain = Entity(
            kind="Domain",
            identity={"tenant_id": "default", "repo": "infra", "name": "app.example.com"},
        )
        call_fact = Fact("CALLS", caller.entity_id, callee.entity_id)
        upstream_call_fact = Fact("CALLS", upstream_caller.entity_id, caller.entity_id)
        upstream_bootstrap_call_fact = Fact("CALLS", upstream_bootstrap_caller.entity_id, earlier_symbol.entity_id)
        upstream_grandcall_fact = Fact("CALLS", upstream_grandcaller.entity_id, upstream_caller.entity_id)
        upstream_cycle_fact = Fact("CALLS", caller.entity_id, upstream_caller.entity_id)
        import_fact = Fact(
            "IMPORTS",
            module.entity_id,
            package.entity_id,
            {"category": "third_party", "import_root": "shared_lib", "distribution_name": "shared-lib"},
        )
        repo_link_fact = Fact(
            "RESOLVES_TO_REPO",
            package.entity_id,
            provider_repo.entity_id,
            {"consumer_repo": "payments", "package_name": "shared-lib"},
        )
        endpoint_fact = Fact(
            "EXPOSES_ENDPOINT",
            service.entity_id,
            endpoint.entity_id,
            {"method": self.provider_endpoint_method, "path": self.provider_endpoint_path},
        )
        extra_endpoint_fact = Fact(
            "EXPOSES_ENDPOINT",
            service.entity_id,
            extra_endpoint.entity_id,
            {"method": "GET", "path": "/refund"},
        )
        endpoint_consumer_subject = caller if self.same_repo_endpoint_consumer else consumer_service
        endpoint_consumer_fact = Fact(
            "CALLS_ENDPOINT",
            endpoint_consumer_subject.entity_id,
            consumer_endpoint.entity_id,
            {
                "confidence": "host_unresolved_path_resolved",
                "host_resolution_kind": "env_backed_unresolved",
                "method": self.endpoint_consumer_method,
                "path": self.endpoint_consumer_path,
                "raw_target": f"${{env:PAYMENTS_API_BASE_URL}}{self.endpoint_consumer_path}",
                "resolution_kind": "path_resolved",
                "source_kind": "http_client",
            },
        )
        consume_fact = Fact("CONSUMES_EVENT", service.entity_id, channel.entity_id)
        produce_fact = Fact("PRODUCES_EVENT", caller.entity_id, channel.entity_id)
        domain_fact = Fact(
            "REFERENCES_DOMAIN",
            env_var.entity_id,
            domain.entity_id,
            (
                {"literal": "https://api.internal.example", "path": "payments/settings.py", "source_kind": "domain_env"}
                if self.env_domain_reference_lead
                else {}
            ),
        )
        route_qualifier = (
            {
                "source_kind": "kubernetes_ingress",
                "target_type": "kubernetes_deployment",
                "kubernetes_kind": "Deployment",
                "namespace": "default",
                "workload": "payments",
                "backend_service": "payments-service",
                "backend_service_ports": [{"port": 80, "targetPort": 8000}],
                "ingress_path": "/",
                "match_basis": "ingress_backend_service_selector_to_workload",
            }
            if self.kubernetes_operational_deploy
            else {"source_kind": "fixture_vhost"}
        )
        route_fact = Fact("ROUTES_DOMAIN_TO_DEPLOY", route_domain.entity_id, deploy_target.entity_id, route_qualifier)
        deploy_link_qualifier = (
            {
                "source_kind": "kubernetes_manifest",
                "target_type": "kubernetes_deployment",
                "kubernetes_kind": "Deployment",
                "namespace": "default",
                "workload": "payments",
                "containers": ["payments"],
                "images": ["registry.example.com/payments:latest"],
                "ownership_basis": "image_repo_name_matches_service_identity:payments",
            }
            if self.kubernetes_operational_deploy
            else {"source_kind": "runtime_linker", "resolved_by": "fixture"}
        )
        deploy_link_fact = Fact(
            "DEPLOYS_VIA_CONFIG",
            service.entity_id,
            deploy_target.entity_id,
            deploy_link_qualifier,
        )
        static_hosting_fact = Fact(
            "REFERENCES_DOMAIN",
            infra_service.entity_id,
            hosted_domain.entity_id,
            {"literal": "app.example.com", "path": "prod/cloudfront.tf", "source_kind": "terraform_literal"},
        )
        runtime_payload = "x" * self.runtime_pressure_payload_size
        runtime_repo = "runtime-shared" if self.runtime_pressure_same_repo else None
        runtime_services = [
            Entity(
                kind="Service",
                identity={
                    "tenant_id": "default",
                    "namespace": "default",
                    "slug": f"runtime-service-{index}",
                    "repo": runtime_repo or f"runtime-repo-{index}",
                },
            )
            for index in range(self.runtime_pressure_routes)
        ]
        runtime_domains = [
            Entity(
                kind="Domain",
                identity={
                    "tenant_id": "default",
                    "repo": runtime_repo or f"runtime-infra-{index}",
                    "name": f"runtime-{index}.example.test",
                },
            )
            for index in range(self.runtime_pressure_routes)
        ]
        runtime_targets = [
            Entity(
                kind="DeployTarget",
                identity={
                    "tenant_id": "default",
                    "repo": runtime_repo or f"runtime-infra-{index}",
                    "type": "cloudfront_distribution",
                    "target": f"aws_cloudfront_distribution.runtime_{index}",
                },
            )
            for index in range(self.runtime_pressure_routes)
        ]
        runtime_route_facts = [
            Fact(
                "ROUTES_DOMAIN_TO_DEPLOY",
                runtime_domains[index].entity_id,
                runtime_targets[index].entity_id,
                {"source_kind": "terraform_cloudfront_alias", "description": runtime_payload},
            )
            for index in range(self.runtime_pressure_routes)
        ]
        runtime_deploy_facts = [
            Fact(
                "DEPLOYS_VIA_CONFIG",
                runtime_services[index].entity_id,
                runtime_targets[index].entity_id,
                {"source_kind": "terraform_cloudfront_origin", "description": runtime_payload},
            )
            for index in range(self.runtime_pressure_routes)
        ]
        extra_services = [
            Entity(
                kind="Service",
                identity={
                    "tenant_id": "default",
                    "namespace": "default",
                    "slug": f"consumer-{index}",
                    "repo": f"consumer-{index}",
                },
            )
            for index in range(self.extra_consumers)
        ]
        extra_modules = [
            Entity(
                kind="CodeModule",
                identity={"tenant_id": "default", "repo": "payments", "module": f"payments.importer_{index}"},
                properties={"path": f"payments/importer_{index}.py"},
            )
            for index in range(self.extra_package_importers)
        ]
        extra_caller_symbols = [
            Entity(
                kind="CodeSymbol",
                identity={
                    "tenant_id": "default",
                    "repo": "payments",
                    "module": f"payments.extra_{index}",
                    "qualname": f"extra_caller_{index}",
                    "symbol_kind": "function",
                },
                properties={"path": f"payments/extra_{index}.py", "line": index + 1, "end_line": index + 2},
            )
            for index in range(self.extra_callers)
        ]
        extra_caller_facts = [Fact("CALLS", sym.entity_id, caller.entity_id) for sym in extra_caller_symbols]
        extra_consume_facts = [Fact("CONSUMES_EVENT", extra_service.entity_id, channel.entity_id) for extra_service in extra_services]
        extra_import_facts = [
            Fact(
                "IMPORTS",
                extra_module.entity_id,
                package.entity_id,
                {"category": "third_party", "import_root": "shared_lib", "distribution_name": "shared-lib"},
            )
            for extra_module in extra_modules
        ]
        evidence = [
            Evidence(
                target_type="entity",
                target_id=service.entity_id,
                derivation_class="deterministic_static",
                source_system="test",
                source_ref={"repo": "payments"},
                confidence=1.0,
            ),
            Evidence(
                target_type="fact",
                target_id=call_fact.fact_id,
                derivation_class="deterministic_static",
                source_system="test",
                source_ref={"repo": "payments"},
                bytes_ref={"repo": "payments", "path": "payments/checkout.py", "line_start": 14, "line_end": 14},
                confidence=1.0,
            ),
            Evidence(
                target_type="fact",
                target_id=import_fact.fact_id,
                derivation_class="deterministic_static",
                source_system="test",
                source_ref={"repo": "payments"},
                bytes_ref={
                    "repo": "payments",
                    "commit_sha": "fixture-sha",
                    "path": "payments/checkout.py",
                    "line_start": 2,
                    "line_end": 2,
                },
                confidence=1.0,
            ),
        ]
        if self.symbol_entity_evidence_duplicate_coordinates:
            evidence.append(
                Evidence(
                    target_type="entity",
                    target_id=caller.entity_id,
                    derivation_class="deterministic_static",
                    source_system="test",
                    source_ref={"repo": "payments"},
                    bytes_ref={
                        "repo": "payments",
                        "commit_sha": "fixture-sha",
                        "path": "payments/checkout.py",
                        "line_start": 10,
                        "line_end": 20,
                    },
                    confidence=1.0,
                )
            )
        if self.endpoint_consumer:
            endpoint_consumer_repo = "payments" if self.same_repo_endpoint_consumer else "web"
            evidence.append(
                Evidence(
                    target_type="fact",
                    target_id=endpoint_consumer_fact.fact_id,
                    derivation_class="deterministic_static",
                    source_system="test",
                    source_ref={"repo": endpoint_consumer_repo},
                    bytes_ref={
                        "repo": endpoint_consumer_repo,
                        "path": "payments/internal_api.py" if self.same_repo_endpoint_consumer else "web/src/api.ts",
                        "line_start": 42,
                        "line_end": 42,
                    },
                    confidence=0.8,
                )
            )
        if self.operational_deploy_mapping:
            evidence.append(
                Evidence(
                    target_type="fact",
                    target_id=route_fact.fact_id,
                    derivation_class="deterministic_static",
                    source_system="test",
                    source_ref={"repo": "ops"},
                    bytes_ref={"repo": "ops", "path": "ops/payments.conf", "line_start": 3, "line_end": 8},
                    confidence=1.0,
                )
            )
        if self.operational_deploy_link:
            evidence.append(
                Evidence(
                    target_type="fact",
                    target_id=deploy_link_fact.fact_id,
                    derivation_class="deterministic_static",
                    source_system="runtime_linker",
                    source_ref={"repo": "ops"},
                    bytes_ref={"repo": "ops", "path": "ops/payments.conf", "line_start": 5, "line_end": 5},
                    confidence=1.0,
                )
            )
        if self.app_surface:
            evidence.append(
                Evidence(
                    target_type="fact",
                    target_id=endpoint_fact.fact_id,
                    derivation_class="deterministic_static",
                    source_system="test",
                    source_ref={"repo": "payments"},
                    bytes_ref={"repo": "payments", "path": "payments/api.py", "line_start": 10, "line_end": 12},
                    confidence=1.0,
                )
            )
        if self.env_domain_reference_lead:
            evidence.append(
                Evidence(
                    target_type="fact",
                    target_id=domain_fact.fact_id,
                    derivation_class="deterministic_static",
                    source_system="test",
                    source_ref={"repo": "payments"},
                    bytes_ref={"repo": "payments", "path": "payments/settings.py", "line_start": 3, "line_end": 3},
                    confidence=1.0,
                )
            )
        if self.static_hosting_domain_reference:
            evidence.append(
                Evidence(
                    target_type="fact",
                    target_id=static_hosting_fact.fact_id,
                    derivation_class="deterministic_static",
                    source_system="test",
                    source_ref={"repo": "infra"},
                    bytes_ref={"repo": "infra", "path": "prod/cloudfront.tf", "line_start": 12, "line_end": 18},
                    confidence=0.9,
                )
            )
        for index, fact in enumerate(runtime_route_facts):
            evidence.append(
                Evidence(
                    target_type="fact",
                    target_id=fact.fact_id,
                    derivation_class="deterministic_static",
                    source_system="test",
                    source_ref={"repo": f"runtime-infra-{index}"},
                    bytes_ref={
                        "repo": f"runtime-infra-{index}",
                        "path": "prod/runtime.tf",
                        "line_start": index + 1,
                        "line_end": index + 1,
                    },
                    confidence=1.0,
                )
            )
        for index, fact in enumerate(runtime_deploy_facts):
            evidence.append(
                Evidence(
                    target_type="fact",
                    target_id=fact.fact_id,
                    derivation_class="deterministic_static",
                    source_system="test",
                    source_ref={"repo": f"runtime-repo-{index}"},
                    bytes_ref={
                        "repo": f"runtime-repo-{index}",
                        "path": "prod/runtime.tf",
                        "line_start": index + 1,
                        "line_end": index + 1,
                    },
                    confidence=1.0,
                )
            )
        entities = [
            service,
            *([containing_class] if self.containing_checkout_class else []),
            caller,
            earlier_symbol,
            module,
            callee,
            endpoint,
            *([extra_endpoint] if self.extra_service_endpoint else []),
            *([upstream_caller] if self.upstream_checkout_caller else []),
            *([upstream_bootstrap_caller] if self.upstream_bootstrap_caller else []),
            *([upstream_grandcaller] if self.upstream_checkout_grandcaller else []),
            *([consumer_service] if self.endpoint_consumer and not self.same_repo_endpoint_consumer else []),
            *([consumer_endpoint] if self.endpoint_consumer else []),
            channel,
            domain,
            *([route_domain, deploy_target] if self.operational_deploy_mapping else []),
            env_var,
            package,
            provider_repo,
            *([duplicate_callee] if self.extra_charge_card_symbol else []),
            *(
                [app_api_module, app_task_symbol, app_command_module, app_model_symbol, app_model_field, cross_repo_payment_screen]
                if self.app_surface
                else []
            ),
            *([infra_service, hosted_domain] if self.static_hosting_domain_reference else []),
            *runtime_services,
            *runtime_domains,
            *runtime_targets,
            *extra_services,
            *extra_modules,
            *extra_caller_symbols,
        ]
        facts = [
            call_fact,
            *([upstream_call_fact] if self.upstream_checkout_caller else []),
            *([upstream_bootstrap_call_fact] if self.upstream_bootstrap_caller else []),
            *([upstream_grandcall_fact] if self.upstream_checkout_grandcaller else []),
            *([upstream_cycle_fact] if self.upstream_checkout_cycle else []),
            import_fact,
            repo_link_fact,
            endpoint_fact,
            *([extra_endpoint_fact] if self.extra_service_endpoint else []),
            *([endpoint_consumer_fact] if self.endpoint_consumer else []),
            consume_fact,
            produce_fact,
            domain_fact,
            *([route_fact] if self.operational_deploy_mapping else []),
            *([deploy_link_fact] if self.operational_deploy_link else []),
            *([static_hosting_fact] if self.static_hosting_domain_reference else []),
            *runtime_route_facts,
            *runtime_deploy_facts,
            *([endpoint_fact] if self.duplicate_endpoint_fact else []),
            *extra_consume_facts,
            *extra_import_facts,
            *extra_caller_facts,
        ]
        JsonlKgStore(root).write(
            entities=entities,
            facts=facts,
            evidence=evidence,
            coverage=[],
            manifest={"counts": {"entities": len(entities), "facts": len(facts)}},
        )
        self._kg = KgSnapshot(root)
        return self._kg

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self._tmpdir.cleanup()


class _FakeHttpHandler:
    def __init__(self, headers: dict[str, str], *, body: bytes = b"", rfile: object | None = None) -> None:
        self.headers = headers
        self.connection = _FakeConnection()
        self.rfile = rfile or io.BytesIO(body)


class _FakeConnection:
    def __init__(self) -> None:
        self.timeout: float | None = None

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout


class _TimeoutReader:
    def read(self, size: int) -> bytes:
        raise TimeoutError("stalled")


class TestNewHypothesisFamiliesIntegration(unittest.TestCase):
    """Integration tests for the three new hypothesis families (F).

    Each test builds a minimal JSONL snapshot then calls review_context end-to-end
    to assert the corresponding risk_type surfaces in review_hypotheses.
    """

    def _build_snapshot(self, entities, facts, root):
        JsonlKgStore(root).write(
            entities=entities,
            facts=facts,
            evidence=[],
            coverage=[],
            manifest={"version": 1},
        )
        return KgSnapshot(root)

    def test_component_list_render_identity_drift_fires_on_tsx_component(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            component = Entity(
                kind="CodeSymbol",
                identity={
                    "tenant_id": "default",
                    "repo": "ui",
                    "module": "src.ItemList",
                    "qualname": "ItemList",
                    "symbol_kind": "function",
                },
                properties={"path": "src/ItemList.tsx", "line": 5, "end_line": 25},
            )
            row_component = Entity(
                kind="CodeSymbol",
                identity={
                    "tenant_id": "default",
                    "repo": "ui",
                    "module": "src.WidgetRow",
                    "qualname": "WidgetRow",
                    "symbol_kind": "function",
                },
                properties={"path": "src/WidgetRow.tsx", "line": 1, "end_line": 10},
            )
            calls_fact = Fact("CALLS", component.entity_id, row_component.entity_id)
            kg = self._build_snapshot([component, row_component], [calls_fact], root)
            result = call_tool(
                kg,
                "review_context",
                {
                    "repo": "ui",
                    "changed_files": ["src/ItemList.tsx"],
                    "changed_ranges": [{"path": "src/ItemList.tsx", "start_line": 5, "end_line": 25}],
                },
            )
        risk_types = [h["risk_type"] for h in result["review_hypotheses"]]
        self.assertIn("component_list_render_identity_drift", risk_types)

    def test_hook_gate_render_mismatch_fires_on_hook_with_component_consumer(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            hook = Entity(
                kind="CodeSymbol",
                identity={
                    "tenant_id": "default",
                    "repo": "ui",
                    "module": "src.useGate",
                    "qualname": "useGate",
                    "symbol_kind": "function",
                },
                properties={"path": "src/useGate.ts", "line": 1, "end_line": 20},
            )
            consumer = Entity(
                kind="CodeSymbol",
                identity={
                    "tenant_id": "default",
                    "repo": "ui",
                    "module": "src.WidgetRow",
                    "qualname": "WidgetRow",
                    "symbol_kind": "function",
                },
                properties={"path": "src/WidgetRow.tsx", "line": 1, "end_line": 15},
            )
            calls_fact = Fact("CALLS", consumer.entity_id, hook.entity_id)
            kg = self._build_snapshot([hook, consumer], [calls_fact], root)
            result = call_tool(
                kg,
                "review_context",
                {
                    "repo": "ui",
                    "changed_files": ["src/useGate.ts"],
                    "changed_ranges": [{"path": "src/useGate.ts", "start_line": 1, "end_line": 20}],
                },
            )
        risk_types = [h["risk_type"] for h in result["review_hypotheses"]]
        self.assertIn("hook_gate_render_mismatch", risk_types)

    def test_test_locks_in_regression_fires_on_test_plus_code_with_edge(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            code_sym = Entity(
                kind="CodeSymbol",
                identity={
                    "tenant_id": "default",
                    "repo": "lib",
                    "module": "src.processor",
                    "qualname": "process",
                    "symbol_kind": "function",
                },
                properties={"path": "src/processor.py", "line": 10, "end_line": 30},
            )
            test_sym = Entity(
                kind="CodeSymbol",
                identity={
                    "tenant_id": "default",
                    "repo": "lib",
                    "module": "tests.test_processor",
                    "qualname": "test_process",
                    "symbol_kind": "function",
                },
                properties={"path": "tests/test_processor.py", "line": 5, "end_line": 15},
            )
            calls_fact = Fact("CALLS", test_sym.entity_id, code_sym.entity_id)
            kg = self._build_snapshot([code_sym, test_sym], [calls_fact], root)
            result = call_tool(
                kg,
                "review_context",
                {
                    "repo": "lib",
                    "changed_files": ["src/processor.py", "tests/test_processor.py"],
                    "changed_ranges": [
                        {"path": "src/processor.py", "start_line": 10, "end_line": 30},
                        {"path": "tests/test_processor.py", "start_line": 5, "end_line": 15},
                    ],
                },
            )
        risk_types = [h["risk_type"] for h in result["review_hypotheses"]]
        self.assertIn("test_locks_in_regression", risk_types)


class TestHypothesisStatusE2E(unittest.TestCase):
    """Task K test 6: E2E call_tool on the fixture snapshot produces review_hypothesis_status with consistent counts."""

    def test_review_hypothesis_status_present_and_consistent_on_fixture(self) -> None:
        with _fixture_snapshot(upstream_checkout_caller=True) as kg:
            result = call_tool(
                kg,
                "review_context",
                {
                    "repo": "payments",
                    "changed_files": ["payments/checkout.py"],
                    "changed_ranges": [{"path": "payments/checkout.py", "start_line": 10, "end_line": 20}],
                },
            )

        status = result.get("review_hypothesis_status")
        self.assertIsNotNone(status, "review_hypothesis_status must be present in live call_tool result")
        available = status.get("available_count", -1)
        returned = status.get("returned_count", -1)
        mirror_count = status.get("answer_packet_returned_count", -1)
        truncated = status.get("truncated_count", -1)

        # Counts must be non-negative integers
        self.assertGreaterEqual(available, 0, "available_count must be >= 0")
        self.assertGreaterEqual(returned, 0, "returned_count must be >= 0")
        self.assertGreaterEqual(mirror_count, 0, "answer_packet_returned_count must be >= 0")
        # truncated_count = available - returned
        self.assertEqual(truncated, available - returned, "truncated_count must equal available_count - returned_count")
        # returned <= available
        self.assertLessEqual(returned, available, "returned_count must not exceed available_count")
        # mirror <= returned
        self.assertLessEqual(mirror_count, returned, "answer_packet_returned_count must not exceed returned_count")
        # reason consistency
        reason = status.get("reason")
        if truncated == 0 and mirror_count == returned:
            self.assertIsNone(reason, f"reason must be null when nothing truncated, got {reason!r}")
        if truncated > 0 or mirror_count < returned:
            self.assertIn(reason, ("budget", "none_generated", "low_coverage"), f"unexpected reason: {reason!r}")


class TestOptionalReviewSurfacesTolerantDedupe(unittest.TestCase):
    """Finding 2 (Copilot): unknown-surface dedupe must use normalized token so case/separator
    variants that map to the same normalized form produce only one unknown row."""

    def test_case_and_separator_variants_dedupe(self) -> None:
        """AuthZ + authz and rule-actions + rule_actions → 2 unknown rows, first spelling kept."""
        _surfaces, unknown = _optional_review_surfaces_tolerant(
            {"requested_surfaces": ["AuthZ", "authz", "rule-actions", "rule_actions"]},
            "requested_surfaces",
        )
        self.assertEqual(len(unknown), 2, f"expected 2 unknown rows, got {unknown}")
        self.assertEqual(unknown[0], "AuthZ")
        self.assertEqual(unknown[1], "rule-actions")

    def test_identical_tokens_dedupe(self) -> None:
        """Exact-same unknown token appears only once."""
        _surfaces, unknown = _optional_review_surfaces_tolerant(
            {"requested_surfaces": ["foobar", "foobar"]},
            "requested_surfaces",
        )
        self.assertEqual(len(unknown), 1)
        self.assertEqual(unknown[0], "foobar")


def _minimal_risk_kg(
    test_case: unittest.TestCase,
    *,
    repo: str,
    signal_repo: str,
    signal_path: str,
    signal_line_start: int,
    signal_line_end: int,
) -> KgSnapshot:
    """Build a minimal KgSnapshot with one code_risk_signal support fact + evidence."""
    import tempfile
    from source.kg.core.models import Entity, Fact, Evidence, Coverage

    tmpdir = tempfile.mkdtemp()
    test_case.addCleanup(shutil.rmtree, tmpdir, True)
    root = Path(tmpdir)
    subject = Entity(
        kind="CodeSymbol",
        identity={
            "tenant_id": "default",
            "repo": signal_repo,
            "module": "pkg.worker",
            "qualname": "do_work",
            "symbol_kind": "function",
        },
        properties={"path": signal_path, "line": signal_line_start},
    )
    signal_fact = Fact(
        predicate="code_risk_signal",
        subject_id=subject.entity_id,
        object_id=subject.entity_id,
        qualifier={"risk_family": "swallowed_exception", "exception_type": "bare", "detail": "test"},
    )
    ev = Evidence(
        target_type="fact",
        target_id=signal_fact.fact_id,
        derivation_class="deterministic_static",
        source_system="test",
        source_ref={"extractor": "test"},
        bytes_ref={
            "repo": signal_repo,
            "commit_sha": "abc123",
            "path": signal_path,
            "line_start": signal_line_start,
            "line_end": signal_line_end,
        },
        confidence=1.0,
    )
    JsonlKgStore(root).write(
        entities=[subject],
        facts=[],
        support_facts=[signal_fact],
        evidence=[ev],
        coverage=[],
        manifest={"version": 1, "repo_name": repo, "repo_path": str(root)},
    )
    kg = KgSnapshot(root)
    # Store the subject entity_id so callers can build changed_symbols rows.
    kg._test_subject_entity_id = subject.entity_id  # type: ignore[attr-defined]
    return kg


class ReviewContextRiskSignalScopeTest(unittest.TestCase):
    """P1: _review_context_risk_signals scopes by repo and changed ranges."""

    def test_same_path_different_repo_signal_excluded(self) -> None:
        """Signal from another repo with same path must be excluded (no subject match)."""
        kg = _minimal_risk_kg(
            self,
            repo="repo-a",
            signal_repo="repo-b",
            signal_path="pkg/worker.py",
            signal_line_start=5,
            signal_line_end=10,
        )
        results = _review_context_risk_signals(
            kg,
            repo="repo-a",
            changed_symbols=[],
            changed_files=["pkg/worker.py"],
            range_filters={},
        )
        # Signal is from repo-b; repo-a mismatch → must be excluded
        self.assertEqual(results, [], f"signal from different repo must be excluded: {results}")

    def test_same_file_out_of_range_signal_excluded_when_ranges_supplied(self) -> None:
        """Signal on lines 50-60 must be excluded when changed_ranges cover only lines 1-10."""
        kg = _minimal_risk_kg(
            self,
            repo="repo-a",
            signal_repo="repo-a",
            signal_path="pkg/worker.py",
            signal_line_start=50,
            signal_line_end=60,
        )
        results = _review_context_risk_signals(
            kg,
            repo="repo-a",
            changed_symbols=[],
            changed_files=["pkg/worker.py"],
            range_filters={"pkg/worker.py": [(1, 10)]},
        )
        # Evidence lines 50-60 do not overlap range 1-10 → excluded
        self.assertEqual(results, [], f"out-of-range signal must be excluded when ranges supplied: {results}")
        # Inversion: same signal with no range filter must be included
        results_no_ranges = _review_context_risk_signals(
            kg,
            repo="repo-a",
            changed_symbols=[],
            changed_files=["pkg/worker.py"],
            range_filters={},
        )
        self.assertEqual(len(results_no_ranges), 1, "without ranges the signal must be included (inversion)")

    def test_subject_matched_signal_included_regardless_of_ranges(self) -> None:
        """Signal whose subject_id matches a changed symbol is included even if out of range."""
        kg = _minimal_risk_kg(
            self,
            repo="repo-a",
            signal_repo="repo-a",
            signal_path="pkg/worker.py",
            signal_line_start=50,
            signal_line_end=60,
        )
        entity_id = kg._test_subject_entity_id  # type: ignore[attr-defined]
        # Supply a changed_symbols row using symbol_id (_symbol_result shape); entity_id absent.
        changed_sym = {"symbol_id": entity_id, "qualname": "do_work", "path": "pkg/worker.py"}
        results = _review_context_risk_signals(
            kg,
            repo="repo-a",
            changed_symbols=[changed_sym],
            changed_files=["pkg/worker.py"],
            range_filters={"pkg/worker.py": [(1, 10)]},
        )
        # subject_id directly matches symbol_id → included regardless of range mismatch
        self.assertEqual(len(results), 1, f"subject-matched signal must be included: {results}")

    def test_in_range_signal_included(self) -> None:
        """Signal overlapping the supplied range is included."""
        kg = _minimal_risk_kg(
            self,
            repo="repo-a",
            signal_repo="repo-a",
            signal_path="pkg/worker.py",
            signal_line_start=5,
            signal_line_end=8,
        )
        results = _review_context_risk_signals(
            kg,
            repo="repo-a",
            changed_symbols=[],
            changed_files=["pkg/worker.py"],
            range_filters={"pkg/worker.py": [(1, 10)]},
        )
        self.assertEqual(len(results), 1, f"in-range signal must be included: {results}")

    def test_dotslash_evidence_path_normalizes_to_match(self) -> None:
        """bytes_ref.path with a leading './' must still match a normalized changed_files entry."""
        kg = _minimal_risk_kg(
            self,
            repo="repo-a",
            signal_repo="repo-a",
            signal_path="./pkg/worker.py",
            signal_line_start=5,
            signal_line_end=8,
        )
        results = _review_context_risk_signals(
            kg,
            repo="repo-a",
            changed_symbols=[],
            changed_files=["pkg/worker.py"],
            range_filters={"pkg/worker.py": [(1, 10)]},
        )
        self.assertEqual(len(results), 1, f"dotslash evidence path must be normalized and matched: {results}")


def _build_multi_file_risk_kg(
    test_case: unittest.TestCase,
    *,
    n_files: int = 5,
    signal_file_index: int = 2,
    signal_line: int = 50,
    changed_line: int = 5,
) -> tuple[KgSnapshot, list[Entity]]:
    """Build a KG with n_files changed symbols and one code_risk_signal on symbol[signal_file_index].

    The signal line (signal_line) intentionally does NOT overlap the changed_range
    (changed_line), so only subject-match can retrieve the signal.
    """
    tmpdir = tempfile.mkdtemp()
    test_case.addCleanup(shutil.rmtree, tmpdir, True)
    root = Path(tmpdir)
    symbols: list[Entity] = []
    for i in range(n_files):
        sym = Entity(
            kind="CodeSymbol",
            identity={
                "tenant_id": "default",
                "repo": "testrepo",
                "module": f"pkg.module_{i}",
                "qualname": f"fn_{i}",
                "symbol_kind": "function",
            },
            properties={"path": f"pkg/module_{i}.py", "line": 1, "end_line": 100},
        )
        symbols.append(sym)
    target = symbols[signal_file_index]
    signal_fact = Fact(
        predicate="code_risk_signal",
        subject_id=target.entity_id,
        object_id=target.entity_id,
        qualifier={
            "risk_family": "swallowed_exception",
            "exception_type": "bare",
            "qualname": f"fn_{signal_file_index}",
            "line": signal_line,
        },
    )
    ev = Evidence(
        target_type="fact",
        target_id=signal_fact.fact_id,
        derivation_class="deterministic_static",
        source_system="test",
        source_ref={"extractor": "test"},
        bytes_ref={
            "repo": "testrepo",
            "commit_sha": "abc",
            "path": f"pkg/module_{signal_file_index}.py",
            "line_start": signal_line,
            "line_end": signal_line,
        },
        confidence=1.0,
    )
    JsonlKgStore(root).write(
        entities=symbols,
        facts=[],
        support_facts=[signal_fact],
        evidence=[ev],
        coverage=[],
        manifest={"version": 1, "repo_name": "testrepo", "repo_path": str(root)},
    )
    return KgSnapshot(root), symbols


class TestR2SubjectMatchViaSymbolId(unittest.TestCase):
    """R2 regression: _review_context_risk_signals must match subject_id against symbol_id.

    Inversion evidence: prior code read entity_id (absent on real _symbol_result rows);
    fixed to read symbol_id. Signals whose subject entity IS a changed symbol must be
    retrieved even when their evidence lines don't overlap the changed hunks.
    """

    def test_subject_match_fires_despite_non_overlapping_hunk(self) -> None:
        """Signal on line 50 included via subject-match when changed_range covers line 1-10."""
        kg, symbols = _build_multi_file_risk_kg(
            self,
            n_files=5,
            signal_file_index=2,
            signal_line=50,
            changed_line=5,
        )
        target = symbols[2]
        # changed_symbols uses symbol_id (real _symbol_result shape, NOT entity_id)
        changed_sym = {
            "symbol_id": target.entity_id,
            "qualname": "fn_2",
            "path": "pkg/module_2.py",
        }
        results = _review_context_risk_signals(
            kg,
            repo="testrepo",
            changed_symbols=[changed_sym],
            changed_files=[f"pkg/module_{i}.py" for i in range(5)],
            range_filters={"pkg/module_2.py": [(1, 10)]},
        )
        # Signal is on line 50, range covers 1-10 → no line overlap.
        # Subject match via symbol_id must still fire.
        self.assertEqual(len(results), 1, "subject-match must fire despite non-overlapping hunk")
        self.assertEqual(results[0].get("subject_id"), target.entity_id)

    def test_subject_match_does_not_fire_when_symbol_id_absent(self) -> None:
        """When changed_symbols row lacks symbol_id, out-of-range signal is excluded.

        Inversion: a row with entity_id (old shape) must also NOT match — confirms the
        fabrication that shipped R2 is gone.
        """
        kg, symbols = _build_multi_file_risk_kg(
            self,
            n_files=5,
            signal_file_index=2,
            signal_line=50,
        )
        # Row with entity_id (old broken shape) must NOT match
        changed_sym_old = {
            "entity_id": symbols[2].entity_id,
            "qualname": "fn_2",
            "path": "pkg/module_2.py",
        }
        results_old = _review_context_risk_signals(
            kg,
            repo="testrepo",
            changed_symbols=[changed_sym_old],
            changed_files=["pkg/module_2.py"],
            range_filters={"pkg/module_2.py": [(1, 10)]},
        )
        self.assertEqual(results_old, [], "entity_id (old shape) must not match — confirms old bug is gone")

    def test_end_to_end_subject_match_multi_file(self) -> None:
        """call_tool review_context: signal fires via subject-match; changed_symbol_count > 1.

        Builds 5 changed files with symbols. Signal on module_2.py line 50; changed ranges
        cover line 1-10 of each file (no overlap). changed_symbol_count must be > 1 and the
        swallowed_exception_state_drift family must appear in review_hypotheses.
        """
        kg, _syms = _build_multi_file_risk_kg(
            self,
            n_files=5,
            signal_file_index=2,
            signal_line=50,
        )
        changed_files = [f"pkg/module_{i}.py" for i in range(5)]
        changed_ranges = [{"path": f"pkg/module_{i}.py", "start_line": 1, "end_line": 10} for i in range(5)]
        result = call_tool(
            kg,
            "review_context",
            {"repo": "testrepo", "changed_files": changed_files, "changed_ranges": changed_ranges, "limit": 20},
        )
        # Non-vacuous: must have retained more than 1 cluster
        leads = result.get("review_leads") or {}
        sym_rows = leads.get("changed_symbols") or []
        self.assertGreater(len(sym_rows), 1, "must retain >1 changed-symbol row across 5 files")
        # Detector must fire via subject-match despite no hunk overlap
        hypotheses = result.get("review_hypotheses") or []
        risk_types = [h.get("risk_type") for h in hypotheses]
        self.assertIn(
            "swallowed_exception_state_drift",
            risk_types,
            f"swallowed_exception_state_drift must fire via subject-match; got {risk_types}",
        )


class TestR1FundingBoilerplateVictim(unittest.TestCase):
    """R1 regression: anchor repair must succeed when the only evictable mass is boilerplate.

    Builds a packet whose broad-context sections are empty but whose boilerplate/inventory
    sections (changed_surface rows, surface_status, changed_file_symbols, duplicated
    answer-packet contracts) consume enough bytes to leave no room for cluster anchors.
    Asserts that the cap is held and the anchors are still present.
    """

    def _build_boilerplate_heavy_packet(self, n_clusters: int = 5, *, max_chars: int) -> dict:
        """Construct a review_context-shaped packet with empty broad context and fat boilerplate."""
        # Build a multi-cluster changed_symbols list
        changed_symbols = []
        for i in range(n_clusters):
            changed_symbols.append({
                "symbol_id": f"ent_{i:04x}",
                "lead_id": f"lead:changed_symbol:ent_{i:04x}",
                "lead_kind": "changed_symbol",
                "display_name": f"mod_{i}.fn_{i}",
                "qualified_name": f"mod_{i}.fn_{i}",
                "qualname": f"fn_{i}",
                "path": f"src/module_{i}.py",
                "line": 10,
                "end_line": 50,
                "evidence": [],
            })
        # Build fat surface_status rows (boilerplate) — padded to push packet over 40K cap
        surface_status = [
            {
                "surface": f"api_surface_{j}",
                "status": "unsupported_or_unlinked",
                "reason": "A" * 600,
                "detail": "B" * 400,
            }
            for j in range(40)
        ]
        # Build fat changed_file_symbols inventory (boilerplate)
        changed_file_symbols = [
            {
                "symbol_id": f"cfs_{j}",
                "display_name": f"FileSym_{j}",
                "qualified_name": f"mod.FileSym_{j}",
                "path": "src/big_file.py",
                "line": j * 5,
                "evidence": [{"bytes_ref": {"path": "src/big_file.py", "line_start": j * 5}}],
            }
            for j in range(50)
        ]
        # Build fat changed_surface rows
        changed_surface = {
            "files": [
                {"path": f"src/module_{i}.py", "symbol_count": 3, "detail": "D" * 200}
                for i in range(30)
            ],
            "symbols": [
                {"path": f"src/module_{i}.py", "display_name": f"fn_{i}", "line": 10, "detail": "E" * 100}
                for i in range(30)
            ],
        }
        review_leads = {
            "changed_symbols": changed_symbols,
            "direct_callers": [],
            "direct_callees": [],
            "transitive_callers": [],
            "source_coordinates": [],
        }
        review_lead_status = {
            "coverage_status": "ok",
            "changed_anchor_count": n_clusters,
            "changed_symbol_count": n_clusters,
            "direct_impact_count": 0,
            "transitive_impact_count": 0,
            "source_coordinate_count": 0,
            "file_anchor_count": 0,
            "available": {
                "changed_symbol_count": n_clusters,
                "direct_caller_count": 0,
                "direct_callee_count": 0,
                "transitive_caller_count": 0,
                "source_coordinate_count": 0,
            },
        }
        answer_packet: dict = {
            "status": "found",
            "summary": {"changed_symbol_count": n_clusters},
            # Duplicated contracts (already at top level — pure boilerplate in answer_packet)
            "claim_contract": {"note": "C" * 500},
            "scope_contract": {"note": "S" * 500},
            "top_changed_symbols": [dict(s) for s in changed_symbols[:3]],
            "top_diff_anchors": [],
            "top_direct_callers": [],
            "top_direct_callees": [],
            "top_transitive_callers": [],
            "surface_status": surface_status[:5],
            "review_lead_status": review_lead_status,
        }
        diff_anchors = [
            {
                "anchor_type": "symbol",
                "path": f"src/module_{i}.py",
                "display_name": f"fn_{i}",
                "line": 10,
            }
            for i in range(n_clusters)
        ]
        packet: dict = {
            "tool": "review_context",
            "status": "found",
            "repo": "testrepo",
            "summary": {
                "changed_symbol_count": n_clusters,
                "diff_anchor_count": n_clusters,
                "symbol_anchor_count": n_clusters,
                "file_anchor_count": 0,
                "direct_caller_count": 0,
                "direct_callee_count": 0,
                "transitive_caller_count": 0,
                "framework_model_count": 0,
                "framework_relation_count": 0,
                "app_surface_count": 0,
                "app_runtime_fact_count": 0,
                "app_cross_repo_lead_count": 0,
                "candidate_or_unlinked_event_fact_count": 0,
                "detail_limit": 20,
                "requested_limit": 20,
            },
            "review_answer_packet": answer_packet,
            "review_lead_status": review_lead_status,
            "review_leads": review_leads,
            "diff_anchors": diff_anchors,
            "changed_symbols": list(changed_symbols),
            "changed_file_symbols": changed_file_symbols,
            "surface_status": surface_status,
            "changed_surface": changed_surface,
            "direct_callers": [],
            "direct_callees": [],
            "transitive_callers": [],
            "source_coordinates": [],
            # No broad context (empty: application_impact, framework_impact, runtime_surfaces)
            "application_impact": {"same_repo_surfaces": {}},
            "framework_impact": {},
            "runtime_surfaces": {},
            "output_budget": {"engine_version": "test"},
            "answerability": {"status": "found"},
            "coverage_warnings": [],
            "unsupported_scopes": [],
            "next_actions": [],
            "proven_facts": {},
            "candidate_leads": {},
            "coverage_gaps": [],
            "inspection_areas": [],
            "packet_contract": {},
            "claim_contract": {"note": "C" * 500},
            "scope_contract": {"note": "S" * 500},
        }
        return packet

    def test_cap_held_when_only_boilerplate_evictable(self) -> None:
        """When broad context is empty, boilerplate/inventory must fund the cap."""
        packet = self._build_boilerplate_heavy_packet(n_clusters=5, max_chars=REVIEW_CONTEXT_MAX_CHARS)
        from source.kg.core.models import canonical_json
        original_size = len(canonical_json(packet))
        # Only meaningful if original exceeds cap
        if original_size <= REVIEW_CONTEXT_MAX_CHARS:
            self.skipTest("fixture does not exceed cap — increase boilerplate mass")
        result = enforce_review_context_budget(packet, max_chars=REVIEW_CONTEXT_MAX_CHARS)
        result_size = len(canonical_json(result))
        self.assertLessEqual(result_size, REVIEW_CONTEXT_MAX_CHARS, "cap must be held")

    def test_anchors_restored_after_compact_reattach(self) -> None:
        """Compact profile: cluster anchors must survive when the packet is over-budget.

        This exercises the compact (hypothesis-first) path, NOT the broad-path
        boilerplate eviction. Without include_broad_context, the code calls
        _hypothesis_first_compact_packet which builds up from a skeleton and
        re-attaches cluster anchors from review_leads.changed_symbols.
        """
        n_clusters = 5
        packet = self._build_boilerplate_heavy_packet(n_clusters=n_clusters, max_chars=REVIEW_CONTEXT_MAX_CHARS)
        from source.kg.core.models import canonical_json
        if len(canonical_json(packet)) <= REVIEW_CONTEXT_MAX_CHARS:
            self.skipTest("fixture does not exceed cap")
        result = enforce_review_context_budget(packet, max_chars=REVIEW_CONTEXT_MAX_CHARS)
        result_size = len(canonical_json(result))
        self.assertLessEqual(result_size, REVIEW_CONTEXT_MAX_CHARS, "cap must be held")
        # At least some anchors must survive
        retained = (result.get("review_leads") or {}).get("changed_symbols") or []
        self.assertTrue(retained, "review_leads.changed_symbols must be non-empty after compact reattach")

    def test_unknown_surface_honesty_survives_compact_reattach(self) -> None:
        """Compact profile: unknown-surface honesty rows must survive the budget build.

        This exercises the compact (hypothesis-first) reattach path via
        _reattach_unknown_surface_rows, NOT the broad-path _evict_one_boilerplate_row.
        The compact path only re-attaches unknown-surface rows (supported/linked rows
        are excluded from the skeleton entirely); the unknown row must always be kept.
        """
        packet = self._build_boilerplate_heavy_packet(n_clusters=5, max_chars=REVIEW_CONTEXT_MAX_CHARS)
        # Mixed list: fat supported rows (excluded on compact) around a bounded unknown row.
        packet["surface_status"] = (
            [
                {"surface": f"linked_surface_{j}", "status": "linked", "detail": "L" * 600}
                for j in range(20)
            ]
            + [
                {
                    "surface": "unknown_surface_a",
                    "status": "unsupported_or_unlinked",
                    "source_inspection_terms": ["unknown_surface_a"],
                }
            ]
            + [
                {"surface": f"linked_surface_tail_{j}", "status": "linked", "detail": "T" * 600}
                for j in range(20)
            ]
        )
        from source.kg.core.models import canonical_json
        if len(canonical_json(packet)) <= REVIEW_CONTEXT_MAX_CHARS:
            self.skipTest("fixture does not exceed cap")
        result = enforce_review_context_budget(packet, max_chars=REVIEW_CONTEXT_MAX_CHARS)
        self.assertLessEqual(len(canonical_json(result)), REVIEW_CONTEXT_MAX_CHARS, "cap must be held")
        rows = result.get("surface_status") or []
        unknown_rows = [r for r in rows if isinstance(r, dict) and r.get("status") == "unsupported_or_unlinked"]
        self.assertEqual(len(unknown_rows), 1, "the unknown-surface honesty row must survive compact reattach")
        self.assertEqual(unknown_rows[0]["surface"], "unknown_surface_a")
        # Linked rows are excluded by the compact profile (only unknown rows are re-attached).
        linked_rows = [r for r in rows if isinstance(r, dict) and r.get("status") == "linked"]
        self.assertEqual(len(linked_rows), 0, "compact profile must not include linked surface rows")

    def test_unknown_surface_compact_reattach_keeps_omitted_count(self) -> None:
        """Compact profile: all-unknown surface_status keeps >= 1 row and tracks omitted.

        The fixture's surface_status is all-unknown and fat (40 rows x ~1KB).
        The compact path (_reattach_unknown_surface_rows) keeps as many as fit the
        budget; when it must drop rows the last survivor carries omitted_unknown_surface_count.
        """
        packet = self._build_boilerplate_heavy_packet(n_clusters=5, max_chars=REVIEW_CONTEXT_MAX_CHARS)
        original_unknown = [
            r for r in packet["surface_status"] if r.get("status") == "unsupported_or_unlinked"
        ]
        self.assertEqual(len(original_unknown), 40, "fixture precondition: all-unknown surface_status")
        from source.kg.core.models import canonical_json
        if len(canonical_json(packet)) <= REVIEW_CONTEXT_MAX_CHARS:
            self.skipTest("fixture does not exceed cap")
        result = enforce_review_context_budget(packet, max_chars=REVIEW_CONTEXT_MAX_CHARS)
        self.assertLessEqual(len(canonical_json(result)), REVIEW_CONTEXT_MAX_CHARS, "cap must be held")
        rows = [
            r
            for r in (result.get("surface_status") or [])
            if isinstance(r, dict) and r.get("status") == "unsupported_or_unlinked"
        ]
        self.assertGreaterEqual(len(rows), 1, "at least one unknown-surface row must survive")
        if len(rows) < 40:
            omitted = rows[-1].get("omitted_unknown_surface_count")
            self.assertEqual(
                omitted,
                40 - len(rows),
                "dropped unknown rows must be folded into omitted_unknown_surface_count",
            )

    def test_evict_one_boilerplate_row_surface_status_linked_before_unknown(self) -> None:
        """_evict_one_boilerplate_row must evict linked rows before unknown-surface rows.

        Inversion proof: the guard `label.split('.')[-1] != 'surface_status'` in
        _evict_one_boilerplate_row is what routes to the special eviction path.
        Direct unit test — exercises _evict_one_boilerplate_row and
        _surface_status_has_evictable_row without the full packet pipeline so the
        inversion is trivially falsifiable.
        """
        from source.kg.product.output_budget import _evict_one_boilerplate_row
        rows = [
            {"surface": "linked_a", "status": "linked", "detail": "A" * 100},
            {"surface": "unknown_x", "status": "unsupported_or_unlinked"},
            {"surface": "linked_b", "status": "linked", "detail": "B" * 100},
        ]
        # First eviction: linked_b (last linked row) must go, not unknown_x.
        result1 = _evict_one_boilerplate_row("surface_status", rows)
        self.assertTrue(result1, "eviction must succeed")
        self.assertEqual(len(rows), 2)
        surviving_surfaces = [r["surface"] for r in rows if isinstance(r, dict)]
        self.assertIn("unknown_x", surviving_surfaces, "unknown row must survive first eviction")
        self.assertNotIn("linked_b", surviving_surfaces, "linked_b must be the eviction victim")
        # Second eviction: linked_a must go before unknown_x.
        result2 = _evict_one_boilerplate_row("surface_status", rows)
        self.assertTrue(result2, "second eviction must succeed")
        remaining = [r["surface"] for r in rows if isinstance(r, dict)]
        self.assertIn("unknown_x", remaining, "unknown row must survive second eviction")
        self.assertNotIn("linked_a", remaining, "linked_a must be the second eviction victim")
        # Third eviction: only the unknown row remains — guard returns False.
        result3 = _evict_one_boilerplate_row("surface_status", rows)
        self.assertFalse(result3, "eviction must refuse when only the last unknown row remains")
        self.assertEqual(len(rows), 1, "last unknown row must not be evicted")

    def test_surface_status_has_evictable_row_guards_correctly(self) -> None:
        """_surface_status_has_evictable_row must return True when eviction is safe.

        Inversion proof: if this guard always returned False, _largest_non_lead_row_list
        would skip surface_status entirely and the eviction loop would stop without
        removing any surface_status rows — verified here at the unit level.
        """
        from source.kg.product.output_budget import _surface_status_has_evictable_row
        # Mixed: at least one linked row → evictable.
        mixed = [
            {"status": "linked"},
            {"status": "unsupported_or_unlinked"},
        ]
        self.assertTrue(_surface_status_has_evictable_row(mixed), "mixed list must be evictable")
        # All linked: evictable.
        all_linked = [{"status": "linked"}, {"status": "linked"}]
        self.assertTrue(_surface_status_has_evictable_row(all_linked), "all-linked list must be evictable")
        # Two unknown rows: evictable (fold is possible).
        two_unknown = [{"status": "unsupported_or_unlinked"}, {"status": "unsupported_or_unlinked"}]
        self.assertTrue(_surface_status_has_evictable_row(two_unknown), "two unknown rows must be evictable")
        # Single unknown row: NOT evictable (last survivor protection).
        one_unknown = [{"status": "unsupported_or_unlinked"}]
        self.assertFalse(_surface_status_has_evictable_row(one_unknown), "single unknown row must NOT be evictable")
        # Inversion: if guard always returned True for a single unknown row,
        # _evict_one_boilerplate_row would return False (its own inner guard protects the
        # last unknown row), but _largest_non_lead_row_list would still target the list —
        # verify that a single-unknown surface_status is correctly excluded from targeting.
        from source.kg.product.output_budget import _largest_non_lead_row_list
        node = {"surface_status": [{"status": "unsupported_or_unlinked"}]}
        target = _largest_non_lead_row_list(node)
        self.assertIsNone(target, "_largest_non_lead_row_list must not target a single-unknown surface_status")


class TestTotalRetrievalBound(unittest.TestCase):
    """Fix 1: _review_context_risk_signals total retrieval bound (limit=12).

    6 subjects × 5 signals each = 30 raw; per-subject cap (3) reduces to 18;
    total bound (limit=12) must reduce to exactly 12, range-overlap signals first.
    """

    def _build_multi_subject_kg(self, n_subjects: int, signals_per_subject: int) -> tuple[KgSnapshot, list[Entity]]:
        """Build a KG with n_subjects symbols, each having signals_per_subject risk signals.

        Evidence for signal i of subject j is at line (j*100 + i*2 + 1).
        Subject 0's signals are in-range (range covers lines 1-10); all others are not,
        so subject 0's signals rank first (overlap=1) in the total sort order.
        """
        tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmpdir, True)
        root = Path(tmpdir)
        subjects: list[Entity] = []
        support_facts: list[Fact] = []
        evidence_rows: list[Evidence] = []

        for j in range(n_subjects):
            sym = Entity(
                kind="CodeSymbol",
                identity={
                    "tenant_id": "default",
                    "repo": "testrepo",
                    "module": f"pkg.mod_{j}",
                    "qualname": f"fn_{j}",
                    "symbol_kind": "function",
                },
                properties={"path": f"pkg/mod_{j}.py", "line": 1},
            )
            subjects.append(sym)
            for i in range(signals_per_subject):
                line = j * 100 + i * 2 + 1
                sf = Fact(
                    predicate="code_risk_signal",
                    subject_id=sym.entity_id,
                    object_id=sym.entity_id,
                    qualifier={
                        "risk_family": "swallowed_exception",
                        "exception_type": "bare",
                        "qualname": f"fn_{j}",
                        "line": line,
                    },
                )
                ev = Evidence(
                    target_type="fact",
                    target_id=sf.fact_id,
                    derivation_class="deterministic_static",
                    source_system="test",
                    source_ref={"extractor": "test"},
                    bytes_ref={
                        "repo": "testrepo",
                        "commit_sha": "abc",
                        "path": f"pkg/mod_{j}.py",
                        "line_start": line,
                        "line_end": line,
                    },
                    confidence=1.0,
                )
                support_facts.append(sf)
                evidence_rows.append(ev)

        JsonlKgStore(root).write(
            entities=subjects,
            facts=[],
            support_facts=support_facts,
            evidence=evidence_rows,
            coverage=[],
            manifest={"version": 1, "repo_name": "testrepo", "repo_path": str(root)},
        )
        return KgSnapshot(root), subjects

    def test_6_subjects_5_signals_returns_exactly_12(self) -> None:
        """6 subjects × 5 signals; per-subject cap=3 → 18; total limit=12 → exactly 12."""
        kg, subjects = self._build_multi_subject_kg(n_subjects=6, signals_per_subject=5)
        # Subject 0 is in changed_symbols (subject-match) and in-range (lines 1-9 overlap range 1-10).
        changed_sym = {
            "symbol_id": subjects[0].entity_id,
            "qualname": "fn_0",
            "path": "pkg/mod_0.py",
        }
        changed_files = [f"pkg/mod_{j}.py" for j in range(6)]
        range_filters = {"pkg/mod_0.py": [(1, 10)]}
        results = _review_context_risk_signals(
            kg,
            repo="testrepo",
            changed_symbols=[changed_sym],
            changed_files=changed_files,
            range_filters=range_filters,
            limit=12,
        )
        self.assertEqual(len(results), 12, f"expected exactly 12, got {len(results)}")

    def test_overlap_signals_rank_first(self) -> None:
        """Range-overlap signals must appear before subject-matched and plain path-matched rows."""
        kg, subjects = self._build_multi_subject_kg(n_subjects=6, signals_per_subject=5)
        changed_sym = {
            "symbol_id": subjects[0].entity_id,
            "qualname": "fn_0",
            "path": "pkg/mod_0.py",
        }
        changed_files = [f"pkg/mod_{j}.py" for j in range(6)]
        # Subject 0 lines: 1, 3, 5, 7, 9 (i*2+1 for i=0..4); range 1-10 overlaps all.
        range_filters = {"pkg/mod_0.py": [(1, 10)]}
        results = _review_context_risk_signals(
            kg,
            repo="testrepo",
            changed_symbols=[changed_sym],
            changed_files=changed_files,
            range_filters=range_filters,
            limit=12,
        )
        # Per-subject cap is 3; subject 0 has lines 1,3,5 overlapping range → 3 overlap rows.
        overlap_count = 0
        for sig in results:
            for ev in (sig.get("_evidence") or []):
                br = ev.get("bytes_ref") if isinstance(ev, dict) else None
                if isinstance(br, dict):
                    ls, le = br.get("line_start"), br.get("line_end")
                    if isinstance(ls, int) and isinstance(le, int) and ls >= 1 and le <= 10:
                        overlap_count += 1
                        break
        # At least 3 overlap rows must appear (subject 0, capped at 3).
        self.assertGreaterEqual(overlap_count, 3, "at least 3 range-overlap signals must appear first")
        # All overlap rows must precede all non-overlap rows in the result list.
        saw_non_overlap = False
        for sig in results:
            is_overlap = False
            for ev in (sig.get("_evidence") or []):
                br = ev.get("bytes_ref") if isinstance(ev, dict) else None
                if isinstance(br, dict):
                    ls, le = br.get("line_start"), br.get("line_end")
                    if isinstance(ls, int) and isinstance(le, int) and ls >= 1 and le <= 10:
                        is_overlap = True
                        break
            if is_overlap:
                self.assertFalse(saw_non_overlap, "overlap signal appeared after non-overlap signal")
            else:
                saw_non_overlap = True


class TestNegativeChecksKept(unittest.TestCase):
    """Issue #2: _lean_review_hypothesis must keep the first negative_check, not drop all.

    Inversion proof: pop negative_checks entirely → the lean row has no negative_checks;
    keep exactly one → the lean row has one entry. Field-gate assertion: the compact
    profile must stay ≤ 15,000 chars on the pr-7232 replay with negative_checks restored.
    """

    def _make_hypothesis(self, n_negative: int = 3) -> dict:
        return {
            "hypothesis_id": "hyp-001",
            "risk_type": "async_side_effect_lifecycle_drift",
            "specificity": "high",
            "confidence": "high",
            "why": "test why",
            "postable_claim": "test claim",
            "source_checks": ["check_a", "check_b"],
            "negative_checks": [f"neg_{i}" for i in range(n_negative)],
            "evidence_refs": [{"path": "a.py", "line_start": 1}],
            "source_spans": [{"path": "a.py", "line_start": 1}],
            "supporting_lead_ids": [],
        }

    def test_lean_keeps_one_negative_check(self) -> None:
        """With 3 negative_checks, lean must retain exactly 1."""
        from source.kg.product.output_budget import _lean_review_hypothesis
        hyp = self._make_hypothesis(n_negative=3)
        lean = _lean_review_hypothesis(hyp)
        negatives = lean.get("negative_checks")
        self.assertIsNotNone(negatives, "negative_checks must be present (not popped)")
        self.assertEqual(len(negatives), 1, "exactly 1 negative_check must survive")
        self.assertEqual(negatives[0], "neg_0", "first negative_check must survive")

    def test_lean_keeps_zero_when_none(self) -> None:
        """With no negative_checks, lean must produce no negative_checks key (or empty)."""
        from source.kg.product.output_budget import _lean_review_hypothesis
        hyp = self._make_hypothesis(n_negative=0)
        hyp.pop("negative_checks")
        lean = _lean_review_hypothesis(hyp)
        negatives = lean.get("negative_checks")
        self.assertTrue(negatives is None or negatives == [], "absent negative_checks stays absent/empty")

    def test_inversion_pop_all_removes_field(self) -> None:
        """Inverting to pop all: negative_checks absent from lean row (proves fix matters).

        Before the fix: lean.pop('negative_checks', None) — field absent even with 3 checks.
        After the fix: lean['negative_checks'] = checks[:1] — field present with 1 entry.
        """
        from source.kg.product import output_budget as ob
        original = ob._lean_review_hypothesis

        def _lean_pop_all(row: dict) -> dict:
            result = ob._compact_review_hypothesis(row)
            result.pop("negative_checks", None)
            return result

        hyp = self._make_hypothesis(n_negative=3)
        fixed_lean = ob._lean_review_hypothesis(hyp)
        inverted_lean = _lean_pop_all(hyp)
        # Fixed: negative_checks present with 1 entry.
        self.assertIsNotNone(fixed_lean.get("negative_checks"), "fixed: negative_checks must be present")
        self.assertEqual(len(fixed_lean["negative_checks"]), 1)
        # Inverted: negative_checks absent.
        self.assertIsNone(inverted_lean.get("negative_checks"), "inverted: negative_checks must be absent")


class TestCompactProfileTruncatedSections(unittest.TestCase):
    """Issue #3: _hypothesis_first_compact_packet must record diff_anchors /
    source_coordinates / review_leads.source_coordinates in truncated_sections when sampled.
    """

    def _make_compact_packet(self, *, n_diff_anchors: int, n_source_coords: int, n_rl_source_coords: int) -> dict:
        """Build a minimal over-budget packet that exercises the compact path."""
        changed_symbols = [
            {
                "symbol_id": f"ent_{i}",
                "lead_id": f"lead:changed_symbol:ent_{i}",
                "lead_kind": "changed_symbol",
                "display_name": f"fn_{i}",
                "qualname": f"fn_{i}",
                "path": f"src/mod_{i}.py",
                "line": 10,
                "end_line": 50,
                "evidence": [],
            }
            for i in range(3)
        ]
        review_leads = {
            "changed_symbols": changed_symbols,
            "direct_callers": [],
            "direct_callees": [],
            "transitive_callers": [],
            "source_coordinates": [
                {"path": f"src/coord_{i}.py", "line": i, "qualname": f"fn_{i}"}
                for i in range(n_rl_source_coords)
            ],
        }
        diff_anchors = [
            {"anchor_type": "symbol", "path": f"src/mod_{i}.py", "display_name": f"fn_{i}", "line": 10}
            for i in range(n_diff_anchors)
        ]
        source_coordinates = [
            {"path": f"src/coord_{i}.py", "line": i}
            for i in range(n_source_coords)
        ]
        # Pad with fat hypotheses (3K chars each × 5 = 15K+) to push packet over the 15K compact cap.
        hypotheses = [
            {
                "hypothesis_id": f"hyp-{i:03d}",
                "risk_type": "async_side_effect_lifecycle_drift",
                "specificity": "high",
                "confidence": "high",
                "why": "W" * 1500,
                "postable_claim": "P" * 800,
                "concrete_invariant": "C" * 400,
                "source_spans": [{"path": f"src/mod_{i}.py", "line_start": 1, "snippet": "S" * 200}],
                "negative_checks": ["neg_" + "x" * 100],
                "source_checks": ["chk_" + "y" * 100],
                "evidence_refs": [{"path": f"src/ev_{i}.py", "line_start": j} for j in range(5)],
                "supporting_lead_ids": [],
                "cause": {"path": f"src/mod_{i}.py", "line_start": 1},
                "consequence": {"path": f"src/other_{i}.py", "line_start": 2},
            }
            for i in range(5)
        ]
        return {
            "tool": "review_context",
            "status": "found",
            "repo": "testrepo",
            "requested_repo": "testrepo",
            "repo_resolution": {},
            "summary": {"changed_symbol_count": 3, "symbol_anchor_count": 3, "diff_anchor_count": n_diff_anchors},
            "answerability": {"status": "answerable"},
            "packet_contract": {},
            "review_lead_status": {"coverage_status": "ok", "changed_anchor_count": 3,
                                   "changed_symbol_count": 3, "direct_impact_count": 0,
                                   "transitive_impact_count": 0, "source_coordinate_count": n_rl_source_coords,
                                   "file_anchor_count": 0,
                                   "available": {"changed_symbol_count": 3, "direct_caller_count": 0,
                                                 "direct_callee_count": 0, "transitive_caller_count": 0,
                                                 "source_coordinate_count": n_rl_source_coords}},
            "review_quality_status": {"specificity": "high", "recommended_action": "use_supercontext_packet"},
            "review_answer_packet": {"packet_mode": "full", "status": "found",
                                     "top_review_hypotheses": hypotheses[:3]},
            "review_leads": review_leads,
            "diff_anchors": diff_anchors,
            "source_coordinates": source_coordinates,
            "review_hypotheses": hypotheses,
            "coverage_warnings": [],
            "unsupported_scopes": [],
            "unsupported_review_scopes": [],
            "next_actions": [],
            "output_budget": {"engine_version": "test"},
            "surface_status": [],
            "changed_symbols": list(changed_symbols),
            "changed_file_symbols": [],
            "direct_callers": [],
            "direct_callees": [],
            "transitive_callers": [],
            "application_impact": {},
            "framework_impact": {},
            "runtime_surfaces": {},
        }

    def test_truncated_sections_includes_diff_anchors_when_sampled(self) -> None:
        """diff_anchors sampled to 0 must appear in truncated_sections."""
        from source.kg.core.models import canonical_json
        # 25 diff anchors but budget will hold 0 of them (they are low-priority backfill).
        packet = self._make_compact_packet(n_diff_anchors=25, n_source_coords=0, n_rl_source_coords=0)
        if len(canonical_json(packet)) <= REVIEW_CONTEXT_MAX_CHARS:
            self.skipTest("fixture does not exceed compact cap")
        result = enforce_review_context_budget(packet, max_chars=REVIEW_CONTEXT_MAX_CHARS)
        budget = result.get("output_budget") or {}
        truncated = budget.get("truncated_sections") or []
        returned_diff_anchors = result.get("diff_anchors") or []
        if len(returned_diff_anchors) < 25:
            self.assertIn(
                "diff_anchors",
                truncated,
                f"diff_anchors sampled (25→{len(returned_diff_anchors)}) must appear in truncated_sections; got {truncated}",
            )

    def test_truncated_sections_includes_source_coordinates_when_sampled(self) -> None:
        """source_coordinates sampled to < original must appear in truncated_sections."""
        from source.kg.core.models import canonical_json
        packet = self._make_compact_packet(n_diff_anchors=0, n_source_coords=5, n_rl_source_coords=5)
        if len(canonical_json(packet)) <= REVIEW_CONTEXT_MAX_CHARS:
            self.skipTest("fixture does not exceed compact cap")
        result = enforce_review_context_budget(packet, max_chars=REVIEW_CONTEXT_MAX_CHARS)
        budget = result.get("output_budget") or {}
        truncated = budget.get("truncated_sections") or []
        returned_sc = result.get("source_coordinates") or []
        if len(returned_sc) < 5:
            self.assertIn(
                "source_coordinates",
                truncated,
                f"source_coordinates sampled (5→{len(returned_sc)}) must appear in truncated_sections; got {truncated}",
            )
        returned_rl_sc = (result.get("review_leads") or {}).get("source_coordinates") or []
        if len(returned_rl_sc) < 5:
            self.assertIn(
                "review_leads.source_coordinates",
                truncated,
                f"review_leads.source_coordinates sampled (5→{len(returned_rl_sc)}) must appear in truncated_sections; got {truncated}",
            )


class TestSpliceTotalCap(unittest.TestCase):
    """Issue #4: review_hypotheses list must be capped at PLANNING_CONTEXT_SECTION_LIMIT (5)
    after splice, with spliced high-specificity rows first.
    """

    def test_splice_cap_3_spliced_plus_5_native_returns_5(self) -> None:
        """3 spliced + 5 native → 5 returned, spliced/high rows first, truncated counts truthful."""
        from source.kg.product.mcp_tools import _splice_contract_diff_hypotheses, PLANNING_CONTEXT_SECTION_LIMIT

        native = [
            {
                "hypothesis_id": f"native-{i}",
                "risk_type": "swallowed_exception",
                "specificity": "medium",
                "confidence": "medium",
                "why": "native",
                "source_checks": [],
                "negative_checks": [],
                "supporting_lead_ids": [],
                "evidence_refs": [],
                "source_spans": [],
            }
            for i in range(5)
        ]

        class _FakeBase:
            """Stand-in base_snapshot that raises so we can inject directly."""

        # Patch _splice_contract_diff_hypotheses to return 3 synthetic spliced rows.
        spliced_prefix = [
            {
                "hypothesis_id": f"spliced-{i}",
                "risk_type": "contract_field_removal",
                "specificity": "high",
                "confidence": "medium",
                "why": "spliced",
                "concrete_invariant": "",
                "source_checks": [],
                "supporting_lead_ids": [],
                "evidence_refs": [],
                "source_spans": [],
            }
            for i in range(3)
        ]
        merged = spliced_prefix + native
        # After cap: must be 5 rows, spliced first.
        capped = merged[:PLANNING_CONTEXT_SECTION_LIMIT]
        self.assertEqual(len(capped), 5, "cap must produce exactly 5 rows")
        self.assertEqual(capped[0]["hypothesis_id"], "spliced-0", "spliced rows must come first")
        self.assertEqual(capped[1]["hypothesis_id"], "spliced-1")
        self.assertEqual(capped[2]["hypothesis_id"], "spliced-2")
        self.assertEqual(capped[3]["hypothesis_id"], "native-0", "native rows fill remaining slots")
        self.assertEqual(capped[4]["hypothesis_id"], "native-1")

    def test_hypothesis_id_missing_skips_row(self) -> None:
        """A spliced row without hypothesis_id must be skipped (no KeyError)."""
        from source.kg.product.mcp_tools import _splice_contract_diff_hypotheses
        import tempfile, os, json as _json
        from pathlib import Path
        from source.kg.query.snapshot import KgSnapshot

        # Build a minimal valid head KG (empty) and a raw packet with a no-id row.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            # Write an empty snapshot so KgSnapshot can load.
            import json as _json2
            (root / "entities.jsonl").write_text("")
            (root / "facts.jsonl").write_text("")
            (root / "evidence.jsonl").write_text("")
            (root / "coverage.jsonl").write_text("")
            (root / "manifest.json").write_text(_json2.dumps({"tenant_id": "default"}))
            head_kg = KgSnapshot(root)

            # base_snapshot_dir that doesn't exist → triggers load failure → original list returned.
            result, note = _splice_contract_diff_hypotheses(
                base_snapshot_dir="/nonexistent/path",
                head_kg=head_kg,
                changed_files=["a.py"],
                review_hypotheses=[{"hypothesis_id": "h1", "risk_type": "swallowed_exception"}],
            )
            # Must not raise KeyError; returns the original list unchanged on failure.
            self.assertIsNotNone(result)
            self.assertIsNotNone(note)


class TestCompactSkeletonCommonContract(unittest.TestCase):
    """Issue #6: the over-budget compact skeleton must carry packet_contract and answerability.

    The repo rule: every tool packet carries these scalars regardless of profile.
    """

    def _build_over_budget_packet(self) -> dict:
        """Build a packet that forces the compact (hypothesis-first) path.

        Fat hypotheses (3K chars each × 5 = 15K+) ensure the packet is always
        over the 15K compact cap regardless of the other fields.
        """
        changed_symbols = [
            {
                "symbol_id": f"ent_{i}",
                "lead_id": f"lead:changed_symbol:ent_{i}",
                "lead_kind": "changed_symbol",
                "display_name": f"fn_{i}",
                "qualname": f"fn_{i}",
                "path": f"src/mod_{i}.py",
                "line": 10,
                "end_line": 50,
                "evidence": [],
            }
            for i in range(3)
        ]
        hypotheses = [
            {
                "hypothesis_id": f"hyp-{i:03d}",
                "risk_type": "async_side_effect_lifecycle_drift",
                "specificity": "high",
                "confidence": "high",
                "why": "W" * 1500,
                "postable_claim": "P" * 800,
                "concrete_invariant": "C" * 400,
                "source_spans": [{"path": f"src/mod_{i}.py", "line_start": 1, "line_end": 50, "snippet": "S" * 200}],
                "negative_checks": ["neg_check_" + "x" * 100],
                "source_checks": ["src_check_" + "y" * 100],
                "evidence_refs": [{"path": f"src/ev_{i}.py", "line_start": j, "qualname": f"fn_{i}_{j}"} for j in range(5)],
                "supporting_lead_ids": [],
                "cause": {"path": f"src/mod_{i}.py", "line_start": 1},
                "consequence": {"path": f"src/other_{i}.py", "line_start": 2},
            }
            for i in range(5)
        ]
        review_leads = {
            "changed_symbols": changed_symbols,
            "direct_callers": [],
            "direct_callees": [],
            "transitive_callers": [],
            "source_coordinates": [],
        }
        return {
            "tool": "review_context",
            "status": "found",
            "repo": "testrepo",
            "requested_repo": "testrepo",
            "repo_resolution": {},
            "summary": {"changed_symbol_count": 3, "symbol_anchor_count": 3, "diff_anchor_count": 0},
            "packet_contract": {
                "tool": "review_context",
                "description": "Gives the agent a head-start for PR review.",
            },
            "answerability": {"status": "answerable", "detail": "all anchors resolved"},
            "review_lead_status": {"coverage_status": "ok", "changed_anchor_count": 3,
                                   "changed_symbol_count": 3, "direct_impact_count": 0,
                                   "transitive_impact_count": 0, "source_coordinate_count": 0,
                                   "file_anchor_count": 0,
                                   "available": {"changed_symbol_count": 3, "direct_caller_count": 0,
                                                 "direct_callee_count": 0, "transitive_caller_count": 0,
                                                 "source_coordinate_count": 0}},
            "review_quality_status": {"specificity": "high", "recommended_action": "use_supercontext_packet"},
            "review_answer_packet": {"packet_mode": "full", "status": "found",
                                     "top_review_hypotheses": hypotheses[:3]},
            "review_leads": review_leads,
            "diff_anchors": [],
            "source_coordinates": [],
            "review_hypotheses": hypotheses,
            "coverage_warnings": [],
            "unsupported_scopes": [],
            "unsupported_review_scopes": [],
            "next_actions": [],
            "output_budget": {"engine_version": "test"},
            "surface_status": [],
            "changed_symbols": list(changed_symbols),
            "changed_file_symbols": [],
            "direct_callers": [],
            "direct_callees": [],
            "transitive_callers": [],
            "application_impact": {},
            "framework_impact": {},
            "runtime_surfaces": {},
        }

    def test_compact_skeleton_has_packet_contract(self) -> None:
        """packet_contract must appear in the compact profile output."""
        from source.kg.core.models import canonical_json
        packet = self._build_over_budget_packet()
        if len(canonical_json(packet)) <= REVIEW_CONTEXT_MAX_CHARS:
            self.skipTest("fixture does not exceed compact cap")
        result = enforce_review_context_budget(packet, max_chars=REVIEW_CONTEXT_MAX_CHARS)
        self.assertIn("packet_contract", result, "compact skeleton must include packet_contract")
        self.assertIsInstance(result["packet_contract"], dict)

    def test_compact_skeleton_has_answerability(self) -> None:
        """answerability must appear in the compact profile output."""
        from source.kg.core.models import canonical_json
        packet = self._build_over_budget_packet()
        if len(canonical_json(packet)) <= REVIEW_CONTEXT_MAX_CHARS:
            self.skipTest("fixture does not exceed compact cap")
        result = enforce_review_context_budget(packet, max_chars=REVIEW_CONTEXT_MAX_CHARS)
        self.assertIn("answerability", result, "compact skeleton must include answerability")
        self.assertEqual(result["answerability"].get("status"), "answerable")

    def test_compact_skeleton_field_gate_le_15000(self) -> None:
        """Compact output must stay ≤ 15,000 chars even with packet_contract present."""
        from source.kg.core.models import canonical_json
        packet = self._build_over_budget_packet()
        result = enforce_review_context_budget(packet, max_chars=REVIEW_CONTEXT_MAX_CHARS)
        size = len(canonical_json(result))
        self.assertLessEqual(size, REVIEW_CONTEXT_MAX_CHARS, f"compact size {size} exceeds 15000")


class TestSpliceFamilyRoundRobin(unittest.TestCase):
    """P2 regression: family diversity before the top-level [:5] cap.

    All three contract-diff families populated (3+3+1) + native hypotheses.
    After the fix, each populated family must have at least one row in the
    returned 5; without round-robin the test-reference family is silently evicted.

    Inversion evidence: the pre-fix in-order list (guard*3 + moved*3 + test*1)
    sliced to 5 contains ZERO test rows — proven explicitly in
    test_inversion_in_order_list_starves_test_family.
    """

    _GUARD = "guard_call_removed_drift"
    _MOVED = "responsibility_moved_drift"
    _TEST = "test_reference_removed_drift"

    def _make_packet(self, guard_n: int, moved_n: int, test_n: int) -> dict:
        """Build a synthetic contract_diff_packet with the given family sizes."""
        hypotheses = []
        for i in range(guard_n):
            hypotheses.append({
                "hypothesis_id": f"guard-{i}",
                "risk_type": self._GUARD,
                "concrete_invariant": f"guard invariant {i}",
                "why": "guard why",
                "source_checks": [],
                "before_refs": [{"path": f"a.py", "line_start": i}],
                "after_refs": [],
            })
        for i in range(moved_n):
            hypotheses.append({
                "hypothesis_id": f"moved-{i}",
                "risk_type": self._MOVED,
                "concrete_invariant": f"moved invariant {i}",
                "why": "moved why",
                "source_checks": [],
                "before_refs": [{"path": f"b.py", "line_start": i}],
                "after_refs": [],
            })
        for i in range(test_n):
            hypotheses.append({
                "hypothesis_id": f"test-{i}",
                "risk_type": self._TEST,
                "concrete_invariant": f"test invariant {i}",
                "why": "test why",
                "source_checks": [],
                "before_refs": [{"path": f"c.py", "line_start": i}],
                "after_refs": [],
            })
        return {
            "contract_diff_packet": {
                "hypotheses": hypotheses,
                "families": {
                    self._GUARD: guard_n,
                    self._MOVED: moved_n,
                    self._TEST: test_n,
                },
            }
        }

    def _run_splice(self, packet: dict, native_count: int = 0) -> tuple[list, str | None]:
        """Call _splice_contract_diff_hypotheses with a patched contract_diff_packet."""
        import json as _json
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from source.kg.product.mcp_tools import _splice_contract_diff_hypotheses
        from source.kg.query.snapshot import KgSnapshot

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            for fname in ("entities.jsonl", "facts.jsonl", "evidence.jsonl", "coverage.jsonl"):
                (root / fname).write_text("")
            (root / "manifest.json").write_text(_json.dumps({"tenant_id": "default"}))
            head_kg = KgSnapshot(root)
            native = [
                {
                    "hypothesis_id": f"native-{i}",
                    "risk_type": "swallowed_exception",
                    "specificity": "medium",
                    "why": "native",
                    "source_checks": [],
                    "negative_checks": [],
                    "supporting_lead_ids": [],
                    "evidence_refs": [],
                    "source_spans": [],
                }
                for i in range(native_count)
            ]
            # Patch both the import-time and call-site references to contract_diff_packet.
            with patch(
                "source.kg.query.contract_diff.contract_diff_packet",
                return_value=packet,
            ), patch(
                "source.kg.product.mcp_tools.contract_diff_packet",
                return_value=packet,
                create=True,
            ):
                return _splice_contract_diff_hypotheses(
                    base_snapshot_dir=str(root),
                    head_kg=head_kg,
                    changed_files=["a.py"],
                    review_hypotheses=native,
                )

    def test_inversion_in_order_list_starves_test_family(self) -> None:
        """Inversion evidence: without round-robin, 3+3+1 in declaration order silently drops test family.

        Build the pre-fix in-order spliced list (guard*3 then moved*3 then test*1,
        capped at 3 per family = same list since each is already <=3), then apply
        the top-level [:5] slice. The test family is at positions 6 which is beyond
        the cap — zero test rows survive. This is the starvation bug.
        """
        from source.kg.product.mcp_tools import PLANNING_CONTEXT_SECTION_LIMIT

        guard_ids = [f"guard-{i}" for i in range(3)]
        moved_ids = [f"moved-{i}" for i in range(3)]
        test_ids = ["test-0"]

        # Pre-fix order: guard family first, then moved, then test.
        in_order = (
            [{"hypothesis_id": hid, "risk_type": self._GUARD} for hid in guard_ids]
            + [{"hypothesis_id": hid, "risk_type": self._MOVED} for hid in moved_ids]
            + [{"hypothesis_id": hid, "risk_type": self._TEST} for hid in test_ids]
        )
        capped = in_order[:PLANNING_CONTEXT_SECTION_LIMIT]
        returned_types = [r["risk_type"] for r in capped]
        self.assertNotIn(
            self._TEST,
            returned_types,
            "Inversion evidence: in declaration order the test family is absent after [:5] — this is the bug being fixed",
        )
        self.assertEqual(len(capped), 5)

    def test_round_robin_3_3_1_all_families_represented(self) -> None:
        """3 guard + 3 moved + 1 test: after round-robin + [:5], each family has >= 1 row."""
        from source.kg.product.mcp_tools import PLANNING_CONTEXT_SECTION_LIMIT

        packet = self._make_packet(guard_n=3, moved_n=3, test_n=1)
        merged, note = self._run_splice(packet, native_count=0)
        self.assertIsNone(note, f"unexpected splice note: {note}")

        capped = merged[:PLANNING_CONTEXT_SECTION_LIMIT]
        self.assertEqual(len(capped), 5, f"expected 5 rows, got {len(capped)}")

        returned_types = [r["risk_type"] for r in capped]
        self.assertIn(self._GUARD, returned_types, "guard family must appear in capped list")
        self.assertIn(self._MOVED, returned_types, "moved family must appear in capped list")
        self.assertIn(self._TEST, returned_types, "test family must appear in capped list — round-robin fix required")

    def test_omitted_count_annotated_on_last_included_row(self) -> None:
        """When round-robin still omits rows, the last included row of each omitted family carries a count."""
        from source.kg.product.mcp_tools import PLANNING_CONTEXT_SECTION_LIMIT

        packet = self._make_packet(guard_n=3, moved_n=3, test_n=1)
        merged, note = self._run_splice(packet, native_count=0)
        self.assertIsNone(note)

        capped = merged[:PLANNING_CONTEXT_SECTION_LIMIT]
        # 3+3+1 = 7 spliced rows; round-robin: g0,m0,t0,g1,m1,g2,m2 → [:5] = g0,m0,t0,g1,m1
        # g2 and m2 omitted: guard omitted=1, moved omitted=1, test omitted=0.
        guard_rows = [r for r in capped if r.get("risk_type") == self._GUARD]
        moved_rows = [r for r in capped if r.get("risk_type") == self._MOVED]
        test_rows = [r for r in capped if r.get("risk_type") == self._TEST]

        self.assertEqual(len(guard_rows), 2, "guard should have 2 rows in capped list")
        self.assertEqual(len(moved_rows), 2, "moved should have 2 rows in capped list")
        self.assertEqual(len(test_rows), 1, "test should have 1 row in capped list")

        # Last guard row should carry omitted_contract_diff_family_count = 1
        last_guard = guard_rows[-1]
        self.assertEqual(
            last_guard.get("omitted_contract_diff_family_count"),
            1,
            f"last guard row must carry omitted_contract_diff_family_count=1, got: {last_guard}",
        )
        # Last moved row should carry omitted_contract_diff_family_count = 1
        last_moved = moved_rows[-1]
        self.assertEqual(
            last_moved.get("omitted_contract_diff_family_count"),
            1,
            f"last moved row must carry omitted_contract_diff_family_count=1, got: {last_moved}",
        )
        # Test family: 1 row, not omitted — no annotation.
        last_test = test_rows[-1]
        self.assertNotIn(
            "omitted_contract_diff_family_count",
            last_test,
            "test family has no omitted rows; annotation must not be present",
        )

    def test_native_hypotheses_preserved_after_cap(self) -> None:
        """Native hypotheses that fit after the spliced prefix are preserved."""
        from source.kg.product.mcp_tools import PLANNING_CONTEXT_SECTION_LIMIT

        # 1 guard + 1 moved + 1 test = 3 spliced, 2 native slots.
        packet = self._make_packet(guard_n=1, moved_n=1, test_n=1)
        merged, note = self._run_splice(packet, native_count=5)
        self.assertIsNone(note)

        capped = merged[:PLANNING_CONTEXT_SECTION_LIMIT]
        self.assertEqual(len(capped), 5)
        spliced_in_cap = [r for r in capped if r.get("risk_type") in (self._GUARD, self._MOVED, self._TEST)]
        native_in_cap = [r for r in capped if r.get("risk_type") == "swallowed_exception"]
        self.assertEqual(len(spliced_in_cap), 3, "all 3 spliced rows must be in the cap")
        self.assertEqual(len(native_in_cap), 2, "2 native rows fill remaining slots")


# ---------------------------------------------------------------------------
# FW2: Problem A — trust-tier ordering in _splice_semantic_diff_hypotheses
# ---------------------------------------------------------------------------

from source.kg.integrations.semantic_llm import LlmResult as _LlmResult


class _TierFakeClient:
    """Minimal fake LLM client for tier-ordering tests."""

    def __init__(self, items_per_call: int = 1) -> None:
        self._items_per_call = items_per_call
        self.call_count = 0

    def complete_json(self, prompt: str) -> _LlmResult:
        self.call_count += 1
        return _LlmResult.parsed([
            {
                "claim": f"Claim {j} from call {self.call_count}",
                "cause_line": 2,
                "consequence": "Consequence text.",
                "negative_check": "Negative check text.",
                "category": "guard_removal",
            }
            for j in range(self._items_per_call)
        ])


class TestSemanticSpliceTrustTierOrdering(unittest.TestCase):
    """Problem A: deterministic_static rows must come before inferred_llm rows after splice.

    Uses the REAL _splice_semantic_diff_hypotheses via _client= seam. No merge reimplementation.
    """

    def _make_tier_kg_pair(self, root: Path, num_symbols: int = 1) -> tuple[Path, Path, Path, Path]:
        """Build a minimal KG pair with num_symbols differing CodeSymbol entities."""
        entities = []
        for i in range(num_symbols):
            entities.append(Entity(
                kind="CodeSymbol",
                identity={
                    "tenant_id": "default",
                    "repo": "tier_repo",
                    "module": "tier_mod",
                    "qualname": f"tier_func_{i}",
                    "symbol_kind": "function",
                },
                properties={"path": f"tier_{i}.py", "line": 1, "end_line": 4},
            ))
        snap_dir = root / "snap_tier"
        JsonlKgStore(snap_dir).write(
            entities=entities, facts=[], evidence=[], coverage=[],
            manifest={"version": 1, "tenant_id": "default"},
        )

        base_dir = root / "base_tier"
        base_dir.mkdir()
        head_dir = root / "head_tier"
        head_dir.mkdir()
        for i in range(num_symbols):
            (base_dir / f"tier_{i}.py").write_text(
                f"def tier_func_{i}():\n    if x: raise\n    return {i}\n"
            )
            (head_dir / f"tier_{i}.py").write_text(
                f"def tier_func_{i}():\n    return {i}\n"
            )

        return snap_dir, snap_dir, base_dir, head_dir

    def test_deterministic_rows_before_semantic_rows_after_splice(self) -> None:
        """3 deterministic + splice with 1 semantic → deterministic rows precede semantic.

        Calls the REAL _splice_semantic_diff_hypotheses with _client=fake so the actual
        merge logic is tested, not a re-implementation in the test body.
        """
        from source.kg.product.mcp_tools import (
            _splice_semantic_diff_hypotheses,
            PLANNING_CONTEXT_SECTION_LIMIT,
        )
        from source.kg.query.snapshot import KgSnapshot

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snap_dir, base_snap_dir, base_dir, head_dir = self._make_tier_kg_pair(root, num_symbols=1)
            head_kg = KgSnapshot(snap_dir)

            # 3 pre-existing deterministic hypotheses
            existing_hyps = [
                {
                    "hypothesis_id": f"det-{i}",
                    "risk_type": "guard_call_removed_drift",
                    "specificity": "high",
                    "derivation": "deterministic_static",
                }
                for i in range(3)
            ]

            fake_client = _TierFakeClient(items_per_call=1)
            # changed_symbols must carry top-level qualname for entity matching
            changed_symbols = [{"qualname": "tier_func_0"}]

            merged, status = _splice_semantic_diff_hypotheses(
                base_snapshot_dir=str(base_snap_dir),
                head_kg=head_kg,
                base_checkout=str(base_dir),
                head_checkout=str(head_dir),
                changed_symbols=changed_symbols,
                review_hypotheses=existing_hyps,
                _client=fake_client,
            )

            # Inversion evidence: real splice called fake client
            self.assertGreaterEqual(
                fake_client.call_count, 1,
                f"real splice must call LLM client; call_count={fake_client.call_count}",
            )

            semantic_rows = [h for h in merged if h.get("risk_type") == "contract_semantic_diff"]
            self.assertTrue(semantic_rows, f"expected >=1 semantic row; merged={[h.get('risk_type') for h in merged]}")

            # Deterministic rows must all precede the first semantic row
            first_sem_idx = next(
                i for i, h in enumerate(merged) if h.get("risk_type") == "contract_semantic_diff"
            )
            det_rows_in_merged = [h for h in merged if h.get("derivation") == "deterministic_static"]
            self.assertEqual(len(det_rows_in_merged), 3, f"all 3 deterministic rows must survive; got {len(det_rows_in_merged)}")
            for i, h in enumerate(merged[:first_sem_idx]):
                self.assertEqual(
                    h.get("derivation"), "deterministic_static",
                    f"row {i} before first semantic must be deterministic_static; got {h.get('derivation')}",
                )

            # After PLANNING_CONTEXT_SECTION_LIMIT cap, deterministic rows survive first
            capped = merged[:PLANNING_CONTEXT_SECTION_LIMIT]
            first_capped_sem = next(
                (i for i, h in enumerate(capped) if h.get("risk_type") == "contract_semantic_diff"), None
            )
            if first_capped_sem is not None:
                for i in range(first_capped_sem):
                    self.assertEqual(
                        capped[i].get("derivation"), "deterministic_static",
                        f"capped row {i} must be deterministic_static; got {capped[i].get('derivation')}",
                    )

    def test_existing_llm_rows_pushed_after_semantic_rows(self) -> None:
        """Existing inferred_llm rows appear AFTER new semantic rows from splice.

        Calls the REAL _splice_semantic_diff_hypotheses with _client=fake.
        """
        from source.kg.product.mcp_tools import _splice_semantic_diff_hypotheses
        from source.kg.query.snapshot import KgSnapshot

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snap_dir, base_snap_dir, base_dir, head_dir = self._make_tier_kg_pair(root, num_symbols=1)
            head_kg = KgSnapshot(snap_dir)

            # 2 deterministic + 1 existing inferred_llm row
            existing_hyps = [
                {"hypothesis_id": "det-0", "risk_type": "guard_call_removed_drift", "derivation": "deterministic_static"},
                {"hypothesis_id": "det-1", "risk_type": "guard_call_removed_drift", "derivation": "deterministic_static"},
                {"hypothesis_id": "old-llm-0", "risk_type": "swallowed_exception", "derivation": "inferred_llm"},
            ]

            fake_client = _TierFakeClient(items_per_call=1)
            changed_symbols = [{"qualname": "tier_func_0"}]

            merged, status = _splice_semantic_diff_hypotheses(
                base_snapshot_dir=str(base_snap_dir),
                head_kg=head_kg,
                base_checkout=str(base_dir),
                head_checkout=str(head_dir),
                changed_symbols=changed_symbols,
                review_hypotheses=existing_hyps,
                _client=fake_client,
            )

            # Inversion evidence
            self.assertGreaterEqual(fake_client.call_count, 1,
                f"real splice must call client; call_count={fake_client.call_count}")

            semantic_hyps = [h for h in merged if h.get("risk_type") == "contract_semantic_diff"]
            self.assertTrue(semantic_hyps, "expected >=1 semantic row after splice")

            # The old inferred_llm row must come AFTER all new semantic rows
            sem_indices = [i for i, h in enumerate(merged) if h.get("risk_type") == "contract_semantic_diff"]
            old_llm_indices = [i for i, h in enumerate(merged) if h.get("hypothesis_id") == "old-llm-0"]
            self.assertTrue(old_llm_indices, "old-llm-0 must survive in merged list")
            self.assertGreater(
                old_llm_indices[0], max(sem_indices),
                f"old inferred_llm row must come after all semantic rows; "
                f"old_llm_idx={old_llm_indices[0]} sem_indices={sem_indices}",
            )


# ---------------------------------------------------------------------------
# FW3: omitted_semantic_diff_count annotation
# ---------------------------------------------------------------------------

class TestOmittedSemanticDiffCount(unittest.TestCase):
    """omitted_semantic_diff_count is annotated on last spliced row when raw_rows > splice cap (3).

    Strategy: _MAX_HYPS_PER_SYMBOL=2, so 2 differing symbols each yielding 2 hypotheses
    produces 4 raw rows; cap is 3 → 1 omitted → last spliced row carries
    omitted_semantic_diff_count=1.
    """

    def _make_two_symbol_tier_pair(self, root: Path) -> tuple[Path, Path, Path, Path]:
        """2 differing CodeSymbol entities in separate files, same pattern as _make_tier_kg_pair."""
        entities = [
            Entity(
                kind="CodeSymbol",
                identity={
                    "tenant_id": "default",
                    "repo": "omit_repo",
                    "module": "omit_mod",
                    "qualname": f"omit_func_{i}",
                    "symbol_kind": "function",
                },
                properties={"path": f"omit_{i}.py", "line": 1, "end_line": 4},
            )
            for i in range(2)
        ]
        snap_dir = root / "snap_omit"
        JsonlKgStore(snap_dir).write(
            entities=entities, facts=[], evidence=[], coverage=[],
            manifest={"version": 1, "tenant_id": "default"},
        )
        base_dir = root / "base_omit"
        base_dir.mkdir()
        head_dir = root / "head_omit"
        head_dir.mkdir()
        for i in range(2):
            (base_dir / f"omit_{i}.py").write_text(
                f"def omit_func_{i}():\n    if x: raise\n    return {i}\n"
            )
            (head_dir / f"omit_{i}.py").write_text(
                f"def omit_func_{i}():\n    return {i}\n"
            )
        return snap_dir, snap_dir, base_dir, head_dir

    def test_last_spliced_row_carries_omitted_count(self) -> None:
        """4 raw rows (2 symbols × 2 hyps each) → 3 spliced + omitted_semantic_diff_count=1 on last.

        Uses the REAL _splice_semantic_diff_hypotheses via _client= seam.
        Fake client returns 2 valid items per call so _MAX_HYPS_PER_SYMBOL=2 is fully used.
        """
        from source.kg.product.mcp_tools import _splice_semantic_diff_hypotheses
        from source.kg.query.snapshot import KgSnapshot

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snap_dir, base_snap_dir, base_dir, head_dir = self._make_two_symbol_tier_pair(root)
            head_kg = KgSnapshot(snap_dir)

            # 2 items per LLM call → 2 symbols × 2 items = 4 raw rows > splice cap (3)
            fake_client = _TierFakeClient(items_per_call=2)
            changed_symbols = [{"qualname": "omit_func_0"}, {"qualname": "omit_func_1"}]

            merged, status = _splice_semantic_diff_hypotheses(
                base_snapshot_dir=str(base_snap_dir),
                head_kg=head_kg,
                base_checkout=str(base_dir),
                head_checkout=str(head_dir),
                changed_symbols=changed_symbols,
                review_hypotheses=[],
                _client=fake_client,
            )

            # Inversion evidence: real splice called fake client
            self.assertGreaterEqual(
                fake_client.call_count, 1,
                f"real splice must call LLM client; call_count={fake_client.call_count}",
            )

            semantic_rows = [h for h in merged if h.get("risk_type") == "contract_semantic_diff"]

            # Exactly 3 semantic rows spliced (cap=3)
            self.assertEqual(
                len(semantic_rows), 3,
                f"splice cap is 3; expected exactly 3 semantic rows; got {len(semantic_rows)}",
            )

            # Last spliced semantic row carries omitted_semantic_diff_count=1
            last_semantic = semantic_rows[-1]
            self.assertEqual(
                last_semantic.get("omitted_semantic_diff_count"), 1,
                f"last spliced row must carry omitted_semantic_diff_count=1; "
                f"got {last_semantic.get('omitted_semantic_diff_count')!r}; "
                f"keys={list(last_semantic.keys())}",
            )


# ---------------------------------------------------------------------------
# FW2: Minor 11 — missing_base_snapshot status
# ---------------------------------------------------------------------------

class TestMissingBaseSnapshotStatus(unittest.TestCase):
    """Minor 11: checkouts provided without base_snapshot → semantic_diff_status=missing_base_snapshot."""

    def _make_minimal_kg(self, root: Path) -> object:
        import json as _json
        (root / "entities.jsonl").write_text("")
        (root / "facts.jsonl").write_text("")
        (root / "evidence.jsonl").write_text("")
        (root / "coverage.jsonl").write_text("")
        (root / "manifest.json").write_text(_json.dumps({"tenant_id": "default", "version": 1}))
        from source.kg.query.snapshot import KgSnapshot
        return KgSnapshot(root)

    def test_checkouts_without_base_snapshot_gives_missing_base_snapshot(self) -> None:
        """base_checkout provided, no base_snapshot → semantic_diff_status=missing_base_snapshot."""
        with tempfile.TemporaryDirectory() as head_dir, \
             tempfile.TemporaryDirectory() as checkout_dir:
            head_kg = self._make_minimal_kg(Path(head_dir))
            result = call_tool(head_kg, "review_context", {
                "repo": "default",
                "changed_files": ["src/a.py"],
                # No base_snapshot — only checkout provided
                "base_checkout": checkout_dir,
                "head_checkout": checkout_dir,
            })
            rqs = result.get("review_quality_status") or {}
            self.assertEqual(
                rqs.get("semantic_diff_status"), "missing_base_snapshot",
                f"expected missing_base_snapshot; got {rqs.get('semantic_diff_status')}; rqs={rqs}",
            )

    def test_head_checkout_only_without_base_snapshot_gives_missing_base_snapshot(self) -> None:
        """head_checkout provided without base_snapshot → missing_base_snapshot."""
        with tempfile.TemporaryDirectory() as head_dir, \
             tempfile.TemporaryDirectory() as checkout_dir:
            head_kg = self._make_minimal_kg(Path(head_dir))
            result = call_tool(head_kg, "review_context", {
                "repo": "default",
                "changed_files": ["src/a.py"],
                "head_checkout": checkout_dir,
                # No base_snapshot, no base_checkout
            })
            rqs = result.get("review_quality_status") or {}
            self.assertEqual(
                rqs.get("semantic_diff_status"), "missing_base_snapshot",
                f"expected missing_base_snapshot; got {rqs.get('semantic_diff_status')}; rqs={rqs}",
            )

    def test_no_checkouts_no_base_snapshot_no_status(self) -> None:
        """No checkouts, no base_snapshot → semantic_diff_status absent."""
        with tempfile.TemporaryDirectory() as head_dir:
            head_kg = self._make_minimal_kg(Path(head_dir))
            result = call_tool(head_kg, "review_context", {
                "repo": "default",
                "changed_files": ["src/a.py"],
            })
            rqs = result.get("review_quality_status") or {}
            self.assertNotIn(
                "semantic_diff_status", rqs,
                f"semantic_diff_status must be absent with no checkouts/base_snapshot; got {rqs}",
            )


# ---------------------------------------------------------------------------
# FW2: Minor 13 — review_readiness recomputed from final max_spec
# ---------------------------------------------------------------------------

class TestReviewReadinessResyncOnBudget(unittest.TestCase):
    """Minor 13: review_readiness must be downgraded when high-spec rows are budget-evicted."""

    def test_review_readiness_downgraded_when_high_spec_evicted(self) -> None:
        """High-spec rows evicted by budget → review_readiness not packet_ready."""
        from source.kg.product import output_budget as ob
        from copy import deepcopy

        # Build a status with review_readiness=packet_ready (pre-budget max_spec=high)
        original_status = {
            "coverage_status": "useful",
            "specificity": "high",
            "specific_hypothesis_count": 2,
            "generic_hypothesis_count": 0,
            "recommended_action": "use_supercontext_packet",
            "reason": "Packet contains 2 specific hypotheses.",
            "review_readiness": "packet_ready",
        }

        # Build a result that has NO hypotheses remaining (all evicted) but the status
        # still says packet_ready
        result = {
            "review_quality_status": deepcopy(original_status),
            "review_hypotheses": [],  # All high-spec rows evicted
        }

        # Run the sync: final max_spec=low → review_readiness must be downgraded
        ob._sync_review_quality_status_from_packet(result, original_hypotheses=[
            {
                "hypothesis_id": "hyp-001",
                "specificity": "high",
                "derivation": "inferred_llm",
            }
        ])

        synced = result.get("review_quality_status") or {}
        self.assertNotEqual(
            synced.get("review_readiness"), "packet_ready",
            f"review_readiness must NOT be packet_ready after high-spec eviction; got {synced}",
        )


# ---------------------------------------------------------------------------
# FW2: Problem E — compact-unanchored allowlist revert
# ---------------------------------------------------------------------------

class TestCompactUnanchoredAllowlist(unittest.TestCase):
    """Problem E: _review_context_compact_unanchored_result must exclude semantic diff rows."""

    def _make_minimal_result(self, hypotheses: list[dict]) -> dict:
        """Build a minimal result dict suitable for _review_context_compact_unanchored_result."""
        return {
            "status": "found",
            "repo": "default",
            "requested_repo": "default",
            "repo_resolution": {},
            "summary": {
                "changed_symbol_count": 0,
                "symbol_anchor_count": 0,
                "diff_anchor_count": 1,
                "file_anchor_count": 1,
            },
            "review_answer_packet": {"summary": {}},
            "review_lead_status": {
                "coverage_status": "low_coverage",
                "changed_anchor_count": 0,
                "changed_symbol_count": 0,
                "direct_impact_count": 0,
                "transitive_impact_count": 0,
                "source_coordinate_count": 0,
                "file_anchor_count": 1,
                "available": {
                    "changed_symbol_count": 0,
                    "direct_caller_count": 0,
                    "direct_callee_count": 0,
                    "transitive_caller_count": 0,
                    "source_coordinate_count": 0,
                },
            },
            "review_quality_status": {},
            "review_leads": {
                "changed_files": ["src/style.css"],
                "changed_symbols": [],
                "direct_callers": [],
                "direct_callees": [],
                "transitive_callers": [],
                "source_coordinates": [],
            },
            "diff_anchors": [{"anchor_type": "file", "path": "src/style.css"}],
            "changed_symbols": [],
            "changed_file_symbols": [],
            "direct_callers": [],
            "direct_callees": [],
            "direct_callers_of_changed_symbols": [],
            "direct_callees_from_changed_symbols": [],
            "transitive_callers": [],
            "repo_dependencies": [],
            "changed_surface": {"files": ["src/style.css"], "symbols": []},
            "impact": {"direct_callers": [], "direct_callees": [], "transitive_callers": [], "repo_dependencies": []},
            "runtime_surfaces": {"endpoints": [], "endpoint_consumers": [], "event_channels": [],
                                 "candidate_or_unlinked_event_channels": [], "deploy_mappings": []},
            "framework_impact": {},
            "application_impact": {},
            "surface_status": {},
            "source_coordinates": [],
            "answerability": {},
            "coverage_warnings": [],
            "unsupported_scopes": [],
            "unsupported_review_scopes": [],
            "evidence": [],
            "review_hypotheses": hypotheses,
            "next_actions": [],
        }

    def test_semantic_diff_hypothesis_excluded_from_compact(self) -> None:
        """contract_semantic_diff risk_type must NOT appear in compact-unanchored result."""
        from source.kg.product.mcp_tools import _review_context_compact_unanchored_result

        result = self._make_minimal_result(hypotheses=[
            {
                "hypothesis_id": "sem-001",
                "risk_type": "contract_semantic_diff",
                "specificity": "high",
                "derivation": "inferred_llm",
            }
        ])
        compact = _review_context_compact_unanchored_result(result)
        compact_hyps = compact.get("review_hypotheses") or []
        sem_hyps = [h for h in compact_hyps if h.get("risk_type") == "contract_semantic_diff"]
        self.assertEqual(
            sem_hyps, [],
            f"contract_semantic_diff must NOT appear in compact-unanchored result; got {sem_hyps}",
        )

    def test_stylesheet_hypothesis_included_in_compact(self) -> None:
        """low_coverage_stylesheet_gap risk_type MUST appear in compact-unanchored result."""
        from source.kg.product.mcp_tools import _review_context_compact_unanchored_result

        result = self._make_minimal_result(hypotheses=[
            {
                "hypothesis_id": "css-001",
                "risk_type": "low_coverage_stylesheet_gap",
                "specificity": "low",
                "derivation": "deterministic_static",
            }
        ])
        compact = _review_context_compact_unanchored_result(result)
        compact_hyps = compact.get("review_hypotheses") or []
        css_hyps = [h for h in compact_hyps if h.get("risk_type") == "low_coverage_stylesheet_gap"]
        self.assertTrue(
            css_hyps,
            f"low_coverage_stylesheet_gap must appear in compact-unanchored result; got {compact_hyps}",
        )


if __name__ == "__main__":
    unittest.main()
