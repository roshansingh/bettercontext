from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from source.kg.build.pipeline import build_kg
from source.kg.product.mcp_tools import call_tool
from source.kg.query.snapshot import KgSnapshot


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _build_breadth_fixture(root: Path) -> tuple[Path, Path, Path, Path]:
    import shutil

    head_repo = root / "repo"
    (head_repo / "pkg").mkdir(parents=True)
    _write(head_repo / "pkg" / "__init__.py", "")
    _write(
        head_repo / "pkg" / "consumer_a.py",
        "from pkg.registry import make_widget\n\n"
        "def use_a():\n"
        "    return make_widget('a')\n",
    )
    _write(
        head_repo / "pkg" / "consumer_b.py",
        "from pkg.registry import make_widget\n\n"
        "def use_b():\n"
        "    return make_widget('b')\n",
    )
    _write(
        head_repo / "pkg" / "consumer_c.py",
        "from pkg.registry import make_widget\n\n"
        "def use_c():\n"
        "    return make_widget('c')\n",
    )
    _write(
        head_repo / "pkg" / "registry.py",
        "def check(config):\n"
        "    return config is not None\n\n"
        "def make_widget(config):\n"
        "    if not check(config):\n"
        "        raise ValueError('missing')\n"
        "    return {'config': config}\n",
    )
    base_checkout = root / "base_checkout"
    shutil.copytree(head_repo, base_checkout)
    out_base = root / "kg_base"
    build_kg(head_repo, out_base)
    _write(
        head_repo / "pkg" / "registry.py",
        "def check(config):\n"
        "    return config is not None\n\n"
        "def make_widget(config):\n"
        "    return {'config': config}\n",
    )
    out_head = root / "kg_head"
    build_kg(head_repo, out_head)
    return out_base, out_head, base_checkout, head_repo


def _no_semantic_splice(**kwargs):
    stats = kwargs.get("_stats_out")
    if isinstance(stats, dict):
        stats.update({"rows_generated": 0, "rows_verified": 0, "rows_unverified": 0})
    return kwargs["review_hypotheses"], "active"


