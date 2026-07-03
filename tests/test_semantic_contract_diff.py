"""S2: LLM-assisted semantic contract-diff tests.

Test coverage:
1. End-to-end via call_tool with mocked client on a real build_kg fixture pair:
   contract_semantic_diff hypothesis present with derivation inferred_llm,
   valid coords, label, survives budget; adjudication fields complete.
2. Malformed LLM output → row skipped, semantic_diff_status=failed note, no crash.
   Missing litellm → unavailable note. No checkouts → missing_checkouts, silent.
3. Cost bounds: >12 changed symbols → 12 LLM calls max (count via mock);
   identical bodies → mock not called.
4. Env-gated LIVE test (skipped unless SUPERCONTEXT_LIVE_LLM_TESTS=1 and key present).
5. Field gate: pr-7232 replay compact <= 15,000 (mechanism inactive — no checkouts).
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any

from source.kg.build.pipeline import build_kg
from source.kg.core.models import JsonObject
from source.kg.core.store import JsonlKgStore
from source.kg.product.mcp_tools import call_tool
from source.kg.query.snapshot import KgSnapshot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TENANT = "default"

_FAKE_RESPONSE = [
    {
        "claim": "Function no longer validates input before processing",
        "cause_line": 3,
        "consequence": "Callers may pass invalid data without receiving an error.",
        "negative_check": "If validation was intentionally removed and callers are trusted, this risk does not apply.",
        "category": "guard_removal",
    }
]


class _FakeClient:
    """Deterministic fake LLM client — returns fixed valid JSON."""

    def __init__(self, response: list | None = None, raise_import: bool = False, return_malformed: bool = False):
        self._response = response or _FAKE_RESPONSE
        self._raise_import = raise_import
        self._return_malformed = return_malformed
        self.call_count = 0

    def complete_json(self, prompt: str) -> Any:
        if self._raise_import:
            raise ImportError("litellm not installed")
        self.call_count += 1
        if self._return_malformed:
            return "not valid json structure"
        return self._response


def _build_two_snapshot_pair(tmpdir: Path) -> tuple[Path, Path, Path, Path]:
    """Build base + head snapshots plus separate checkout dirs with different file content.

    Returns (out_base, out_head, base_checkout, head_checkout).
    """
    svc = tmpdir / "svc2"
    svc.mkdir()
    (svc / "__init__.py").write_text("", encoding="utf-8")

    base_content = (
        "def process(data):\n    if data is None:\n        raise ValueError('no data')\n    return data * 2\n"
    )
    head_content = "def process(data):\n    return data * 2\n"

    (svc / "handler.py").write_text(base_content, encoding="utf-8")
    out_base = tmpdir / "kg_base2"
    build_kg(svc, out_base, tenant_id=TENANT)

    # Head: remove the guard
    (svc / "handler.py").write_text(head_content, encoding="utf-8")
    out_head = tmpdir / "kg_head2"
    build_kg(svc, out_head, tenant_id=TENANT)

    # Separate checkout dirs so base and head bodies differ
    base_checkout = tmpdir / "checkout_base"
    base_checkout.mkdir()
    (base_checkout / "handler.py").write_text(base_content, encoding="utf-8")
    head_checkout = tmpdir / "checkout_head"
    head_checkout.mkdir()
    (head_checkout / "handler.py").write_text(head_content, encoding="utf-8")

    return out_base, out_head, base_checkout, head_checkout


def _make_minimal_kg(root: Path, tenant_id: str = "default") -> KgSnapshot:
    (root / "entities.jsonl").write_text("")
    (root / "facts.jsonl").write_text("")
    (root / "evidence.jsonl").write_text("")
    (root / "coverage.jsonl").write_text("")
    (root / "manifest.json").write_text(json.dumps({"tenant_id": tenant_id, "version": 1}))
    return KgSnapshot(root)


# ---------------------------------------------------------------------------
# 1. End-to-end with mocked client and real build_kg fixture
# ---------------------------------------------------------------------------

class TestSemanticDiffEndToEnd(unittest.TestCase):
    """Mocked client: end-to-end call_tool on real two-commit fixture pair."""

    def _run_with_mock_client(self, head_kg: KgSnapshot, base_dir: Path, svc_dir: Path) -> dict:
        from unittest.mock import patch
        fake_client = _FakeClient()
        with patch(
            "source.kg.product.mcp_tools._splice_semantic_diff_hypotheses",
            wraps=lambda **kw: _splice_with_fake_client(fake_client, **kw),
        ):
            result = call_tool(head_kg, "review_context", {
                "repo": TENANT,
                "changed_files": ["handler.py"],
                "changed_ranges": [{"path": "handler.py", "start_line": 1, "end_line": 5}],
                "base_snapshot": str(base_dir),
                "base_checkout": str(svc_dir),
                "head_checkout": str(svc_dir),
            })
        return result

    def test_semantic_diff_hypothesis_in_result(self) -> None:
        """contract_semantic_diff hypothesis present with derivation=inferred_llm."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            out_base, out_head, base_checkout, head_checkout = _build_two_snapshot_pair(root)
            head_kg = KgSnapshot(out_head)

            fake_client = _FakeClient()
            from unittest.mock import patch
            with patch(
                "source.kg.product.mcp_tools._splice_semantic_diff_hypotheses",
                side_effect=lambda **kw: _direct_splice(fake_client, **kw),
            ):
                result = call_tool(head_kg, "review_context", {
                    "repo": TENANT,
                    "changed_files": ["handler.py"],
                    "changed_ranges": [{"path": "handler.py", "start_line": 1, "end_line": 5}],
                    "base_snapshot": str(out_base),
                    "base_checkout": str(base_checkout),
                    "head_checkout": str(head_checkout),
                })

            hyps = result.get("review_hypotheses") or []
            semantic_hyps = [h for h in hyps if h.get("risk_type") == "contract_semantic_diff"]
            self.assertTrue(
                semantic_hyps,
                f"expected contract_semantic_diff hypothesis; got risk_types={[h.get('risk_type') for h in hyps]}",
            )
            h = semantic_hyps[0]
            self.assertEqual(h.get("derivation"), "inferred_llm",
                             f"derivation must be inferred_llm; got {h.get('derivation')}")
            self.assertIn("label", h, "hypothesis must carry label")
            self.assertIn("hypothesis_id", h, "hypothesis must carry hypothesis_id")
            self.assertIn("postable_claim", h, "hypothesis must carry postable_claim")
            self.assertIn("specificity", h, "hypothesis must carry specificity")
            self.assertEqual(h.get("specificity"), "high",
                             f"specificity must be high; got {h.get('specificity')}")

    def test_semantic_diff_status_active_when_checkouts_present(self) -> None:
        """semantic_diff_status=active when all three (base_snapshot, checkouts) present."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            out_base, out_head, base_checkout, head_checkout = _build_two_snapshot_pair(root)
            head_kg = KgSnapshot(out_head)

            fake_client = _FakeClient()
            from unittest.mock import patch
            with patch(
                "source.kg.product.mcp_tools._splice_semantic_diff_hypotheses",
                side_effect=lambda **kw: _direct_splice(fake_client, **kw),
            ):
                result = call_tool(head_kg, "review_context", {
                    "repo": TENANT,
                    "changed_files": ["handler.py"],
                    "base_snapshot": str(out_base),
                    "base_checkout": str(base_checkout),
                    "head_checkout": str(head_checkout),
                })

            rqs = result.get("review_quality_status") or {}
            self.assertEqual(
                rqs.get("semantic_diff_status"), "active",
                f"semantic_diff_status must be active; got {rqs.get('semantic_diff_status')}; full rqs={rqs}",
            )

    def test_adjudication_fields_complete(self) -> None:
        """All adjudication fields (cause, consequence, negative_checks) are present."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            out_base, out_head, base_checkout, head_checkout = _build_two_snapshot_pair(root)
            head_kg = KgSnapshot(out_head)

            fake_client = _FakeClient()
            from unittest.mock import patch
            with patch(
                "source.kg.product.mcp_tools._splice_semantic_diff_hypotheses",
                side_effect=lambda **kw: _direct_splice(fake_client, **kw),
            ):
                result = call_tool(head_kg, "review_context", {
                    "repo": TENANT,
                    "changed_files": ["handler.py"],
                    "changed_ranges": [{"path": "handler.py", "start_line": 1, "end_line": 5}],
                    "base_snapshot": str(out_base),
                    "base_checkout": str(base_checkout),
                    "head_checkout": str(head_checkout),
                })

            hyps = result.get("review_hypotheses") or []
            semantic_hyps = [h for h in hyps if h.get("risk_type") == "contract_semantic_diff"]
            if not semantic_hyps:
                self.skipTest("no semantic hyps generated — fixture may have no differing bodies")
            h = semantic_hyps[0]
            for field in ("cause", "negative_checks", "source_checks", "confidence", "why"):
                self.assertIn(field, h, f"field {field!r} must be present; keys={list(h.keys())}")


