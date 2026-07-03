"""Task P: cluster-anchored budget policy (P1) and truncation_summary (P2).

Spec coverage:
  1. Multi-file over-budget packet: every cluster retains >= 1 changed-symbol anchor.
  2. Extreme pressure: lowest-ranked cluster dropped whole; its symbols appear in
     omitted_high_risk_clusters; kept clusters intact.
  3. truncation_summary shape + bounds; absent when nothing truncated.
  4. Hypothesis anchor bound: returned hypothesis carries up to 4 evidence_refs.
  5. End-to-end enforce_review_context_budget on a multi-file fixture asserting
     cluster coverage in the live packet.
  6. All existing budget/floor/mirror invariants pass (verified by running full suite).
"""
from __future__ import annotations

import unittest
from copy import deepcopy

from source.kg.core.models import canonical_json
from source.kg.product.output_budget import enforce_review_context_budget
from source.kg.product.review_attribution import add_review_lead_ids, review_available_counts


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _make_changed_symbol(lead_id: str, path: str, name: str) -> dict:
    return {
        "lead_id": lead_id,
        "lead_kind": "changed_symbol",
        "qualname": name,
        "name": name,
        "path": path,
        "repo": "repo-a",
        "line_start": 10,
        "line_end": 30,
    }


def _make_caller_row(lead_id: str, path: str, subject: str, object_: str) -> dict:
    return {
        "lead_id": lead_id,
        "lead_kind": "direct_caller",
        "subject": subject,
        "object": object_,
        "path": path,
        "repo": "repo-a",
        "line_start": 5,
        "line_end": 10,
    }


def _make_hypothesis(risk_type: str, supporting_lead_ids: list[str], evidence_refs: list[dict]) -> dict:
    return {
        "hypothesis_id": f"hypothesis:{risk_type}:deadbeef00000001",
        "risk_type": risk_type,
        "confidence": "medium",
        "why": f"Risk of type {risk_type}.",
        "supporting_lead_ids": list(supporting_lead_ids),
        "evidence_refs": list(evidence_refs),
        "source_checks": [{"reason": "check", "anchor": "src", "repo": "repo-a", "path": "src/a.py"}],
    }


def _make_evidence_ref(path: str, line: int = 1) -> dict:
    return {"repo": "repo-a", "path": path, "line_start": line, "line_end": line + 5}


def _make_over_budget_packet(
    file_count: int = 3,
    symbols_per_file: int = 8,
    hyp_cluster_paths: list[str] | None = None,
) -> dict:
    """Build a well-formed review_context packet big enough to exceed REVIEW_CONTEXT_MAX_CHARS.

    file_count files, each with symbols_per_file changed symbols.
    hyp_cluster_paths: paths whose lead_ids a hypothesis will reference (default: first 2 files).
    """
    files = [f"src/module_{i}.py" for i in range(file_count)]
    if hyp_cluster_paths is None:
        hyp_cluster_paths = files[:2]

    changed_symbols: list[dict] = []
    for fi, fpath in enumerate(files):
        for si in range(symbols_per_file):
            lid = f"lead:changed_symbol:file{fi}_sym{si}"
            changed_symbols.append(_make_changed_symbol(lid, fpath, f"func_{fi}_{si}"))

    direct_callers: list[dict] = []
    for fi, fpath in enumerate(files):
        for si in range(4):
            lid = f"lead:direct_caller:file{fi}_caller{si}"
            direct_callers.append(_make_caller_row(lid, fpath, f"caller_{fi}_{si}", f"func_{fi}_0"))

    direct_callees: list[dict] = []
    for fi, fpath in enumerate(files):
        for si in range(4):
            lid = f"lead:direct_callee:file{fi}_callee{si}"
            direct_callees.append(_make_caller_row(lid, fpath, f"func_{fi}_0", f"callee_{fi}_{si}"))

    # Build hypothesis referencing the first 2 symbols per hyp_cluster_paths file
    hyp_lead_ids = []
    for fpath in hyp_cluster_paths:
        fi = files.index(fpath)
        for si in range(2):
            hyp_lead_ids.append(f"lead:changed_symbol:file{fi}_sym{si}")

    evidence_refs = [_make_evidence_ref(path, 10 + i * 5) for i, path in enumerate(hyp_cluster_paths)]
    hypothesis = _make_hypothesis("direct_call_contract_drift", hyp_lead_ids, evidence_refs)

    leads_pre: dict = {
        "changed_files": list(files),
        "changed_symbols": list(changed_symbols),
        "direct_callers": list(direct_callers),
        "direct_callees": list(direct_callees),
        "transitive_callers": [],
        "source_coordinates": [],
    }
    leads = add_review_lead_ids(leads_pre)

    available = review_available_counts(
        changed_symbols=changed_symbols,
        direct_callers=direct_callers,
        direct_callees=direct_callees,
        transitive_callers=[],
        source_coordinates=[],
    )

    lead_status = {
        "coverage_status": "useful",
        "recommended_action": "use_supercontext_packet",
        "changed_anchor_count": len(changed_symbols),
        "changed_symbol_count": len(changed_symbols),
        "direct_impact_count": len(direct_callers) + len(direct_callees),
        "transitive_impact_count": 0,
        "source_coordinate_count": 0,
        "file_anchor_count": file_count,
        "available": available,
        "returned": {
            "changed_symbol_count": len(changed_symbols),
            "direct_caller_count": len(direct_callers),
            "direct_callee_count": len(direct_callees),
            "transitive_caller_count": 0,
            "source_coordinate_count": 0,
        },
    }

    # Build large bulk rows to push the packet well over 40000 chars
    # Use application_impact to add bulk without touching leads
    bulk_rows = [
        {
            "subject": f"ServiceA",
            "object": f"ServiceB_{i}",
            "predicate": "calls",
            "path": f"src/module_{i % file_count}.py",
            "detail": "x" * 600,
        }
        for i in range(100)
    ]

    review_answer_packet: dict = {
        "top_changed_symbols": [dict(s) for s in leads["changed_symbols"][:3]],
        "top_direct_callers": [dict(r) for r in leads["direct_callers"][:3]],
        "top_direct_callees": [dict(r) for r in leads["direct_callees"][:3]],
        "top_transitive_callers": [],
        "top_review_hypotheses": [],
        "review_lead_status": lead_status,
    }

    packet: dict = {
        "tool": "review_context",
        "status": "ok",
        "query": {"changed_files": list(files)},
        "summary": {
            "symbol_anchor_count": len(changed_symbols),
            "file_anchor_count": file_count,
        },
        "snapshot_summary": {},
        "snapshot_scope": {},
        "review_leads": leads,
        "review_lead_status": lead_status,
        "review_hypotheses": [hypothesis],
        "review_answer_packet": review_answer_packet,
        "application_impact": {"direct_callers": bulk_rows},
        "output_budget": {
            "truncated": False,
            "measured_chars": 0,
            "max_chars": 40000,
            "truncated_sections": [],
        },
    }
    return packet