class TestCallerBreadth(unittest.TestCase):
    def test_compute_consumer_breadth_counts_out_of_diff_callers(self) -> None:
        from source.kg.query.caller_breadth import compute_consumer_breadth

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _out_base, out_head, _base_repo, _head_repo = _build_breadth_fixture(root)
            kg = KgSnapshot(out_head)
            symbol = next(
                entity
                for entity in kg.entities
                if entity.get("kind") == "CodeSymbol"
                and (entity.get("identity") or {}).get("qualname") == "make_widget"
            )

            breadth = compute_consumer_breadth(
                kg,
                symbol_urn=str(symbol["urn"]),
                qualname="make_widget",
                path="pkg/registry.py",
                changed_files=["pkg/registry.py", "pkg/consumer_a.py"],
            ).to_json()

        self.assertEqual(breadth["enumeration_status"], "complete")
        self.assertEqual(breadth["consumer_unit"], "file")
        self.assertEqual(breadth["total_consumers"], 3)
        self.assertEqual(breadth["in_diff_count"], 1)
        self.assertEqual(breadth["out_of_diff_count"], 2)
        self.assertEqual(len(breadth["top_out_of_diff"]), 2)
        self.assertTrue(
            all(row.get("path") in {"pkg/consumer_b.py", "pkg/consumer_c.py"} for row in breadth["top_out_of_diff"]),
            f"unexpected out-of-diff coordinates: {breadth['top_out_of_diff']!r}",
        )

    def test_compute_consumer_breadth_no_facts_is_honest(self) -> None:
        from source.kg.query.caller_breadth import compute_consumer_breadth

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            _write(repo / "pkg" / "__init__.py", "")
            _write(repo / "pkg" / "registry.py", "def lonely():\n    return 1\n")
            out = root / "kg"
            build_kg(repo, out)
            kg = KgSnapshot(out)
            symbol = next(
                entity
                for entity in kg.entities
                if entity.get("kind") == "CodeSymbol"
                and (entity.get("identity") or {}).get("qualname") == "lonely"
            )

            breadth = compute_consumer_breadth(
                kg,
                symbol_urn=str(symbol["urn"]),
                qualname="lonely",
                path="pkg/registry.py",
                changed_files=["pkg/registry.py"],
            ).to_json()

        self.assertEqual(breadth["enumeration_status"], "no_facts")
        self.assertEqual(breadth["total_consumers"], 0)
        self.assertEqual(breadth["top_out_of_diff"], [])

    def test_compute_consumer_breadth_unresolved_is_not_no_facts(self) -> None:
        from source.kg.query.caller_breadth import compute_consumer_breadth

        class _FakeKg:
            entities = []

            def find_callers(self, *args, **kwargs):  # noqa: ANN002, ANN003
                return {"target": {"status": "ambiguous"}, "callers": []}

        breadth = compute_consumer_breadth(
            _FakeKg(),  # type: ignore[arg-type]
            qualname="target",
            changed_files=["pkg/changed.py"],
        ).to_json()

        self.assertEqual(breadth["enumeration_status"], "unresolved")
        self.assertEqual(breadth["total_consumers"], 0)

    def test_compute_consumer_breadth_dedupes_import_and_call_in_same_file(self) -> None:
        from source.kg.query.caller_breadth import compute_consumer_breadth

        class _FakeKg:
            entities = [
                {
                    "kind": "CodeSymbol",
                    "urn": "urn:target",
                    "identity": {"qualname": "target"},
                    "properties": {"path": "pkg/lib.py"},
                }
            ]

            def find_callers(self, *args, **kwargs):  # noqa: ANN002, ANN003
                return {
                    "target": {"status": "resolved"},
                    "callers": [
                        {
                            "subject": "use_target",
                            "evidence": [
                                {"bytes_ref": {"path": "pkg/consumer.py", "line_start": 8}},
                            ],
                        }
                    ],
                }

            def symbol_import_consumer_leads(self, *args, **kwargs):  # noqa: ANN002, ANN003
                return {
                    "leads": [
                        {
                            "importer": {"path": "pkg/consumer.py", "display_name": "pkg.consumer"},
                            "fact": {"evidence": [{"bytes_ref": {"path": "pkg/consumer.py", "line_start": 1}}]},
                        }
                    ]
                }

        breadth = compute_consumer_breadth(
            _FakeKg(),  # type: ignore[arg-type]
            symbol_urn="urn:target",
            changed_files=["pkg/lib.py"],
        ).to_json()

        self.assertEqual(breadth["consumer_unit"], "file")
        self.assertEqual(breadth["total_consumers"], 1)
        self.assertEqual(breadth["out_of_diff_count"], 1)
        self.assertEqual(breadth["top_out_of_diff"], [{"path": "pkg/consumer.py", "line": 8, "qualname": "use_target"}])

    def test_import_consumer_line_uses_matching_evidence_path_only(self) -> None:
        from source.kg.query.caller_breadth import compute_consumer_breadth

        class _FakeKg:
            entities = [
                {
                    "kind": "CodeSymbol",
                    "urn": "urn:target",
                    "identity": {"qualname": "target"},
                    "properties": {"path": "pkg/lib.py"},
                }
            ]

            def find_callers(self, *args, **kwargs):  # noqa: ANN002, ANN003
                return {"target": {"status": "resolved"}, "callers": []}

            def symbol_import_consumer_leads(self, *args, **kwargs):  # noqa: ANN002, ANN003
                return {
                    "leads": [
                        {
                            "importer": {"path": "pkg/importer.py", "display_name": "pkg.importer"},
                            "fact": {"evidence": [{"bytes_ref": {"path": "pkg/other.py", "line_start": 12}}]},
                        }
                    ]
                }

        breadth = compute_consumer_breadth(
            _FakeKg(),  # type: ignore[arg-type]
            symbol_urn="urn:target",
            changed_files=["pkg/lib.py"],
        ).to_json()

        self.assertEqual(
            breadth["top_out_of_diff"],
            [{"path": "pkg/importer.py", "qualname": "pkg.importer"}],
            "importer coordinates must not borrow line numbers from evidence in another file",
        )

    def test_compute_consumer_breadth_marks_saturated_enumeration_capped(self) -> None:
        from source.kg.query.caller_breadth import compute_consumer_breadth

        class _FakeKg:
            entities = []

            def find_callers(self, *args, **kwargs):  # noqa: ANN002, ANN003
                limit = int(kwargs.get("limit") or 201)
                return {
                    "target": {"status": "resolved"},
                    "callers": [
                        {
                            "subject": f"use_target_{index}",
                            "evidence": [
                                {"bytes_ref": {"path": f"pkg/consumer_{index}.py", "line_start": 3}},
                            ],
                        }
                        for index in range(limit)
                    ],
                }

            def symbol_import_consumer_leads(self, *args, **kwargs):  # noqa: ANN002, ANN003
                return {"leads": []}

        breadth = compute_consumer_breadth(
            _FakeKg(),  # type: ignore[arg-type]
            qualname="target",
            changed_files=["pkg/changed.py"],
        ).to_json()

        self.assertEqual(breadth["enumeration_status"], "capped")
        self.assertEqual(breadth["consumer_unit"], "file")
        self.assertEqual(breadth["total_consumers"], 200)
        self.assertEqual(breadth["out_of_diff_count"], 200)
        self.assertEqual(breadth["enumerated_limit"], 200)

    def test_compute_consumer_breadth_raw_fact_cap_is_not_file_cap(self) -> None:
        from source.kg.query.caller_breadth import compute_consumer_breadth

        class _FakeKg:
            entities = []

            def find_callers(self, *args, **kwargs):  # noqa: ANN002, ANN003
                limit = int(kwargs.get("limit") or 1001)
                return {
                    "target": {"status": "resolved"},
                    "callers": [
                        {
                            "subject": f"use_target_{index}",
                            "evidence": [
                                {"bytes_ref": {"path": "pkg/one_consumer.py", "line_start": index + 1}},
                            ],
                        }
                        for index in range(limit)
                    ],
                }

            def symbol_import_consumer_leads(self, *args, **kwargs):  # noqa: ANN002, ANN003
                return {"leads": []}

        breadth = compute_consumer_breadth(
            _FakeKg(),  # type: ignore[arg-type]
            qualname="target",
            changed_files=["pkg/changed.py"],
        ).to_json()

        self.assertEqual(breadth["enumeration_status"], "fact_capped")
        self.assertEqual(breadth["total_consumers"], 1)
        self.assertNotIn("enumerated_limit", breadth)
        self.assertEqual(breadth["raw_fact_limit"], 1000)

    def test_missing_consumer_coordinates_make_breadth_partial(self) -> None:
        from source.kg.query.caller_breadth import compute_consumer_breadth

        class _FakeKg:
            entities = []

            def find_callers(self, *args, **kwargs):  # noqa: ANN002, ANN003
                return {
                    "target": {"status": "resolved"},
                    "callers": [
                        {
                            "subject": "in_diff_consumer",
                            "evidence": [{"bytes_ref": {"path": "pkg/changed.py", "line_start": 3}}],
                        },
                        {
                            "subject": "unknown_consumer",
                            "evidence": [],
                        },
                    ],
                }

            def symbol_import_consumer_leads(self, *args, **kwargs):  # noqa: ANN002, ANN003
                return {"leads": []}

        breadth = compute_consumer_breadth(
            _FakeKg(),  # type: ignore[arg-type]
            qualname="target",
            changed_files=["pkg/changed.py"],
        ).to_json()

        self.assertEqual(breadth["enumeration_status"], "partial")
        self.assertEqual(breadth["total_consumers"], 1)
        self.assertEqual(breadth["in_diff_count"], 1)
        self.assertEqual(breadth["out_of_diff_count"], 0)
        self.assertEqual(breadth["unknown_coordinate_count"], 1)

    def test_review_context_contract_row_carries_consumer_breadth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out_base, out_head, base_repo, head_repo = _build_breadth_fixture(root)
            kg = KgSnapshot(out_head)

            with patch(
                "source.kg.product.mcp_tools._splice_semantic_diff_hypotheses",
                side_effect=_no_semantic_splice,
            ):
                result = call_tool(kg, "review_context", {
                    "repo": "repo",
                    "changed_files": ["pkg/registry.py", "pkg/consumer_a.py"],
                    "base_snapshot": str(out_base),
                    "base_checkout": str(base_repo),
                    "head_checkout": str(head_repo),
                    "execute_followups": False,
                })

        rows = [
            row for row in result.get("review_hypotheses") or []
            if row.get("risk_type") == "guard_call_removed_drift"
        ]
        self.assertTrue(rows, f"expected guard row; got {result.get('review_hypotheses')!r}")
        row = rows[0]
        self.assertEqual(row.get("subject_path"), "pkg/registry.py")
        breadth = row.get("consumer_breadth")
        self.assertIsInstance(breadth, dict, f"consumer_breadth missing from row: {row!r}")
        self.assertEqual(breadth.get("total_consumers"), 3)
        self.assertEqual(breadth.get("in_diff_count"), 1)
        self.assertEqual(breadth.get("out_of_diff_count"), 2)
        self.assertIn("2 outside", row.get("postable_claim", ""))
        self.assertTrue(
            any("known static consumer files" in str(check) for check in row.get("source_checks") or []),
            f"source_checks must name the static-consumer boundary: {row.get('source_checks')!r}",
        )

    def test_review_context_semantic_diff_row_carries_consumer_breadth(self) -> None:
        from source.kg.integrations.semantic_llm import LlmResult
        from source.kg.product.mcp_tools import _splice_semantic_diff_hypotheses as real_semantic_splice

        class _SemanticClient:
            model = "fake-model"

            def __init__(self) -> None:
                self.calls = 0

            def complete_json(self, prompt: str) -> LlmResult:
                self.calls += 1
                if self.calls == 1:
                    return LlmResult.parsed([])
                return LlmResult.parsed([
                    {
                        "claim": "make_widget no longer rejects missing config.",
                        "cause_line": 4,
                        "consequence": "Callers may pass missing config without an error.",
                        "negative_check": "If all callers guarantee config, this does not apply.",
                        "category": "guard_removal",
                        "old_contract": "make_widget rejected missing config via check.",
                        "new_contract": "make_widget returns a widget without calling check.",
                        "violated_invariant": "Callers relied on missing config being rejected.",
                    }
                ])

        fake_client = _SemanticClient()

        def _real_semantic_with_fake_client(**kwargs):
            return real_semantic_splice(**kwargs, _client=fake_client)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out_base, out_head, base_repo, head_repo = _build_breadth_fixture(root)
            kg = KgSnapshot(out_head)

            with patch(
                "source.kg.product.mcp_tools._splice_semantic_diff_hypotheses",
                side_effect=_real_semantic_with_fake_client,
            ):
                result = call_tool(kg, "review_context", {
                    "repo": "repo",
                    "changed_files": ["pkg/registry.py", "pkg/consumer_a.py"],
                    "base_snapshot": str(out_base),
                    "base_checkout": str(base_repo),
                    "head_checkout": str(head_repo),
                    "execute_followups": False,
                })

        rows = [
            row for row in result.get("review_hypotheses") or []
            if row.get("risk_type") == "contract_semantic_diff"
        ]
        self.assertTrue(rows, f"expected semantic row; got {result.get('review_hypotheses')!r}")
        breadth = rows[0].get("consumer_breadth")
        self.assertIsInstance(breadth, dict, f"semantic row missing consumer_breadth: {rows[0]!r}")
        self.assertEqual(breadth.get("total_consumers"), 3)
        self.assertEqual(breadth.get("out_of_diff_count"), 2)

    def test_direct_call_contract_drift_aggregate_row_is_not_breadth_enriched(self) -> None:
        from source.kg.product.mcp_tools import _attach_consumer_breadth_to_hypotheses

        row = {
            "hypothesis_id": "hypothesis:direct_call_contract_drift:1",
            "risk_type": "direct_call_contract_drift",
            "postable_claim": "caller calls callee with a changed contract.",
            "subject_urn": "urn:unrelated",
            "subject_qualname": "unrelated",
            "subject_path": "pkg/unrelated.py",
        }

        enriched = _attach_consumer_breadth_to_hypotheses(
            head_kg=object(),  # type: ignore[arg-type]
            changed_files=["pkg/unrelated.py"],
            review_hypotheses=[row],
        )

        self.assertEqual(enriched, [row])
        self.assertNotIn("consumer_breadth", enriched[0])

    def test_consumer_breadth_requires_explicit_subject_coordinates(self) -> None:
        from source.kg.product.mcp_tools import _attach_consumer_breadth_to_hypotheses

        class _FailIfUsedKg:
            entities = []

            def find_callers(self, *args, **kwargs):  # noqa: ANN002, ANN003
                raise AssertionError("cause-only rows must not trigger breadth enumeration")

        row = {
            "hypothesis_id": "hypothesis:guard_call_removed_drift:1",
            "risk_type": "guard_call_removed_drift",
            "postable_claim": "guard was removed.",
            "cause": {"path": "pkg/guard.py", "qualname": "not_the_subject"},
        }

        enriched = _attach_consumer_breadth_to_hypotheses(
            head_kg=_FailIfUsedKg(),  # type: ignore[arg-type]
            changed_files=["pkg/registry.py"],
            review_hypotheses=[row],
        )

        self.assertEqual(enriched, [row])
        self.assertNotIn("consumer_breadth", enriched[0])

    def test_consumer_breadth_attaches_to_all_subject_backed_risk_types(self) -> None:
        from source.kg.product.mcp_tools import _attach_consumer_breadth_to_hypotheses

        class _FakeKg:
            entities = [
                {
                    "kind": "CodeSymbol",
                    "urn": "urn:subject",
                    "identity": {"qualname": "make_widget"},
                    "properties": {"path": "pkg/registry.py"},
                }
            ]

            def find_callers(self, *args, **kwargs):  # noqa: ANN002, ANN003
                return {
                    "target": {"status": "resolved"},
                    "callers": [
                        {
                            "subject": "use_widget",
                            "evidence": [{"bytes_ref": {"path": "pkg/consumer.py", "line_start": 9}}],
                        }
                    ],
                }

            def symbol_import_consumer_leads(self, *args, **kwargs):  # noqa: ANN002, ANN003
                return {"leads": []}

        for risk_type in (
            "contract_semantic_diff",
            "responsibility_moved_drift",
            "abstract_contract_unimplemented",
        ):
            with self.subTest(risk_type=risk_type):
                row = {
                    "hypothesis_id": f"hypothesis:{risk_type}:1",
                    "risk_type": risk_type,
                    "postable_claim": "make_widget contract changed.",
                    "source_checks": [],
                    "subject_urn": "urn:subject",
                    "subject_qualname": "make_widget",
                    "subject_path": "pkg/registry.py",
                }

                enriched = _attach_consumer_breadth_to_hypotheses(
                    head_kg=_FakeKg(),  # type: ignore[arg-type]
                    changed_files=["pkg/registry.py"],
                    review_hypotheses=[row],
                )

                breadth = enriched[0].get("consumer_breadth")
                self.assertIsInstance(breadth, dict)
                self.assertEqual(breadth.get("total_consumers"), 1)
                self.assertEqual(breadth.get("out_of_diff_count"), 1)

    def test_consumer_breadth_failure_leaves_row_unenriched(self) -> None:
        from source.kg.product.mcp_tools import _attach_consumer_breadth_to_hypotheses

        class _RaisingKg:
            entities = []

            def find_callers(self, *args, **kwargs):  # noqa: ANN002, ANN003
                raise RuntimeError("boom")

        row = {
            "hypothesis_id": "hypothesis:guard_call_removed_drift:1",
            "risk_type": "guard_call_removed_drift",
            "postable_claim": "guard was removed.",
            "subject_qualname": "make_widget",
            "subject_path": "pkg/registry.py",
        }

        enriched = _attach_consumer_breadth_to_hypotheses(
            head_kg=_RaisingKg(),  # type: ignore[arg-type]
            changed_files=["pkg/registry.py"],
            review_hypotheses=[row],
        )

        self.assertEqual(enriched, [row])
        self.assertNotIn("consumer_breadth", enriched[0])

    def test_consumer_breadth_failures_do_not_consume_enumeration_cap(self) -> None:
        from source.kg.product.mcp_tools import _attach_consumer_breadth_to_hypotheses

        class _FlakyKg:
            entities = []

            def __init__(self) -> None:
                self.calls = 0

            def find_callers(self, *args, **kwargs):  # noqa: ANN002, ANN003
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("boom")
                return {"target": {"status": "resolved"}, "callers": []}

            def symbol_import_consumer_leads(self, *args, **kwargs):  # noqa: ANN002, ANN003
                return {"leads": []}

        rows = [
            {
                "hypothesis_id": f"hypothesis:guard_call_removed_drift:{index}",
                "risk_type": "guard_call_removed_drift",
                "postable_claim": "guard was removed.",
                "subject_qualname": f"subject_{index}",
                "subject_path": "pkg/lib.py",
            }
            for index in range(11)
        ]

        enriched = _attach_consumer_breadth_to_hypotheses(
            head_kg=_FlakyKg(),  # type: ignore[arg-type]
            changed_files=["pkg/lib.py"],
            review_hypotheses=rows,
        )

        self.assertNotIn("consumer_breadth", enriched[0])
        self.assertFalse(enriched[10].get("consumer_breadth_skipped"))
        self.assertIn(
            "consumer_breadth", enriched[10],
            "failed breadth attempts must not consume the successful-enumeration cap",
        )

    def test_consumer_breadth_enumeration_cap_marks_skipped_rows(self) -> None:
        from source.kg.product.mcp_tools import _attach_consumer_breadth_to_hypotheses

        class _FakeKg:
            entities = []

            def find_callers(self, *args, **kwargs):  # noqa: ANN002, ANN003
                return {"target": {"status": "resolved"}, "callers": []}

            def symbol_import_consumer_leads(self, *args, **kwargs):  # noqa: ANN002, ANN003
                return {"leads": []}

        rows = [
            {
                "hypothesis_id": f"hypothesis:guard_call_removed_drift:{index}",
                "risk_type": "guard_call_removed_drift",
                "postable_claim": "guard was removed.",
                "subject_qualname": f"subject_{index}",
                "subject_path": "pkg/lib.py",
            }
            for index in range(11)
        ]

        enriched = _attach_consumer_breadth_to_hypotheses(
            head_kg=_FakeKg(),  # type: ignore[arg-type]
            changed_files=["pkg/lib.py"],
            review_hypotheses=rows,
        )

        self.assertIn("consumer_breadth", enriched[9])
        self.assertNotIn("consumer_breadth", enriched[10])
        self.assertTrue(enriched[10].get("consumer_breadth_skipped"))
        self.assertEqual(enriched[10].get("consumer_breadth_skip_reason"), "enumeration_cap")

    def test_consumer_breadth_status_messages_are_honest(self) -> None:
        from source.kg.product.mcp_tools import _row_with_consumer_breadth

        base = {
            "hypothesis_id": "hypothesis:guard_call_removed_drift:1",
            "risk_type": "guard_call_removed_drift",
            "postable_claim": "guard was removed.",
            "concrete_invariant": "The guard call enforced a caller-visible invariant.",
            "source_checks": [],
        }

        capped = _row_with_consumer_breadth(
            base,
            {
                "total_consumers": 200,
                "consumer_unit": "file",
                "in_diff_count": 1,
                "out_of_diff_count": 199,
                "enumeration_status": "capped",
                "enumerated_limit": 200,
                "top_out_of_diff": [{"path": "pkg/consumer.py", "line": 7}],
            },
        )
        self.assertIn("at least 200 known static consumer files", capped["postable_claim"])
        self.assertIn("at least 200 known static consumer files", capped["concrete_invariant"])
        self.assertTrue(any("lower bounds" in check for check in capped["source_checks"]))

        synthesized = _row_with_consumer_breadth(
            {
                "hypothesis_id": "hypothesis:guard_call_removed_drift:2",
                "risk_type": "guard_call_removed_drift",
                "concrete_invariant": "The guard call enforced a caller-visible invariant.",
                "source_checks": [],
            },
            {
                "total_consumers": 2,
                "consumer_unit": "file",
                "in_diff_count": 0,
                "out_of_diff_count": 2,
                "enumeration_status": "complete",
                "top_out_of_diff": [{"path": "pkg/consumer.py", "line": 7}],
            },
        )
        self.assertIn("2 known static consumer files", synthesized["postable_claim"])
        self.assertEqual(
            synthesized["concrete_invariant"],
            "The guard call enforced a caller-visible invariant.",
        )

        fact_capped = _row_with_consumer_breadth(
            base,
            {
                "total_consumers": 1,
                "consumer_unit": "file",
                "in_diff_count": 0,
                "out_of_diff_count": 1,
                "enumeration_status": "fact_capped",
                "raw_fact_limit": 1000,
                "top_out_of_diff": [{"path": "pkg/consumer.py", "line": 7}],
            },
        )
        self.assertIn("raw fact scan limit", fact_capped["postable_claim"])
        self.assertTrue(any("raw CALLS/IMPORTS fact scan limit" in check for check in fact_capped["source_checks"]))

        partial = _row_with_consumer_breadth(
            base,
            {
                "total_consumers": 1,
                "consumer_unit": "file",
                "in_diff_count": 1,
                "out_of_diff_count": 0,
                "enumeration_status": "partial",
                "unknown_coordinate_count": 1,
                "top_out_of_diff": [],
            },
        )
        self.assertNotIn("all in changed files", " ".join(partial["source_checks"]))
        self.assertTrue(any("lacked source coordinates" in check for check in partial["source_checks"]))

        no_facts = _row_with_consumer_breadth(
            base,
            {
                "total_consumers": 0,
                "consumer_unit": "file",
                "in_diff_count": 0,
                "out_of_diff_count": 0,
                "enumeration_status": "no_facts",
                "top_out_of_diff": [],
            },
        )
        self.assertTrue(any("No known static consumers" in check for check in no_facts["source_checks"]))

        unresolved = _row_with_consumer_breadth(
            base,
            {
                "total_consumers": 0,
                "consumer_unit": "file",
                "in_diff_count": 0,
                "out_of_diff_count": 0,
                "enumeration_status": "unresolved",
                "top_out_of_diff": [],
            },
        )
        self.assertTrue(any("could not resolve" in check for check in unresolved["source_checks"]))

    def test_abstract_contract_no_facts_does_not_emit_consumer_absence_check(self) -> None:
        from source.kg.product.mcp_tools import _row_with_consumer_breadth

        row = {
            "hypothesis_id": "hypothesis:abstract_contract_unimplemented:1",
            "risk_type": "abstract_contract_unimplemented",
            "postable_claim": "Subclass gained an unimplemented abstract contract.",
            "source_checks": [],
        }

        enriched = _row_with_consumer_breadth(
            row,
            {
                "total_consumers": 0,
                "consumer_unit": "file",
                "in_diff_count": 0,
                "out_of_diff_count": 0,
                "enumeration_status": "no_facts",
                "top_out_of_diff": [],
            },
        )

        self.assertEqual(enriched.get("source_checks"), [])

    def test_compact_review_hypothesis_preserves_consumer_breadth_counts(self) -> None:
        from source.kg.product.output_budget import _compact_review_hypothesis

        row = {
            "hypothesis_id": "hypothesis:guard_call_removed_drift:1",
            "label": "guard_call_removed_drift-0001",
            "risk_type": "guard_call_removed_drift",
            "specificity": "high",
            "confidence": "medium",
            "postable_claim": "make_widget has out-of-diff consumers.",
            "consumer_breadth": {
                "total_consumers": 4,
                "consumer_unit": "file",
                "in_diff_count": 1,
                "out_of_diff_count": 3,
                "enumeration_status": "capped",
                "enumerated_limit": 200,
                "raw_fact_limit": 1000,
                "unknown_coordinate_count": 1,
                "top_out_of_diff": [
                    {"path": "pkg/a.py", "line": 3, "qualname": "use_a"},
                    {"path": "pkg/b.py", "line": 4, "qualname": "use_b"},
                ],
            },
        }

        compact = _compact_review_hypothesis(row)

        self.assertEqual(compact["consumer_breadth"]["total_consumers"], 4)
        self.assertEqual(compact["consumer_breadth"]["consumer_unit"], "file")
        self.assertEqual(compact["consumer_breadth"]["out_of_diff_count"], 3)
        self.assertEqual(compact["consumer_breadth"]["enumerated_limit"], 200)
        self.assertEqual(compact["consumer_breadth"]["raw_fact_limit"], 1000)
        self.assertEqual(compact["consumer_breadth"]["unknown_coordinate_count"], 1)
        self.assertEqual(
            compact["consumer_breadth"]["top_out_of_diff"],
            [{"path": "pkg/a.py", "line": 3, "qualname": "use_a"}],
            "compact hypothesis rows should retain one inspection coordinate when out_of_diff_count > 0",
        )

    def test_compact_review_hypothesis_preserves_consumer_breadth_skip_marker(self) -> None:
        from source.kg.product.output_budget import _compact_review_hypothesis

        row = {
            "hypothesis_id": "hypothesis:guard_call_removed_drift:overflow",
            "label": "guard_call_removed_drift-0011",
            "risk_type": "guard_call_removed_drift",
            "specificity": "medium",
            "confidence": "medium",
            "postable_claim": "Overflow row still needs a breadth honesty marker.",
            "consumer_breadth_skipped": True,
            "consumer_breadth_skip_reason": "enumeration_cap",
            "consumer_breadth_enumeration_cap": 10,
        }

        compact = _compact_review_hypothesis(row)

        self.assertNotIn("consumer_breadth", compact)
        self.assertTrue(compact.get("consumer_breadth_skipped"))
        self.assertEqual(compact.get("consumer_breadth_skip_reason"), "enumeration_cap")
        self.assertEqual(compact.get("consumer_breadth_enumeration_cap"), 10)