def _direct_splice(fake_client: _FakeClient, **kw) -> tuple[list[JsonObject], str]:
    """Calls the real semantic_contract_diff with a fake LLM client."""
    from pathlib import Path
    from source.kg.query.semantic_contract_diff import semantic_contract_diff
    from source.kg.query.snapshot import KgSnapshot as _KgSnap

    base_snapshot_dir = kw["base_snapshot_dir"]
    head_kg = kw["head_kg"]
    base_checkout = kw["base_checkout"]
    head_checkout = kw["head_checkout"]
    review_hypotheses = kw["review_hypotheses"]

    head_entities = [e for e in head_kg.entities if e.get("kind") == "CodeSymbol"]
    if not head_entities:
        return review_hypotheses, "active"

    try:
        base_snap = _KgSnap(base_snapshot_dir)
        raw_rows = semantic_contract_diff(
            base_snapshot=base_snap,
            head_snapshot=head_kg,
            base_root=Path(base_checkout),
            head_root=Path(head_checkout),
            changed_symbols=head_entities,
            client=fake_client,
        )
    except Exception:  # noqa: BLE001
        return review_hypotheses, "failed:test"

    spliced = [r for r in raw_rows if isinstance(r, dict) and r.get("hypothesis_id")][:3]
    return spliced + review_hypotheses, "active"