# ---------------------------------------------------------------------------
# Spec 1: Multi-file over-budget — every cluster retains >= 1 changed-symbol anchor
# ---------------------------------------------------------------------------

class TestClusterCoverageMultiFile(unittest.TestCase):
    """P1: every changed-file cluster keeps >= 1 changed-symbol anchor under budget pressure."""

    def test_every_cluster_retains_at_least_one_changed_symbol(self):
        packet = _make_over_budget_packet(file_count=3, symbols_per_file=8)
        # Verify it starts over budget (broad context included in raw packet)
        self.assertGreater(len(canonical_json(packet)), 40_000, "fixture must start over budget")

        # include_broad_context=True: tests the 40K broad-path budgeter (the fixture uses
        # application_impact bulk to create pressure, which is stripped in compact profile).
        result = enforce_review_context_budget(packet, include_broad_context=True)

        review_leads = result.get("review_leads")
        self.assertIsInstance(review_leads, dict, "review_leads must be present")
        changed_symbols = review_leads.get("changed_symbols") or []
        self.assertIsInstance(changed_symbols, list)

        # Non-vacuous: the packet must retain changed_symbols rows at the default cap.
        self.assertTrue(changed_symbols, "no changed_symbols retained; cluster assertions did not execute")

        # Collect which file paths appear in the returned changed_symbols
        retained_paths = {row.get("path") for row in changed_symbols if isinstance(row, dict)}

        # Every cluster (file) must have at least one representative
        original_paths = {f"src/module_{i}.py" for i in range(3)}
        for path in original_paths:
            self.assertIn(
                path, retained_paths,
                f"cluster {path} has no changed-symbol anchor; retained: {retained_paths}",
            )

    def test_returned_counts_match_review_leads_rows(self):
        packet = _make_over_budget_packet(file_count=3, symbols_per_file=8)
        # include_broad_context=True: tests the 40K broad-path counts.
        result = enforce_review_context_budget(packet, include_broad_context=True)

        lead_status = result.get("review_lead_status")
        self.assertIsInstance(lead_status, dict)
        returned = lead_status.get("returned") or {}
        review_leads = result.get("review_leads") or {}

        for field, count_key in (
            ("changed_symbols", "changed_symbol_count"),
            ("direct_callers", "direct_caller_count"),
            ("direct_callees", "direct_callee_count"),
        ):
            actual = len(review_leads.get(field) or [])
            stated = returned.get(count_key, -1)
            self.assertEqual(actual, stated, f"{field}: returned count {stated} != actual rows {actual}")

    def test_cap_never_exceeded(self):
        packet = _make_over_budget_packet(file_count=3, symbols_per_file=8)
        # include_broad_context=True: broad-path 40K cap assertion.
        result = enforce_review_context_budget(packet, include_broad_context=True)
        size = len(canonical_json(result))
        self.assertLessEqual(size, 40_000, f"packet size {size} exceeds 40000 cap")


class TestClusterCoverageCompactProfile(unittest.TestCase):
    """B4: P1 cluster coverage and cap must hold on the default compact (15K) profile.

    Pressure comes from lead mass (many changed symbols/callers), not broad-context
    bulk — the compact profile strips broad sections, so a broad-bulk fixture would
    test nothing here.
    """

    def _make_compact_pressure_packet(self, file_count: int = 5) -> dict:
        packet = _make_over_budget_packet(file_count=file_count, symbols_per_file=60)
        from source.kg.product.output_budget import _strip_broad_context
        stripped_size = len(canonical_json(_strip_broad_context(packet)))
        self.assertGreater(
            stripped_size,
            15_000,
            "fixture must exceed the compact cap without broad-context mass",
        )
        return packet

    def test_cap_held_at_15k_compact_default(self):
        packet = self._make_compact_pressure_packet()
        result = enforce_review_context_budget(packet)
        size = len(canonical_json(result))
        self.assertLessEqual(size, 15_000, f"packet size {size} exceeds the 15000 compact cap")

    def test_every_cluster_retains_anchor_at_15k_compact_default(self):
        file_count = 5
        packet = self._make_compact_pressure_packet(file_count=file_count)
        result = enforce_review_context_budget(packet)
        self.assertLessEqual(len(canonical_json(result)), 15_000)
        review_leads = result.get("review_leads") or {}
        changed_symbols = [r for r in (review_leads.get("changed_symbols") or []) if isinstance(r, dict)]
        self.assertTrue(changed_symbols, "no changed_symbols retained; cluster assertions did not execute")
        retained_paths = {r.get("path") for r in changed_symbols}
        for i in range(file_count):
            path = f"src/module_{i}.py"
            self.assertIn(
                path,
                retained_paths,
                f"cluster {path} has no changed-symbol anchor at the 15K compact cap; retained: {retained_paths}",
            )

    def test_broad_sections_absent_on_compact_default(self):
        packet = self._make_compact_pressure_packet()
        result = enforce_review_context_budget(packet)
        self.assertNotIn("application_impact", result, "broad context must be stripped on compact profile")


# ---------------------------------------------------------------------------
# Spec 2: Extreme pressure — lowest-ranked cluster dropped whole
# ---------------------------------------------------------------------------

class TestExtremePressureClusterDrop(unittest.TestCase):
    """P1: under extreme budget pressure, drop whole lowest-ranked cluster.

    Uses a 5-file x 10-symbol fixture and tight caps so the lead rows themselves come
    under pressure (the exp111 shape) — the assertions below are proven non-vacuous by
    asserting that clusters really were dropped.
    """

    _ORIGINAL_PATHS = {f"src/module_{i}.py" for i in range(5)}

    def _make_extreme_packet(self) -> dict:
        """5 files, 10 symbols each, hypothesis references only files 0+1."""
        return _make_over_budget_packet(
            file_count=5,
            symbols_per_file=10,
            hyp_cluster_paths=["src/module_0.py", "src/module_1.py"],
        )

    def _retained_paths(self, result: dict) -> set:
        review_leads = result.get("review_leads") or {}
        changed_symbols = review_leads.get("changed_symbols") or []
        return {row.get("path") for row in changed_symbols if isinstance(row, dict)}

    def test_cap_held_under_extreme_pressure(self):
        packet = self._make_extreme_packet()
        result = enforce_review_context_budget(packet, max_chars=8_000)
        size = len(canonical_json(result))
        self.assertLessEqual(size, 8_000, f"cap exceeded: {size}")

    def test_truncation_summary_present_when_truncated(self):
        packet = self._make_extreme_packet()
        result = enforce_review_context_budget(packet, max_chars=8_000)
        ob = result.get("output_budget")
        self.assertIsInstance(ob, dict, "output_budget missing")
        self.assertTrue(ob.get("truncated"), "expected truncated=True")
        # P2: truncation_summary must be present when truncated
        self.assertIn("truncation_summary", ob, "truncation_summary missing from output_budget")

    def test_omitted_cluster_appears_in_truncation_summary(self):
        """Spec 2 shape: whole-dropped lowest-ranked clusters recorded; kept clusters intact.

        At a 9k cap the top-3 ranked clusters keep one anchor each and the two
        lowest-ranked clusters drop whole — the exact contract from the brief.
        """
        packet = self._make_extreme_packet()
        # include_broad_context=True: fixture uses application_impact bulk to create cluster-drop
        # pressure at 9K; stripping broad context (compact profile) removes that pressure.
        result = enforce_review_context_budget(packet, max_chars=9_000, include_broad_context=True)
        self.assertLessEqual(len(canonical_json(result)), 9_000)

        retained_paths = self._retained_paths(result)
        omitted_paths = self._ORIGINAL_PATHS - retained_paths
        # Non-vacuous: this cap must actually drop whole clusters while keeping others.
        self.assertTrue(omitted_paths, "expected whole clusters dropped at 9k cap; none were")
        self.assertTrue(retained_paths, "expected kept clusters at 9k cap; none were")

        ts = (result.get("output_budget") or {}).get("truncation_summary") or {}
        clusters = ts.get("omitted_high_risk_clusters") or []
        self.assertIsInstance(clusters, list)
        self.assertGreater(len(clusters), 0, "omitted clusters expected but omitted_high_risk_clusters is empty")
        all_cluster_files = []
        for cr in clusters:
            self.assertIsInstance(cr, dict)
            changed_files = cr.get("changed_files") or []
            all_cluster_files.extend(changed_files)
        # Every whole-dropped cluster must be recorded (<= 5 dropped here), and the
        # summary must never claim a kept cluster was omitted.
        for op in sorted(omitted_paths):
            self.assertIn(op, all_cluster_files, f"omitted path {op} not in any cluster row")
        for cf in all_cluster_files:
            self.assertIn(cf, omitted_paths, f"summary claims kept cluster {cf} was omitted")

    def test_summary_records_top_ranked_dropped_clusters_at_extreme_cap(self):
        """At an extreme cap not all dropped clusters fit; the recorded ones must be the
        highest-ranked dropped clusters, in rank order, and all must really be omitted."""
        packet = self._make_extreme_packet()
        # include_broad_context=True: fixture uses application_impact bulk for 8K
        # cluster-drop pressure; the compact composer keeps all anchors (no drops).
        result = enforce_review_context_budget(packet, max_chars=8_000, include_broad_context=True)
        self.assertLessEqual(len(canonical_json(result)), 8_000)
        retained_paths = self._retained_paths(result)
        omitted_paths = self._ORIGINAL_PATHS - retained_paths
        self.assertTrue(omitted_paths, "expected whole clusters dropped at 8k cap; none were")
        ts = (result.get("output_budget") or {}).get("truncation_summary") or {}
        clusters = ts.get("omitted_high_risk_clusters") or []
        self.assertGreater(len(clusters), 0, "at least the top-ranked dropped cluster must be recorded")
        recorded = [cf for cr in clusters for cf in (cr.get("changed_files") or [])]
        for cf in recorded:
            self.assertIn(cf, omitted_paths, f"summary claims kept cluster {cf} was omitted")
        # Rank order: a hypothesis-referenced dropped cluster must be recorded first.
        dropped_referenced = sorted(omitted_paths & {"src/module_0.py", "src/module_1.py"})
        if dropped_referenced:
            self.assertEqual(
                recorded[0], dropped_referenced[0],
                "highest-ranked (hypothesis-referenced) dropped cluster must be recorded first",
            )

    def test_kept_clusters_are_hypothesis_referenced(self):
        """Retained clusters must come from the hypothesis-referenced set when fewer
        clusters survive than were referenced (rank: referenced first)."""
        packet = self._make_extreme_packet()
        # include_broad_context=True: broad-bulk fixture; see note on the sibling 8K test.
        result = enforce_review_context_budget(packet, max_chars=8_000, include_broad_context=True)
        retained_paths = self._retained_paths(result)
        # Non-vacuous: something must be retained and something dropped.
        self.assertTrue(retained_paths, "no changed_symbols retained at all")
        self.assertLess(len(retained_paths), 5, "expected cluster drops at 8k cap")
        referenced = {"src/module_0.py", "src/module_1.py"}
        if len(retained_paths) <= len(referenced):
            self.assertTrue(
                retained_paths <= referenced,
                f"retained {retained_paths} includes non-referenced cluster while referenced clusters were dropped",
            )

    def test_one_anchor_per_cluster_before_second_row(self):
        """Core P1 rule: no cluster keeps a 2nd row while another original cluster has 0.

        Uses a 9k cap where the reviewer probe confirmed uncovered={module_3, module_4},
        so the ``if uncovered`` branch fires and the assertion is non-vacuous.
        """
        packet = self._make_extreme_packet()
        # include_broad_context=True: broad bulk is required to create 9K cluster-drop pressure.
        result = enforce_review_context_budget(packet, max_chars=9_000, include_broad_context=True)
        self.assertLessEqual(len(canonical_json(result)), 9_000)
        review_leads = result.get("review_leads") or {}
        changed_symbols = [r for r in (review_leads.get("changed_symbols") or []) if isinstance(r, dict)]
        self.assertTrue(changed_symbols, "no changed_symbols retained at 9k cap")
        counts: dict = {}
        for row in changed_symbols:
            p = row.get("path")
            counts[p] = counts.get(p, 0) + 1
        uncovered = self._ORIGINAL_PATHS - set(counts)
        doubled = {p for p, c in counts.items() if c >= 2}
        # Non-vacuous: at 9k the fixture leaves uncovered clusters (module_3, module_4).
        self.assertTrue(uncovered, f"expected uncovered clusters at 9k cap; counts={counts}")
        self.assertFalse(
            doubled,
            f"clusters {doubled} keep >= 2 rows while clusters {uncovered} have no anchor",
        )

    def test_truthful_omitted_counts(self):
        """P2 omitted counts must equal pre-budget totals minus retained rows."""
        packet = self._make_extreme_packet()
        result = enforce_review_context_budget(packet, max_chars=8_000)
        review_leads = result.get("review_leads") or {}
        retained = len(review_leads.get("changed_symbols") or [])
        actual_omitted = 50 - retained
        # Non-vacuous: rows really were omitted at this cap.
        self.assertGreater(actual_omitted, 0, "expected omitted changed symbols at 8k cap")
        ts = (result.get("output_budget") or {}).get("truncation_summary") or {}
        self.assertEqual(
            ts.get("omitted_changed_symbol_count"),
            actual_omitted,
            f"summary reports {ts.get('omitted_changed_symbol_count')} omitted; actual {actual_omitted}",
        )