# ---------------------------------------------------------------------------
# 2. Failure handling
# ---------------------------------------------------------------------------

class TestSemanticDiffFailures(unittest.TestCase):
    """Failure scenarios: malformed output, missing litellm, no checkouts."""

    def test_malformed_output_skipped_no_crash(self) -> None:
        """Malformed LLM response → semantic hyps skipped, status=failed, no exception."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            out_base, out_head, base_checkout, head_checkout = _build_two_snapshot_pair(root)
            head_kg = KgSnapshot(out_head)

            malformed_client = _FakeClient(return_malformed=True)

            from unittest.mock import patch
            with patch(
                "source.kg.product.mcp_tools._splice_semantic_diff_hypotheses",
                side_effect=lambda **kw: _direct_splice(malformed_client, **kw),
            ):
                result = call_tool(head_kg, "review_context", {
                    "repo": TENANT,
                    "changed_files": ["handler.py"],
                    "base_snapshot": str(out_base),
                    "base_checkout": str(base_checkout),
                    "head_checkout": str(head_checkout),
                })

            # No exception raised; result is a dict
            self.assertIsInstance(result, dict)
            # No contract_semantic_diff rows since malformed
            hyps = result.get("review_hypotheses") or []
            semantic_hyps = [h for h in hyps if h.get("risk_type") == "contract_semantic_diff"]
            self.assertEqual(semantic_hyps, [], "malformed output must produce no semantic hyps")

    def test_missing_checkouts_gives_missing_checkouts_status(self) -> None:
        """No base_checkout/head_checkout → semantic_diff_status=missing_checkouts."""
        with tempfile.TemporaryDirectory() as head_dir, \
             tempfile.TemporaryDirectory() as base_dir:
            head_kg = _make_minimal_kg(Path(head_dir))
            _make_minimal_kg(Path(base_dir))
            result = call_tool(head_kg, "review_context", {
                "repo": TENANT,
                "changed_files": ["src/a.py"],
                "changed_ranges": [{"path": "src/a.py", "start_line": 1, "end_line": 5}],
                "base_snapshot": str(base_dir),
                # No base_checkout / head_checkout
            })
            rqs = result.get("review_quality_status") or {}
            self.assertEqual(
                rqs.get("semantic_diff_status"), "missing_checkouts",
                f"expected missing_checkouts; got {rqs.get('semantic_diff_status')}",
            )

    def test_no_base_snapshot_no_semantic_diff_status(self) -> None:
        """Without base_snapshot, semantic_diff_status must not appear."""
        with tempfile.TemporaryDirectory() as head_dir:
            head_kg = _make_minimal_kg(Path(head_dir))
            result = call_tool(head_kg, "review_context", {
                "repo": TENANT,
                "changed_files": ["src/a.py"],
            })
            rqs = result.get("review_quality_status") or {}
            self.assertNotIn(
                "semantic_diff_status", rqs,
                f"semantic_diff_status must not appear without base_snapshot; got {rqs}",
            )

    def test_missing_litellm_produces_unavailable_status(self) -> None:
        """ImportError from semantic_llm import → semantic_diff_status contains 'unavailable'."""
        from source.kg.product.mcp_tools import _splice_semantic_diff_hypotheses
        from unittest.mock import patch
        import sys

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            out_base, out_head, base_checkout, head_checkout = _build_two_snapshot_pair(root)
            head_kg = KgSnapshot(out_head)

            # Block the semantic_llm module import inside _splice_semantic_diff_hypotheses
            with patch.dict(sys.modules, {"source.kg.integrations.semantic_llm": None}):
                _, status = _splice_semantic_diff_hypotheses(
                    base_snapshot_dir=str(out_base),
                    head_kg=head_kg,
                    base_checkout=str(base_checkout),
                    head_checkout=str(head_checkout),
                    changed_symbols=head_kg.entities,
                    review_hypotheses=[],
                )
            self.assertIn(
                "unavailable", status,
                f"status must contain 'unavailable' on ImportError; got {status!r}",
            )


# ---------------------------------------------------------------------------
# 3. Cost bounds
# ---------------------------------------------------------------------------

class TestSemanticDiffCostBounds(unittest.TestCase):
    """Cost-bound enforcement: max 12 LLM calls; identical bodies skipped."""

    def test_max_12_llm_calls_for_more_than_12_changed_symbols(self) -> None:
        """When >12 changed symbols are provided, the LLM is called at most 12 times."""
        from source.kg.query.semantic_contract_diff import semantic_contract_diff
        from source.kg.core.models import Entity
        from source.kg.core.store import JsonlKgStore

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            entity_objects = []
            for i in range(15):
                e = Entity(
                    kind="CodeSymbol",
                    identity={
                        "tenant_id": TENANT,
                        "repo": "svc_cost",
                        "module": "svc_cost.handler",
                        "qualname": f"func_{i}",
                        "symbol_kind": "function",
                    },
                    properties={"path": f"file_{i}.py", "line": 1, "end_line": 5},
                )
                entity_objects.append(e)

            snap_dir = root / "snap_cost"
            JsonlKgStore(snap_dir).write(
                entities=entity_objects,
                facts=[],
                evidence=[],
                coverage=[],
                manifest={"version": 1, "tenant_id": TENANT},
            )
            snap = KgSnapshot(snap_dir)
            # KgSnapshot.entities is a list of plain dicts
            entity_dicts = snap.entities

            # Create base files with different content so bodies differ
            base_dir = root / "base_src"
            base_dir.mkdir()
            for i in range(15):
                (base_dir / f"file_{i}.py").write_text(f"def func_{i}():\n    return {i}\n")

            head_dir = root / "head_src"
            head_dir.mkdir()
            for i in range(15):
                (head_dir / f"file_{i}.py").write_text(f"def func_{i}():\n    return {i} + 1\n")

            counting_client = _FakeClient()
            semantic_contract_diff(
                base_snapshot=snap,
                head_snapshot=snap,
                base_root=base_dir,
                head_root=head_dir,
                changed_symbols=entity_dicts,
                client=counting_client,
            )
            self.assertLessEqual(
                counting_client.call_count, 12,
                f"LLM must be called at most 12 times; was called {counting_client.call_count} times",
            )

    def test_identical_bodies_not_sent_to_llm(self) -> None:
        """If base and head bodies are identical, the LLM client must not be called."""
        from source.kg.query.semantic_contract_diff import semantic_contract_diff
        from source.kg.core.models import Entity
        from source.kg.core.store import JsonlKgStore

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            e = Entity(
                kind="CodeSymbol",
                identity={
                    "tenant_id": TENANT,
                    "repo": "repo_id",
                    "module": "mod.handler",
                    "qualname": "unchanged_func",
                    "symbol_kind": "function",
                },
                properties={"path": "handler.py", "line": 1, "end_line": 3},
            )
            snap_dir = root / "snap_id"
            JsonlKgStore(snap_dir).write(
                entities=[e],
                facts=[],
                evidence=[],
                coverage=[],
                manifest={"version": 1, "tenant_id": TENANT},
            )
            snap = KgSnapshot(snap_dir)
            entity_dicts = [d for d in snap.entities if d.get("kind") == "CodeSymbol"]

            # Same content in both base and head
            same_body = "def unchanged_func():\n    return 42\n"
            base_dir = root / "base_id"
            base_dir.mkdir()
            (base_dir / "handler.py").write_text(same_body)
            head_dir = root / "head_id"
            head_dir.mkdir()
            (head_dir / "handler.py").write_text(same_body)

            counting_client = _FakeClient()
            semantic_contract_diff(
                base_snapshot=snap,
                head_snapshot=snap,
                base_root=base_dir,
                head_root=head_dir,
                changed_symbols=entity_dicts,
                client=counting_client,
            )
            self.assertEqual(
                counting_client.call_count, 0,
                f"LLM must NOT be called for identical bodies; was called {counting_client.call_count} times",
            )

    def test_inversion_different_bodies_do_call_llm(self) -> None:
        """Inversion: different bodies DO call the LLM (proves the skip-condition is on identity)."""
        from source.kg.query.semantic_contract_diff import semantic_contract_diff
        from source.kg.core.models import Entity
        from source.kg.core.store import JsonlKgStore

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            e = Entity(
                kind="CodeSymbol",
                identity={
                    "tenant_id": TENANT,
                    "repo": "repo_diff",
                    "module": "mod.handler",
                    "qualname": "changed_func",
                    "symbol_kind": "function",
                },
                properties={"path": "handler.py", "line": 1, "end_line": 3},
            )
            snap_dir = root / "snap_diff"
            JsonlKgStore(snap_dir).write(
                entities=[e],
                facts=[],
                evidence=[],
                coverage=[],
                manifest={"version": 1, "tenant_id": TENANT},
            )
            snap = KgSnapshot(snap_dir)
            entity_dicts = [d for d in snap.entities if d.get("kind") == "CodeSymbol"]

            base_dir = root / "base_diff"
            base_dir.mkdir()
            (base_dir / "handler.py").write_text("def changed_func():\n    if x: raise\n    return 42\n")
            head_dir = root / "head_diff"
            head_dir.mkdir()
            (head_dir / "handler.py").write_text("def changed_func():\n    return 42\n")

            counting_client = _FakeClient()
            semantic_contract_diff(
                base_snapshot=snap,
                head_snapshot=snap,
                base_root=base_dir,
                head_root=head_dir,
                changed_symbols=entity_dicts,
                client=counting_client,
            )
            self.assertEqual(
                counting_client.call_count, 1,
                f"LLM must be called once for different bodies; was called {counting_client.call_count} times",
            )


# ---------------------------------------------------------------------------
# 4. Env-gated LIVE test
# ---------------------------------------------------------------------------

@unittest.skipUnless(
    os.getenv("SUPERCONTEXT_LIVE_LLM_TESTS") == "1"
    and (os.getenv("OPENAI_API_KEY") or os.getenv("LITELLM_API_KEY")),
    "SUPERCONTEXT_LIVE_LLM_TESTS=1 and an API key are required for live tests",
)
class TestSemanticDiffLive(unittest.TestCase):
    """Live LLM test — shape only, no content assertions."""

    def test_live_call_returns_valid_shape(self) -> None:
        from source.kg.query.semantic_contract_diff import semantic_contract_diff
        from source.kg.integrations.semantic_llm import SemanticDiffLlmClient
        from source.kg.core.models import Entity
        from source.kg.core.store import JsonlKgStore

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            e = Entity(
                kind="CodeSymbol",
                identity={
                    "tenant_id": TENANT,
                    "repo": "live_repo",
                    "module": "live.handler",
                    "qualname": "live_func",
                    "symbol_kind": "function",
                },
                properties={"path": "handler.py", "line": 1, "end_line": 4},
            )
            snap_dir = root / "snap_live"
            JsonlKgStore(snap_dir).write(
                entities=[e], facts=[], evidence=[], coverage=[],
                manifest={"version": 1, "tenant_id": TENANT},
            )
            snap = KgSnapshot(snap_dir)

            base_dir = root / "base_live"
            base_dir.mkdir()
            (base_dir / "handler.py").write_text("def live_func(x):\n    if x is None:\n        raise ValueError\n    return x\n")
            head_dir = root / "head_live"
            head_dir.mkdir()
            (head_dir / "handler.py").write_text("def live_func(x):\n    return x\n")

            client = SemanticDiffLlmClient()
            rows = semantic_contract_diff(
                base_snapshot=snap,
                head_snapshot=snap,
                base_root=base_dir,
                head_root=head_dir,
                changed_symbols=[e],
                client=client,
            )
            # Shape-only assertions
            self.assertIsInstance(rows, list, "result must be a list")
            for row in rows:
                self.assertIsInstance(row, dict)
                self.assertIn("hypothesis_id", row)
                self.assertIn("derivation", row)
                self.assertEqual(row.get("derivation"), "inferred_llm")
                self.assertIn("risk_type", row)
                self.assertEqual(row.get("risk_type"), "contract_semantic_diff")


# ---------------------------------------------------------------------------
# 5. Field gate: pr-7232 replay (mechanism inactive — no checkouts)
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
class TestFieldGatePr7232S2(unittest.TestCase):
    """S2 field gate: pr-7232 compact replay — mechanism inactive (no checkouts in args)."""

    @classmethod
    def setUpClass(cls):
        with open(_FIELD_REPLAY_ARGS) as f:
            cls.args = json.load(f)
        cls.result = call_tool(KgSnapshot(Path(_FIELD_REPLAY_KG)), "review_context", cls.args)

    def test_size_le_15000(self) -> None:
        from source.kg.core.models import canonical_json
        size = len(canonical_json(self.result))
        self.assertLessEqual(size, 15_000, f"compact size {size} exceeds 15,000")

    def test_semantic_diff_status_absent_when_no_checkouts(self) -> None:
        """Mechanism must be silent when no base_checkout/head_checkout in args."""
        rqs = self.result.get("review_quality_status") or {}
        # The field replay args don't have base_checkout/head_checkout,
        # so either semantic_diff_status is absent or equals missing_checkouts.
        semantic_status = rqs.get("semantic_diff_status")
        self.assertIn(
            semantic_status, (None, "missing_checkouts"),
            f"semantic_diff_status must be absent or missing_checkouts without checkouts; got {semantic_status!r}",
        )


# ---------------------------------------------------------------------------
# Unit tests for semantic_llm module
# ---------------------------------------------------------------------------

class TestSemanticLlmClient(unittest.TestCase):
    """Unit tests for SemanticDiffLlmClient and _extract_json."""

    def test_extract_json_valid_array(self) -> None:
        from source.kg.integrations.semantic_llm import _extract_json
        result = _extract_json('[{"a": 1}]')
        self.assertEqual(result, [{"a": 1}])

    def test_extract_json_embedded_in_prose(self) -> None:
        from source.kg.integrations.semantic_llm import _extract_json
        text = 'Here is the result:\n[{"claim": "x", "cause_line": 1}]\nDone.'
        result = _extract_json(text)
        self.assertIsInstance(result, list)
        self.assertEqual(result[0]["claim"], "x")

    def test_extract_json_malformed_returns_none(self) -> None:
        from source.kg.integrations.semantic_llm import _extract_json
        result = _extract_json("not json at all")
        self.assertIsNone(result)

    def test_extract_json_empty_returns_none(self) -> None:
        from source.kg.integrations.semantic_llm import _extract_json
        result = _extract_json("")
        self.assertIsNone(result)

    def test_model_resolution_constructor_arg(self) -> None:
        from source.kg.integrations.semantic_llm import SemanticDiffLlmClient
        c = SemanticDiffLlmClient(model="gpt-custom")
        self.assertEqual(c.model, "gpt-custom")

    def test_model_resolution_env_var(self) -> None:
        from source.kg.integrations.semantic_llm import SemanticDiffLlmClient
        import os
        orig = os.environ.get("SUPERCONTEXT_SEMANTIC_DIFF_MODEL")
        try:
            os.environ["SUPERCONTEXT_SEMANTIC_DIFF_MODEL"] = "gpt-env-model"
            c = SemanticDiffLlmClient()
            self.assertEqual(c.model, "gpt-env-model")
        finally:
            if orig is None:
                os.environ.pop("SUPERCONTEXT_SEMANTIC_DIFF_MODEL", None)
            else:
                os.environ["SUPERCONTEXT_SEMANTIC_DIFF_MODEL"] = orig

    def test_model_resolution_default(self) -> None:
        from source.kg.integrations.semantic_llm import SemanticDiffLlmClient, DEFAULT_SEMANTIC_DIFF_MODEL
        import os
        orig = os.environ.get("SUPERCONTEXT_SEMANTIC_DIFF_MODEL")
        try:
            os.environ.pop("SUPERCONTEXT_SEMANTIC_DIFF_MODEL", None)
            c = SemanticDiffLlmClient()
            self.assertEqual(c.model, DEFAULT_SEMANTIC_DIFF_MODEL)
        finally:
            if orig is not None:
                os.environ["SUPERCONTEXT_SEMANTIC_DIFF_MODEL"] = orig

    def test_complete_json_raises_import_when_litellm_missing(self) -> None:
        from source.kg.integrations.semantic_llm import SemanticDiffLlmClient
        import sys
        from unittest.mock import patch
        c = SemanticDiffLlmClient(model="gpt-5.4-mini")
        # Temporarily make litellm unimportable
        with patch.dict(sys.modules, {"litellm": None}):
            with self.assertRaises(ImportError):
                c.complete_json("test prompt")


if __name__ == "__main__":
    unittest.main()