# ---------------------------------------------------------------------------
# Spec 3: truncation_summary shape + bounds; absent when nothing truncated
# ---------------------------------------------------------------------------

class TestTruncationSummaryShape(unittest.TestCase):
    """P2: truncation_summary shape, bounds, and absence when not truncated."""

    def test_no_truncation_summary_when_not_truncated(self):
        """Small packet that fits under cap must not have truncation_summary."""
        tiny_packet: dict = {
            "tool": "review_context",
            "status": "ok",
            "query": {},
            "summary": {},
            "snapshot_summary": {},
            "snapshot_scope": {},
            "review_leads": {
                "changed_files": ["src/a.py"],
                "changed_symbols": [
                    {"lead_id": "lead:changed_symbol:x1", "lead_kind": "changed_symbol",
                     "path": "src/a.py", "qualname": "foo", "repo": "r"}
                ],
                "direct_callers": [],
                "direct_callees": [],
                "transitive_callers": [],
                "source_coordinates": [],
            },
            "review_lead_status": {
                "coverage_status": "useful",
                "available": {"changed_symbol_count": 1, "direct_caller_count": 0,
                              "direct_callee_count": 0, "transitive_caller_count": 0,
                              "source_coordinate_count": 0},
                "returned": {"changed_symbol_count": 1, "direct_caller_count": 0,
                             "direct_callee_count": 0, "transitive_caller_count": 0,
                             "source_coordinate_count": 0},
            },
            "review_hypotheses": [],
            "review_answer_packet": {
                "top_changed_symbols": [],
                "top_direct_callers": [],
                "top_direct_callees": [],
                "top_transitive_callers": [],
                "top_review_hypotheses": [],
            },
        }
        result = enforce_review_context_budget(tiny_packet)
        ob = result.get("output_budget") or {}
        # If not truncated, must have no truncation_summary
        truncated = ob.get("truncated", False)
        if not truncated:
            self.assertNotIn("truncation_summary", ob, "truncation_summary must be absent when not truncated")

    def test_no_truncation_summary_broad_bulk_only(self):
        """Broad-bulk-only truncation (no lead rows omitted) must not produce a summary.

        The 3-file x 8-symbol fixture at the 40k broad cap keeps all lead rows
        (only application_impact bulk is evicted), so the summary would be all-zero
        and is suppressed. include_broad_context=True: fixture relies on broad bulk;
        compact profile strips it entirely so no budget pressure arises there.
        """
        packet = _make_over_budget_packet(file_count=3, symbols_per_file=8)
        result = enforce_review_context_budget(packet, include_broad_context=True)
        ob = result.get("output_budget") or {}
        # Packet must be truncated (bulk evicted) but have no omitted lead rows.
        self.assertTrue(ob.get("truncated"), "fixture should be truncated at default cap")
        rl = result.get("review_leads") or {}
        self.assertEqual(
            len(rl.get("changed_symbols") or []), 24,
            "all 24 changed_symbols should be retained (3 files * 8 symbols)",
        )
        self.assertNotIn(
            "truncation_summary", ob,
            "broad-bulk-only truncation must not produce truncation_summary",
        )

    def test_truncation_summary_fields_present(self):
        # Use the extreme-pressure packet at 8k to guarantee lead rows are omitted
        # (broad-bulk-only truncation at the default cap no longer produces a summary).
        packet = _make_over_budget_packet(file_count=5, symbols_per_file=10,
                                          hyp_cluster_paths=["src/module_0.py", "src/module_1.py"])
        result = enforce_review_context_budget(packet, max_chars=8_000)
        ob = result.get("output_budget") or {}
        self.assertTrue(ob.get("truncated"), "extreme fixture must truncate at 8k")
        ts = ob.get("truncation_summary")
        self.assertIsInstance(ts, dict, "truncation_summary must be a dict when lead rows are omitted")
        # Required count fields
        for field in (
            "omitted_changed_symbol_count",
            "omitted_direct_caller_count",
            "omitted_direct_callee_count",
            "omitted_transitive_caller_count",
        ):
            self.assertIn(field, ts, f"missing field {field}")
            self.assertIsInstance(ts[field], int, f"{field} must be int")
            self.assertGreaterEqual(ts[field], 0, f"{field} must be non-negative")
        # At least one lead field must be nonzero (confirms gate is passed).
        self.assertTrue(
            any(ts.get(f, 0) > 0 for f in (
                "omitted_changed_symbol_count",
                "omitted_direct_caller_count",
                "omitted_direct_callee_count",
                "omitted_transitive_caller_count",
            )),
            "at least one omitted-lead count must be nonzero for summary to be present",
        )
        # omitted_high_risk_clusters bounded to <= 5
        clusters = ts.get("omitted_high_risk_clusters") or []
        self.assertIsInstance(clusters, list)
        self.assertLessEqual(len(clusters), 5, "omitted_high_risk_clusters must have <= 5 entries")

    def test_truncation_summary_cluster_row_shape(self):
        # Use the extreme-pressure packet at 8k (same reason as test above).
        packet = _make_over_budget_packet(file_count=5, symbols_per_file=10,
                                          hyp_cluster_paths=["src/module_0.py", "src/module_1.py"])
        result = enforce_review_context_budget(packet, max_chars=8_000)
        ob = result.get("output_budget") or {}
        if not ob.get("truncated"):
            self.skipTest("packet did not truncate")
        ts = ob.get("truncation_summary") or {}
        clusters = ts.get("omitted_high_risk_clusters") or []
        for cr in clusters:
            self.assertIsInstance(cr, dict)
            # Must have changed_files
            self.assertIn("changed_files", cr)
            cfs = cr["changed_files"]
            self.assertIsInstance(cfs, list)
            # representative_symbols bounded to <= 3
            rep_syms = cr.get("representative_symbols") or []
            self.assertLessEqual(len(rep_syms), 3)
            # representative_lead_ids bounded to <= 3
            rep_ids = cr.get("representative_lead_ids") or []
            self.assertLessEqual(len(rep_ids), 3)

    def test_truncation_summary_itself_does_not_push_over_cap(self):
        packet = _make_over_budget_packet(file_count=3, symbols_per_file=8)
        result = enforce_review_context_budget(packet)
        size = len(canonical_json(result))
        self.assertLessEqual(size, 40_000, f"truncation_summary pushed packet over cap: {size}")


# ---------------------------------------------------------------------------
# Spec 4: Hypothesis anchor bound (up to 4 evidence_refs)
# ---------------------------------------------------------------------------

class TestHypothesisAnchorBound(unittest.TestCase):
    """P1: compacted hypothesis carries up to 4 evidence_refs (was 3)."""

    def test_compacted_hypothesis_carries_up_to_4_evidence_refs(self):
        from source.kg.product.output_budget import _compact_review_hypothesis
        hyp = {
            "hypothesis_id": "hypothesis:some_risk:abc123",
            "risk_type": "some_risk",
            "confidence": "high",
            "why": "test",
            "evidence_refs": [
                {"repo": "r", "path": "a.py", "line_start": 1, "line_end": 5},
                {"repo": "r", "path": "b.py", "line_start": 1, "line_end": 5},
                {"repo": "r", "path": "c.py", "line_start": 1, "line_end": 5},
                {"repo": "r", "path": "d.py", "line_start": 1, "line_end": 5},
                {"repo": "r", "path": "e.py", "line_start": 1, "line_end": 5},
            ],
            "source_checks": [],
            "supporting_lead_ids": ["lead:changed_symbol:x1"],
        }
        compact = _compact_review_hypothesis(hyp)
        self.assertIn("evidence_refs", compact)
        self.assertEqual(len(compact["evidence_refs"]), 4, "compacted hypothesis must keep up to 4 evidence_refs")

    def test_compacted_hypothesis_with_fewer_than_4_refs_keeps_all(self):
        from source.kg.product.output_budget import _compact_review_hypothesis
        hyp = {
            "hypothesis_id": "hypothesis:risk:xyz",
            "risk_type": "risk",
            "confidence": "low",
            "why": "test",
            "evidence_refs": [
                {"repo": "r", "path": "a.py"},
                {"repo": "r", "path": "b.py"},
            ],
            "source_checks": [],
            "supporting_lead_ids": [],
        }
        compact = _compact_review_hypothesis(hyp)
        self.assertEqual(len(compact.get("evidence_refs", [])), 2)


# ---------------------------------------------------------------------------
# Spec 5: End-to-end via enforce_review_context_budget
# ---------------------------------------------------------------------------

class TestEndToEndClusterCoverage(unittest.TestCase):
    """Spec 5: end-to-end enforce call with cluster coverage assertion."""

    def test_end_to_end_cluster_coverage(self):
        packet = _make_over_budget_packet(file_count=3, symbols_per_file=8)
        original_size = len(canonical_json(packet))
        self.assertGreater(original_size, 40_000, "fixture must start over budget")

        result = enforce_review_context_budget(packet)

        # Basic shape
        self.assertIn("review_leads", result)
        self.assertIn("review_lead_status", result)
        self.assertIn("output_budget", result)

        # Cap
        self.assertLessEqual(len(canonical_json(result)), 40_000)

        # Cluster coverage: every file path must appear in changed_symbols.
        review_leads = result["review_leads"]
        changed_symbols = review_leads.get("changed_symbols") or []
        # Non-vacuous: rows must survive at the default cap for this fixture.
        self.assertTrue(changed_symbols, "no changed_symbols retained; coverage assertions did not execute")
        retained_paths = {r.get("path") for r in changed_symbols if isinstance(r, dict)}
        for i in range(3):
            self.assertIn(
                f"src/module_{i}.py",
                retained_paths,
                f"cluster module_{i}.py missing from retained changed_symbols",
            )

    def test_hypothesis_floor_preserved(self):
        packet = _make_over_budget_packet(file_count=3, symbols_per_file=8)
        result = enforce_review_context_budget(packet)
        hyps = result.get("review_hypotheses") or []
        self.assertGreaterEqual(len(hyps), 1, "hypothesis floor: at least 1 hypothesis must survive")

    def test_mirror_subset_invariant(self):
        """top_changed_symbols in answer_packet must be subset of review_leads.changed_symbols."""
        packet = _make_over_budget_packet(file_count=3, symbols_per_file=8)
        result = enforce_review_context_budget(packet)

        review_leads = result.get("review_leads") or {}
        answer_packet = result.get("review_answer_packet") or {}

        lead_ids_in_leads = {
            r.get("lead_id") for r in (review_leads.get("changed_symbols") or [])
            if isinstance(r, dict) and r.get("lead_id")
        }
        for row in (answer_packet.get("top_changed_symbols") or []):
            if isinstance(row, dict) and row.get("lead_id"):
                self.assertIn(
                    row["lead_id"], lead_ids_in_leads,
                    "top_changed_symbols mirror contains lead_id not in review_leads.changed_symbols",
                )


# ---------------------------------------------------------------------------
# Spec 6: Top-level changed_symbols mirrors review_leads after post-repair eviction
# ---------------------------------------------------------------------------

def _make_over_budget_packet_with_top_cs(
    file_count: int = 5,
    symbols_per_file: int = 10,
    hyp_cluster_paths: list[str] | None = None,
) -> dict:
    """Variant of _make_over_budget_packet that includes a top-level changed_symbols key.

    The equality contract (review_leads.changed_symbols == changed_symbols) only applies
    when a top-level changed_symbols key is present (the MCP review_context tool emits
    both; the test fixture in test_mcp_tools.py does too). This helper matches that shape.
    """
    packet = _make_over_budget_packet(
        file_count=file_count,
        symbols_per_file=symbols_per_file,
        hyp_cluster_paths=hyp_cluster_paths,
    )
    # Mirror the review_leads.changed_symbols into the top-level key so the
    # de-alias desync path can be exercised.
    rl = packet.get("review_leads") or {}
    packet["changed_symbols"] = list(rl.get("changed_symbols") or [])
    return packet


class TestTopLevelChangedSymbolsMirror(unittest.TestCase):
    """Regression for the desync path: post-repair eviction must not break equality.

    Before the fix, _repair_cluster_coverage de-aliased result["changed_symbols"]
    from review_leads["changed_symbols"], and the subsequent _evict_review_rows_to_fit
    could pop from the top-level list (classified non-lead) while leaving
    review_leads["changed_symbols"] untouched, violating the equality contract.
    """

    def test_top_level_mirrors_review_leads_after_post_repair_eviction(self):
        """Force the desync path by using a cap where repair runs and eviction follows."""
        # 5-file x 10-symbol fixture; hypothesis references only files 0+1.
        # At 9k the repair pass runs (hard-cap re-ranks rows cluster-blind) and
        # subsequent eviction can touch the top-level list before the re-mirror.
        packet = _make_over_budget_packet_with_top_cs(
            file_count=5,
            symbols_per_file=10,
            hyp_cluster_paths=["src/module_0.py", "src/module_1.py"],
        )
        # include_broad_context=True: the desync path under test (repair de-alias +
        # post-repair eviction + re-mirror) exists only on the broad ladder; the
        # compact composer has no top-level changed_symbols mirror by design.
        result = enforce_review_context_budget(packet, max_chars=9_000, include_broad_context=True)
        self.assertLessEqual(len(canonical_json(result)), 9_000)

        review_leads = result.get("review_leads") or {}
        rl_cs = review_leads.get("changed_symbols") or []
        # Only assert when the top-level key is present (it must be — we seeded it above).
        self.assertIn("changed_symbols", result, "top-level changed_symbols key must be present")
        top_cs = result["changed_symbols"]

        # Non-vacuous: the packet must actually be truncated so post-repair eviction runs.
        ob = result.get("output_budget") or {}
        self.assertTrue(ob.get("truncated"), "fixture must be truncated at 9k cap")

        # Equality contract: the two lists must be identical after finalize.
        self.assertEqual(
            rl_cs,
            top_cs,
            f"review_leads.changed_symbols ({len(rl_cs)} rows) != "
            f"top-level changed_symbols ({len(top_cs)} rows) after post-repair eviction",
        )

    def test_top_level_mirrors_review_leads_at_extreme_pressure(self):
        """Same equality contract under extreme budget pressure (8k)."""
        packet = _make_over_budget_packet_with_top_cs(
            file_count=5,
            symbols_per_file=10,
            hyp_cluster_paths=["src/module_0.py", "src/module_1.py"],
        )
        # include_broad_context=True: broad-ladder desync path; see sibling test note.
        result = enforce_review_context_budget(packet, max_chars=8_000, include_broad_context=True)
        self.assertLessEqual(len(canonical_json(result)), 8_000)

        review_leads = result.get("review_leads") or {}
        rl_cs = review_leads.get("changed_symbols") or []
        self.assertIn("changed_symbols", result, "top-level changed_symbols key must be present")
        top_cs = result["changed_symbols"]

        ob = result.get("output_budget") or {}
        self.assertTrue(ob.get("truncated"), "fixture must be truncated at 8k cap")

        self.assertEqual(
            rl_cs,
            top_cs,
            f"review_leads.changed_symbols ({len(rl_cs)} rows) != "
            f"top-level changed_symbols ({len(top_cs)} rows) at extreme pressure",
        )


# ---------------------------------------------------------------------------
# Regression: tier-2 funded anchor repair (R1b)
# ---------------------------------------------------------------------------

class ClusterAnchorTier2FundingTest(unittest.TestCase):
    """Regression: anchor repair must consume tier-2 victims when no swap donors exist.

    Geometry: 8 clusters (one symbol each = no multi-row donors) plus bulky
    claim_contract/scope_contract/changed_surface/surface_status sections that push
    the packet over budget. After enforcement every cluster must have >= 1 anchor,
    proving the tier-2 funding path in _repair_cluster_coverage.
    """

    def _make_packet(self) -> dict:
        files = [f"src/router_{i}.ts" for i in range(8)]
        changed_symbols = [
            _make_changed_symbol(f"lead:cs:{i}", files[i], f"handler_{i}")
            for i in range(8)
        ]
        bulky_item = {"key": "x" * 300, "value": "y" * 300, "meta": {"a": 1, "b": 2}}
        changed_surface = {
            "symbols": [dict(bulky_item, path=f) for f in files],
            "files": [{"path": f, "size": 9999} for f in files],
        }
        surface_status = [dict(bulky_item, path=f) for f in files for _ in range(4)]
        changed_file_symbols = [dict(bulky_item, path=f) for f in files for _ in range(4)]
        claim_contract = {"fields": ["x" * 50] * 20}
        scope_contract = {"repos": ["r" * 50] * 20}
        answer_packet = {
            "status": "ok",
            "claim_contract": claim_contract,
            "scope_contract": scope_contract,
            "top_changed_symbols": [
                _make_changed_symbol(f"lead:cs:{i}", files[i], f"handler_{i}")
                for i in range(8)
            ],
        }
        review_leads = {
            "changed_symbols": list(changed_symbols),
            "changed_files": list(files),
        }
        review_lead_status = {
            "changed_symbol_count": 8,
            "changed_anchor_count": 8,
            "direct_impact_count": 0,
            "transitive_impact_count": 0,
            "source_coordinate_count": 0,
            "file_anchor_count": 0,
            "coverage_status": "ok",
            "available": {
                "changed_symbol_count": 8,
                "direct_caller_count": 0,
                "direct_callee_count": 0,
                "transitive_caller_count": 0,
                "source_coordinate_count": 0,
            },
        }
        packet = {
            "tool": "review_context",
            "status": "ok",
            "repo": "repo-a",
            "summary": {"changed_symbol_count": 8},
            "review_leads": review_leads,
            "review_lead_status": review_lead_status,
            "review_answer_packet": answer_packet,
            "changed_symbols": list(changed_symbols),
            "changed_surface": changed_surface,
            "surface_status": surface_status,
            "changed_file_symbols": changed_file_symbols,
            "claim_contract": claim_contract,
            "scope_contract": scope_contract,
            "output_budget": {"engine_version": "test"},
        }
        return add_review_lead_ids(packet)

    def test_tier2_funding_restores_all_cluster_anchors(self) -> None:
        from source.kg.product.output_budget import REVIEW_CONTEXT_MAX_CHARS
        packet = self._make_packet()
        self.assertGreater(
            len(canonical_json(packet)), REVIEW_CONTEXT_MAX_CHARS,
            "fixture must be over budget",
        )
        result = enforce_review_context_budget(packet)
        self.assertLessEqual(
            len(canonical_json(result)), REVIEW_CONTEXT_MAX_CHARS,
            "result must fit within budget",
        )
        rl = result.get("review_leads", {})
        retained = rl.get("changed_symbols", [])
        paths_with_anchor = {r["path"] for r in retained if isinstance(r, dict) and r.get("path")}
        expected_paths = {f"src/router_{i}.ts" for i in range(8)}
        self.assertEqual(
            paths_with_anchor,
            expected_paths,
            f"missing anchors for: {expected_paths - paths_with_anchor}",
        )
        rls = result.get("review_lead_status", {})
        returned = rls.get("returned", {})
        self.assertGreaterEqual(returned.get("changed_symbol_count", 0), 1)


# ---------------------------------------------------------------------------
# Spec 7: Snapshot-derived edge counts used for re-interleave ranking
# ---------------------------------------------------------------------------

class TestSnapshotEdgeCountsForRanking(unittest.TestCase):
    """Rank-input consistency: re-interleave in repair and finalize must use snapshot counts.

    When edge rows are evicted during budget passes, the compacted result has fewer
    edge rows than the original snapshot. Re-interleaving from compacted rows would
    use a different edge count than repair/P2, causing non-deterministic ordering
    depending on which rows survived eviction. The fix threads snapshot edge counts
    into both re-interleave calls.
    """

    def test_cluster_order_uses_snapshot_not_compacted_edges(self):
        """After extreme eviction the returned changed_symbols are ranked by snapshot counts.

        With 5 files x 10 symbols and hypothesis on files 0+1, files 2/3/4 are
        ranked by snapshot edge counts (4 callers + 4 callees = 8 per file, equal
        among non-referenced clusters, so path is the final tie-breaker). The first
        retained non-referenced cluster after repair must be module_2.py (lowest path).
        """
        packet = _make_over_budget_packet(
            file_count=5,
            symbols_per_file=10,
            hyp_cluster_paths=["src/module_0.py", "src/module_1.py"],
        )
        result = enforce_review_context_budget(packet, max_chars=9_000)
        self.assertLessEqual(len(canonical_json(result)), 9_000)

        review_leads = result.get("review_leads") or {}
        cs = [r for r in (review_leads.get("changed_symbols") or []) if isinstance(r, dict)]
        retained_paths = {r.get("path") for r in cs}

        # At 9k the top-3 clusters survive; referenced (0+1) plus the lowest-path
        # non-referenced one (module_2.py when edge counts are equal).
        if len(retained_paths) == 3:
            self.assertIn(
                "src/module_2.py", retained_paths,
                "module_2.py (lowest-path non-referenced cluster) must be the 3rd retained cluster",
            )


class TestContractDedupPresenceGuard(unittest.TestCase):
    """Fix 2: contract-dedup must only delete review_answer_packet.claim_contract /
    scope_contract when the top-level copy exists and is a dict.  When the top-level
    copy is absent, the answer-packet entry is the only copy and must be retained.
    """

    def _make_over_budget_packet_no_top_level_contract(self) -> dict:
        """Packet where review_answer_packet has claim_contract but top-level does NOT."""
        files = [f"src/mod_{i}.py" for i in range(4)]
        changed_symbols = [
            _make_changed_symbol(f"lead:cs:{i}", files[i], f"fn_{i}")
            for i in range(4)
        ]
        # Bulky broad-context to push over budget
        bulky = [{"k": "x" * 200, "v": "y" * 200} for _ in range(50)]
        answer_packet = {
            "status": "ok",
            # claim_contract present only in the answer-packet, NOT at top-level
            "claim_contract": {"fields": ["field_" + "x" * 20] * 10},
            "scope_contract": {"repos": ["repo_" + "r" * 20] * 10},
            "top_changed_symbols": list(changed_symbols),
        }
        review_leads = {
            "changed_symbols": list(changed_symbols),
            "changed_files": list(files),
        }
        review_lead_status = {
            "changed_symbol_count": 4,
            "changed_anchor_count": 4,
            "direct_impact_count": 0,
            "transitive_impact_count": 0,
            "source_coordinate_count": 0,
            "file_anchor_count": 0,
            "coverage_status": "ok",
            "available": {
                "changed_symbol_count": 4,
                "direct_caller_count": 0,
                "direct_callee_count": 0,
                "transitive_caller_count": 0,
                "source_coordinate_count": 0,
            },
        }
        packet = {
            "tool": "review_context",
            "status": "ok",
            "repo": "repo-a",
            "summary": {"changed_symbol_count": 4},
            "review_leads": review_leads,
            "review_lead_status": review_lead_status,
            "review_answer_packet": answer_packet,
            "changed_symbols": list(changed_symbols),
            # No top-level claim_contract / scope_contract
            "application_impact": {"direct_callers": bulky},
            "output_budget": {"engine_version": "test"},
        }
        return add_review_lead_ids(packet)

    def test_answer_packet_contract_retained_when_top_level_absent(self) -> None:
        """When top-level claim_contract is absent, answer-packet copy must not be deleted."""
        from source.kg.product.output_budget import REVIEW_CONTEXT_MAX_CHARS, enforce_review_context_budget
        packet = self._make_over_budget_packet_no_top_level_contract()
        # Confirm top-level contract is absent in fixture
        self.assertNotIn("claim_contract", packet, "fixture must not have top-level claim_contract")
        result = enforce_review_context_budget(packet, max_chars=REVIEW_CONTEXT_MAX_CHARS)
        answer_packet = result.get("review_answer_packet") or {}
        # answer-packet entry must be retained since there's no top-level duplicate
        self.assertIn(
            "claim_contract",
            answer_packet,
            "review_answer_packet.claim_contract must be retained when top-level copy is absent",
        )

    def test_answer_packet_contract_removed_when_top_level_present(self) -> None:
        """When top-level claim_contract is a dict, answer-packet duplicate may be evicted."""
        from source.kg.product.output_budget import enforce_review_context_budget
        packet = self._make_over_budget_packet_no_top_level_contract()
        # Add top-level copies so the dedup guard allows removal
        packet["claim_contract"] = {"fields": ["field_" + "x" * 20] * 10}
        packet["scope_contract"] = {"repos": ["repo_" + "r" * 20] * 10}
        # Push well over budget so tier-2 is reached
        max_chars = 3_000
        if len(canonical_json(packet)) <= max_chars:
            self.skipTest("fixture does not exceed small cap; adjust test geometry")
        result = enforce_review_context_budget(packet, max_chars=max_chars)
        # After eviction the answer-packet duplicate should have been removed
        answer_packet = result.get("review_answer_packet") or {}
        # At least one of the two contract keys should be gone (the loop removes one per iteration)
        both_present = (
            "claim_contract" in answer_packet and "scope_contract" in answer_packet
        )
        self.assertFalse(
            both_present,
            "at least one answer-packet contract key must be removed when top-level copy exists",
        )


if __name__ == "__main__":
    unittest.main()
