"""S2: LLM-assisted semantic contract-diff tests.

Test coverage:
1. End-to-end via call_tool with mocked client on a real build_kg fixture pair:
   contract_semantic_diff hypothesis present with derivation inferred_llm,
   valid coords, label, survives budget; adjudication fields complete.
2. Malformed LLM output → row skipped, no crash.
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

# Real repo names assigned by build_kg — the svc directory name, not the tenant ID.
_SVC2_REPO = "svc2"      # _build_two_snapshot_pair
_SVC_TWO_REPO = "svc_two"  # _build_two_symbol_snapshot_pair

_FAKE_RESPONSE = [
    {
        "claim": "Function no longer validates input before processing",
        "cause_line": 3,
        "consequence": "Callers may pass invalid data without receiving an error.",
        "negative_check": "If validation was intentionally removed and callers are trusted, this risk does not apply.",
        "category": "guard_removal",
    }
]


from source.kg.integrations.semantic_llm import LlmResult


class _FakeClient:
    """Deterministic fake LLM client — returns fixed valid JSON via LlmResult."""

    def __init__(self, response: list | None = None, raise_import: bool = False, return_malformed: bool = False):
        self._response = response or _FAKE_RESPONSE
        self._raise_import = raise_import
        self._return_malformed = return_malformed
        self.call_count = 0

    def complete_json(self, prompt: str) -> LlmResult:
        if self._raise_import:
            raise ImportError("litellm not installed")
        self.call_count += 1
        if self._return_malformed:
            return LlmResult.parse_miss()
        return LlmResult.parsed(self._response)


class _FailingClient:
    """Client that always raises a non-auth RuntimeError."""

    def __init__(self, exc: Exception | None = None):
        self._exc = exc or RuntimeError("timeout")
        self.call_count = 0

    def complete_json(self, prompt: str) -> LlmResult:
        self.call_count += 1
        raise self._exc


class _PartialFailClient:
    """Client that succeeds on the first call, then raises on subsequent calls."""

    def __init__(self, success_response: list | None = None):
        self._response = success_response or _FAKE_RESPONSE
        self.call_count = 0

    def complete_json(self, prompt: str) -> LlmResult:
        self.call_count += 1
        if self.call_count == 1:
            return LlmResult.parsed(self._response)
        raise RuntimeError("timeout on call 2+")


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


def _build_two_symbol_snapshot_pair(tmpdir: Path) -> tuple[Path, Path, Path, Path]:
    """Like _build_two_snapshot_pair but with two differing symbols in separate files."""
    svc = tmpdir / "svc_two"
    svc.mkdir()
    (svc / "__init__.py").write_text("", encoding="utf-8")

    base_a = "def func_a(x):\n    if x is None:\n        raise ValueError\n    return x\n"
    head_a = "def func_a(x):\n    return x\n"
    base_b = "def func_b(y):\n    assert y > 0\n    return y * 2\n"
    head_b = "def func_b(y):\n    return y * 2\n"

    (svc / "module_a.py").write_text(base_a, encoding="utf-8")
    (svc / "module_b.py").write_text(base_b, encoding="utf-8")
    out_base = tmpdir / "kg_base_two"
    build_kg(svc, out_base, tenant_id=TENANT)

    (svc / "module_a.py").write_text(head_a, encoding="utf-8")
    (svc / "module_b.py").write_text(head_b, encoding="utf-8")
    out_head = tmpdir / "kg_head_two"
    build_kg(svc, out_head, tenant_id=TENANT)

    base_checkout = tmpdir / "checkout_base_two"
    base_checkout.mkdir()
    (base_checkout / "module_a.py").write_text(base_a, encoding="utf-8")
    (base_checkout / "module_b.py").write_text(base_b, encoding="utf-8")
    head_checkout = tmpdir / "checkout_head_two"
    head_checkout.mkdir()
    (head_checkout / "module_a.py").write_text(head_a, encoding="utf-8")
    (head_checkout / "module_b.py").write_text(head_b, encoding="utf-8")

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

def _call_tool_with_fake_client(
    fake_client: _FakeClient,
    head_kg: KgSnapshot,
    arguments: dict,
) -> dict:
    """Call call_tool routing through the REAL _splice_semantic_diff_hypotheses with a fake client.

    Patches _splice_semantic_diff_hypotheses with a wrapper that passes _client=fake_client,
    so the real splice logic runs but uses the fake client instead of a live LLM.
    """
    from unittest.mock import patch
    from source.kg.product.mcp_tools import _splice_semantic_diff_hypotheses as _real_splice

    def _with_fake(**kw: object) -> tuple:
        return _real_splice(**kw, _client=fake_client)  # type: ignore[arg-type]

    with patch(
        "source.kg.product.mcp_tools._splice_semantic_diff_hypotheses",
        side_effect=lambda **kw: _with_fake(**kw),
    ):
        return call_tool(head_kg, "review_context", arguments)


class TestSemanticDiffEndToEnd(unittest.TestCase):
    """Mocked client: end-to-end call_tool on real two-commit fixture pair.

    All tests route through the REAL _splice_semantic_diff_hypotheses via the _client seam.
    No bypass helpers — real splice logic is exercised in every test.
    """

    def test_real_splice_calls_fake_client(self) -> None:
        """Real _splice_semantic_diff_hypotheses exercises fake client (call_count >= 1).

        Inversion evidence: call_count >= 1 proves the real splice path ran, not a bypass.
        Also asserts >=1 contract_semantic_diff hypothesis in the result.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            out_base, out_head, base_checkout, head_checkout = _build_two_snapshot_pair(root)
            head_kg = KgSnapshot(out_head)

            # Hard assert: fixture KG must have CodeSymbol entities for the splice to exercise
            self.assertTrue(
                any(e.get("kind") == "CodeSymbol" for e in head_kg.entities),
                "fixture KG must have CodeSymbol entities — check build_kg output",
            )

            fake_client = _FakeClient()
            result = _call_tool_with_fake_client(fake_client, head_kg, {
                "repo": _SVC2_REPO,
                "changed_files": ["handler.py"],
                # No changed_ranges: avoids compact-unanchored path stripping hypotheses.
                "base_snapshot": str(out_base),
                "base_checkout": str(base_checkout),
                "head_checkout": str(head_checkout),
            })

            # Inversion evidence: the real splice called the fake client
            self.assertGreaterEqual(
                fake_client.call_count, 1,
                f"real splice must call LLM client; call_count={fake_client.call_count}",
            )
            # At least 1 semantic hypothesis in result
            hyps = result.get("review_hypotheses") or []
            semantic_hyps = [h for h in hyps if h.get("risk_type") == "contract_semantic_diff"]
            self.assertTrue(
                semantic_hyps,
                f"expected >=1 contract_semantic_diff hypothesis via real splice; "
                f"got risk_types={[h.get('risk_type') for h in hyps]}",
            )

    def test_semantic_diff_hypothesis_in_result(self) -> None:
        """contract_semantic_diff hypothesis present with derivation=inferred_llm.

        Note: changed_ranges deliberately omitted here. Passing changed_ranges with no
        matching symbols triggers the compact-unanchored path, which correctly excludes
        semantic diff hypotheses (Problem E revert: only stylesheet gap rows survive that
        path, since checkouts are incompatible with the zero-anchor trigger condition).
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            out_base, out_head, base_checkout, head_checkout = _build_two_snapshot_pair(root)
            head_kg = KgSnapshot(out_head)

            # Hard assert: fixture KG must have CodeSymbol entities
            self.assertTrue(
                any(e.get("kind") == "CodeSymbol" for e in head_kg.entities),
                "fixture KG must have CodeSymbol entities — check build_kg output",
            )

            fake_client = _FakeClient()
            result = _call_tool_with_fake_client(fake_client, head_kg, {
                "repo": _SVC2_REPO,
                "changed_files": ["handler.py"],
                # No changed_ranges: avoids the zero-anchor compact-unanchored path
                # so review_hypotheses are retained in the full result.
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
            result = _call_tool_with_fake_client(fake_client, head_kg, {
                "repo": _SVC2_REPO,
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
        """All adjudication fields (negative_checks, source_checks, confidence, why) are present.

        Note: changed_ranges omitted to avoid compact-unanchored path stripping hypotheses.
        cause/consequence are now conditional on cause_line being in-range (Problem C fix),
        so only always-present adjudication fields are checked.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            out_base, out_head, base_checkout, head_checkout = _build_two_snapshot_pair(root)
            head_kg = KgSnapshot(out_head)

            fake_client = _FakeClient()
            result = _call_tool_with_fake_client(fake_client, head_kg, {
                "repo": _SVC2_REPO,
                "changed_files": ["handler.py"],
                # No changed_ranges: avoids compact-unanchored path.
                "base_snapshot": str(out_base),
                "base_checkout": str(base_checkout),
                "head_checkout": str(head_checkout),
            })

            hyps = result.get("review_hypotheses") or []
            semantic_hyps = [h for h in hyps if h.get("risk_type") == "contract_semantic_diff"]
            if not semantic_hyps:
                self.skipTest("no semantic hyps generated — fixture may have no differing bodies")
            h = semantic_hyps[0]
            # cause/consequence are conditional on cause_line being in-range (Problem C fix).
            for field in ("negative_checks", "source_checks", "confidence", "why"):
                self.assertIn(field, h, f"field {field!r} must be present; keys={list(h.keys())}")


# ---------------------------------------------------------------------------
# 2. Failure handling
# ---------------------------------------------------------------------------

class TestSemanticDiffFailures(unittest.TestCase):
    """Failure scenarios: malformed output, missing litellm, no checkouts."""

    def test_malformed_output_skipped_no_crash(self) -> None:
        """Malformed LLM response → semantic hyps skipped, no exception."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            out_base, out_head, base_checkout, head_checkout = _build_two_snapshot_pair(root)
            head_kg = KgSnapshot(out_head)

            malformed_client = _FakeClient(return_malformed=True)
            result = _call_tool_with_fake_client(malformed_client, head_kg, {
                "repo": _SVC2_REPO,
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
            # Status should be active or absent (not "failed")
            rqs = result.get("review_quality_status") or {}
            self.assertIn(rqs.get("semantic_diff_status"), (None, "active"))

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
        """ImportError from semantic_llm import → semantic_diff_status starts with 'unavailable'."""
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
            self.assertTrue(
                status.startswith("unavailable"),
                f"status must start with 'unavailable' on ImportError; got {status!r}",
            )

    def test_all_llm_calls_fail_produces_llm_error_status(self) -> None:
        """All LLM calls failing → status == 'llm_error' (exact)."""
        from source.kg.product.mcp_tools import _splice_semantic_diff_hypotheses

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            out_base, out_head, base_checkout, head_checkout = _build_two_snapshot_pair(root)
            head_kg = KgSnapshot(out_head)

            failing_client = _FailingClient(RuntimeError("timeout"))

            # changed_symbols must carry top-level qualname so _splice can match head_entities
            changed_symbols = [{"qualname": "process"}]

            _, status = _splice_semantic_diff_hypotheses(
                base_snapshot_dir=str(out_base),
                head_kg=head_kg,
                base_checkout=str(base_checkout),
                head_checkout=str(head_checkout),
                changed_symbols=changed_symbols,
                review_hypotheses=[],
                _client=failing_client,
            )
            self.assertEqual(status, "llm_error", f"expected 'llm_error'; got {status!r}")

    def test_auth_failure_produces_no_api_key_status(self) -> None:
        """Auth exception → status == 'no_api_key' (exact)."""
        from source.kg.product.mcp_tools import _splice_semantic_diff_hypotheses

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            out_base, out_head, base_checkout, head_checkout = _build_two_snapshot_pair(root)
            head_kg = KgSnapshot(out_head)

            auth_client = _FailingClient(Exception("401 Unauthorized"))

            # changed_symbols must carry top-level qualname so _splice can match head_entities
            changed_symbols = [{"qualname": "process"}]

            _, status = _splice_semantic_diff_hypotheses(
                base_snapshot_dir=str(out_base),
                head_kg=head_kg,
                base_checkout=str(base_checkout),
                head_checkout=str(head_checkout),
                changed_symbols=changed_symbols,
                review_hypotheses=[],
                _client=auth_client,
            )
            self.assertEqual(status, "no_api_key", f"expected 'no_api_key'; got {status!r}")

    def test_partial_failure_produces_partial_status(self) -> None:
        """First call succeeds, subsequent calls fail → status == 'partial' (exact)."""
        from source.kg.product.mcp_tools import _splice_semantic_diff_hypotheses

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            out_base, out_head, base_checkout, head_checkout = _build_two_symbol_snapshot_pair(root)
            head_kg = KgSnapshot(out_head)

            partial_client = _PartialFailClient()

            # Pass both differing symbol qnames so head_entities matching finds them
            changed_symbols = [{"qualname": "func_a"}, {"qualname": "func_b"}]

            _, status = _splice_semantic_diff_hypotheses(
                base_snapshot_dir=str(out_base),
                head_kg=head_kg,
                base_checkout=str(base_checkout),
                head_checkout=str(head_checkout),
                changed_symbols=changed_symbols,
                review_hypotheses=[],
                _client=partial_client,
            )
            # Only "partial" if at least 2 differing symbols exist (>=2 LLM calls attempted)
            if partial_client.call_count >= 2:
                self.assertEqual(status, "partial", f"expected 'partial'; got {status!r}")
            else:
                self.skipTest(
                    f"fixture produced only {partial_client.call_count} differing symbols — "
                    "need >=2 for partial test"
                )


# ---------------------------------------------------------------------------
# 3. Cost bounds
# ---------------------------------------------------------------------------

class TestSemanticDiffCostBounds(unittest.TestCase):
    """Cost-bound enforcement: max 12 LLM calls; identical bodies skipped."""

    def test_max_12_llm_calls_for_more_than_12_changed_symbols(self) -> None:
        """When >12 changed symbols are provided, the LLM is called exactly 12 times."""
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
            rows, _status = semantic_contract_diff(
                base_snapshot=snap,
                head_snapshot=snap,
                base_root=base_dir,
                head_root=head_dir,
                changed_symbols=entity_dicts,
                client=counting_client,
            )
            self.assertEqual(
                counting_client.call_count, 12,
                f"LLM must be called exactly 12 times; was called {counting_client.call_count} times",
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
            _rows, _status = semantic_contract_diff(
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
            _rows, _status = semantic_contract_diff(
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
# 4. String clamping tests
# ---------------------------------------------------------------------------

class TestSemanticDiffStringClamping(unittest.TestCase):
    """String clamping: oversized fields are clamped or items are dropped."""

    def _make_single_symbol_snap(self, root: Path) -> tuple[KgSnapshot, Path, Path]:
        """Return (snap, base_dir, head_dir) for a single differing symbol."""
        from source.kg.core.models import Entity
        from source.kg.core.store import JsonlKgStore

        e = Entity(
            kind="CodeSymbol",
            identity={
                "tenant_id": TENANT,
                "repo": "repo_clamp",
                "module": "mod.handler",
                "qualname": "clamp_func",
                "symbol_kind": "function",
            },
            properties={"path": "handler.py", "line": 1, "end_line": 3},
        )
        snap_dir = root / "snap_clamp"
        JsonlKgStore(snap_dir).write(
            entities=[e], facts=[], evidence=[], coverage=[],
            manifest={"version": 1, "tenant_id": TENANT},
        )
        snap = KgSnapshot(snap_dir)

        base_dir = root / "base_clamp"
        base_dir.mkdir()
        (base_dir / "handler.py").write_text("def clamp_func():\n    if x: raise\n    return 42\n")
        head_dir = root / "head_clamp"
        head_dir.mkdir()
        (head_dir / "handler.py").write_text("def clamp_func():\n    return 42\n")

        return snap, base_dir, head_dir

    def test_oversized_claim_clamped_to_300(self) -> None:
        """Claim of length 500 is clamped to 300 in the output row."""
        from source.kg.query.semantic_contract_diff import semantic_contract_diff

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snap, base_dir, head_dir = self._make_single_symbol_snap(root)
            entity_dicts = [d for d in snap.entities if d.get("kind") == "CodeSymbol"]

            oversized_claim = "x" * 500
            client = _FakeClient(response=[{
                "claim": oversized_claim,
                "cause_line": 2,
                "consequence": "Some consequence.",
                "negative_check": "No check.",
                "category": "guard_removal",
            }])

            rows, status = semantic_contract_diff(
                base_snapshot=snap,
                head_snapshot=snap,
                base_root=base_dir,
                head_root=head_dir,
                changed_symbols=entity_dicts,
                client=client,
            )
            self.assertTrue(rows, "expected at least one row")
            claim_in_row = rows[0].get("postable_claim", "")
            self.assertLessEqual(
                len(claim_in_row), 300,
                f"postable_claim length must be <=300; got {len(claim_in_row)}",
            )

    def test_garbage_item_4x_dropped(self) -> None:
        """Item with claim of length 1500 (>1200 4x limit) is dropped entirely."""
        from source.kg.query.semantic_contract_diff import semantic_contract_diff

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snap, base_dir, head_dir = self._make_single_symbol_snap(root)
            entity_dicts = [d for d in snap.entities if d.get("kind") == "CodeSymbol"]

            garbage_claim = "x" * 1500
            client = _FakeClient(response=[{
                "claim": garbage_claim,
                "cause_line": 2,
                "consequence": "Some consequence.",
                "negative_check": "No check.",
                "category": "guard_removal",
            }])

            rows, status = semantic_contract_diff(
                base_snapshot=snap,
                head_snapshot=snap,
                base_root=base_dir,
                head_root=head_dir,
                changed_symbols=entity_dicts,
                client=client,
            )
            self.assertEqual(len(rows), 0, f"garbage item (claim>1200) must be dropped; got {len(rows)} rows")

    def test_adversarial_50kb_claim_through_real_splice_and_budget(self) -> None:
        """Regression: adversarial 50KB claim through REAL splice + budget → packet <= 15,000.

        Two-item response:
          item A: claim = 50,000 chars → exceeds _DROP_CLAIM (1200) → DROPPED entirely
          item B: claim = 500 chars   → exceeds _CLAMP_CLAIM (300) → clamped to exactly 300

        Asserts:
          (a) canonical_json(packet) size <= 15,000
          (b) no hypothesis has postable_claim longer than 300
          (c) item A produced NO row (50KB > _DROP_CLAIM → dropped)
          (d) item B produced a row with postable_claim of exactly 300 chars (clamped)
        """
        from source.kg.core.models import canonical_json

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            out_base, out_head, base_checkout, head_checkout = _build_two_snapshot_pair(root)
            head_kg = KgSnapshot(out_head)

            self.assertTrue(
                any(e.get("kind") == "CodeSymbol" for e in head_kg.entities),
                "fixture KG must have CodeSymbol entities — check build_kg output",
            )

            # item A: 50KB claim → must be DROPPED (> _DROP_CLAIM = 1200)
            claim_50kb = "x" * 50_000
            # item B: 500-char claim → above 300 clamp, below 1200 drop → CLAMPED to 300
            claim_500 = "y" * 500

            two_item_client = _FakeClient(response=[
                {
                    "claim": claim_50kb,
                    "cause_line": 2,
                    "consequence": "Consequence for 50KB item.",
                    "negative_check": "No check.",
                    "category": "guard_removal",
                },
                {
                    "claim": claim_500,
                    "cause_line": 3,
                    "consequence": "Consequence for 500-char item.",
                    "negative_check": "No check.",
                    "category": "guard_removal",
                },
            ])

            result = _call_tool_with_fake_client(two_item_client, head_kg, {
                "repo": _SVC2_REPO,
                "changed_files": ["handler.py"],
                "base_snapshot": str(out_base),
                "base_checkout": str(base_checkout),
                "head_checkout": str(head_checkout),
            })

            # (a) Total packet size <= 15,000
            packet_json = canonical_json(result)
            packet_size = len(packet_json)
            self.assertLessEqual(
                packet_size, 15_000,
                f"canonical_json packet size {packet_size} exceeds 15,000",
            )

            hyps = result.get("review_hypotheses") or []
            semantic_hyps = [h for h in hyps if h.get("risk_type") == "contract_semantic_diff"]

            # (b) No hypothesis has postable_claim longer than 300
            for h in semantic_hyps:
                claim_len = len(h.get("postable_claim", ""))
                self.assertLessEqual(
                    claim_len, 300,
                    f"postable_claim length {claim_len} exceeds 300; claim={h.get('postable_claim', '')[:60]!r}",
                )

            # (c) 50KB item produced NO row (dropped because claim > _DROP_CLAIM = 1200)
            fifty_kb_rows = [
                h for h in semantic_hyps
                if h.get("postable_claim", "").startswith("x" * 50)
            ]
            self.assertEqual(
                len(fifty_kb_rows), 0,
                f"50KB claim item must be dropped; found {len(fifty_kb_rows)} rows with x-prefix",
            )

            # (d) 500-char item → postable_claim exactly 300 chars (clamped)
            clamped_rows = [
                h for h in semantic_hyps
                if h.get("postable_claim", "").startswith("y")
            ]
            self.assertEqual(
                len(clamped_rows), 1,
                f"500-char claim item must produce exactly 1 row; got {len(clamped_rows)}",
            )
            self.assertEqual(
                len(clamped_rows[0].get("postable_claim", "")), 300,
                f"clamped postable_claim must be exactly 300 chars; "
                f"got {len(clamped_rows[0].get('postable_claim', ''))}",
            )


# ---------------------------------------------------------------------------
# 8. Claim dedupe: first wins, different claims → 2 rows
# ---------------------------------------------------------------------------

class TestSemanticDiffClaimDedupe(unittest.TestCase):
    """Problem D: per-symbol claim dedupe — first item wins on duplicate claim text."""

    def _make_single_symbol_snap_dedupe(self, root: Path) -> tuple[KgSnapshot, Path, Path]:
        """Return (snap, base_dir, head_dir) for a single differing symbol."""
        from source.kg.core.models import Entity
        from source.kg.core.store import JsonlKgStore

        e = Entity(
            kind="CodeSymbol",
            identity={
                "tenant_id": TENANT,
                "repo": "repo_dedupe",
                "module": "mod.handler",
                "qualname": "dedupe_func",
                "symbol_kind": "function",
            },
            properties={"path": "handler.py", "line": 1, "end_line": 4},
        )
        snap_dir = root / "snap_dedupe"
        JsonlKgStore(snap_dir).write(
            entities=[e], facts=[], evidence=[], coverage=[],
            manifest={"version": 1, "tenant_id": TENANT},
        )
        snap = KgSnapshot(snap_dir)

        base_dir = root / "base_dedupe"
        base_dir.mkdir()
        (base_dir / "handler.py").write_text("def dedupe_func():\n    if x: raise\n    return 42\n")
        head_dir = root / "head_dedupe"
        head_dir.mkdir()
        (head_dir / "handler.py").write_text("def dedupe_func():\n    return 42\n")

        return snap, base_dir, head_dir

    def test_identical_claims_dedupe_to_one_row_first_category_wins(self) -> None:
        """Two items with IDENTICAL claim text → exactly 1 row; category = first item's category."""
        from source.kg.query.semantic_contract_diff import semantic_contract_diff

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snap, base_dir, head_dir = self._make_single_symbol_snap_dedupe(root)
            entity_dicts = [d for d in snap.entities if d.get("kind") == "CodeSymbol"]

            shared_claim = "Guard removed from dedupe_func."
            client = _FakeClient(response=[
                {
                    "claim": shared_claim,
                    "cause_line": 2,
                    "consequence": "Callers may pass None.",
                    "negative_check": "No check A.",
                    "category": "guard_removal",   # first item's category — must win
                },
                {
                    "claim": shared_claim,          # IDENTICAL claim text
                    "cause_line": 3,
                    "consequence": "Data loss possible.",
                    "negative_check": "No check B.",
                    "category": "ownership_moved",  # second item's category — must be dropped
                },
            ])

            rows, _status = semantic_contract_diff(
                base_snapshot=snap,
                head_snapshot=snap,
                base_root=base_dir,
                head_root=head_dir,
                changed_symbols=entity_dicts,
                client=client,
            )

            self.assertEqual(
                len(rows), 1,
                f"identical claims must produce exactly 1 row; got {len(rows)}",
            )
            self.assertEqual(
                rows[0].get("category"), "guard_removal",
                f"first item's category must win; got {rows[0].get('category')!r}",
            )

    def test_different_claims_produce_two_rows(self) -> None:
        """Inversion: two items with DIFFERENT claim texts → 2 rows (up to _MAX_HYPS_PER_SYMBOL=2)."""
        from source.kg.query.semantic_contract_diff import semantic_contract_diff

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snap, base_dir, head_dir = self._make_single_symbol_snap_dedupe(root)
            entity_dicts = [d for d in snap.entities if d.get("kind") == "CodeSymbol"]

            client = _FakeClient(response=[
                {
                    "claim": "Guard removed from dedupe_func.",
                    "cause_line": 2,
                    "consequence": "Callers may pass None.",
                    "negative_check": "No check.",
                    "category": "guard_removal",
                },
                {
                    "claim": "Return type widened to include None.",  # different claim
                    "cause_line": 3,
                    "consequence": "Callers expecting non-None may crash.",
                    "negative_check": "No check.",
                    "category": "return_type_widened",
                },
            ])

            rows, _status = semantic_contract_diff(
                base_snapshot=snap,
                head_snapshot=snap,
                base_root=base_dir,
                head_root=head_dir,
                changed_symbols=entity_dicts,
                client=client,
            )

            self.assertEqual(
                len(rows), 2,
                f"different claims must produce exactly 2 rows (_MAX_HYPS_PER_SYMBOL=2); got {len(rows)}",
            )


# ---------------------------------------------------------------------------
# 5. Prefilter-before-cap tests
# ---------------------------------------------------------------------------

class TestSemanticDiffPrefilterBeforeCap(unittest.TestCase):
    """Prefilter runs BEFORE cap: identical-body symbols never count against the 12-slot budget."""

    def test_differing_symbols_analyzed_when_identical_padded(self) -> None:
        """12 identical + 3 differing → exactly 3 LLM calls (not 0 after cap eats all slots)."""
        from source.kg.query.semantic_contract_diff import semantic_contract_diff
        from source.kg.core.models import Entity
        from source.kg.core.store import JsonlKgStore

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            entity_objects = []
            # 12 identical symbols (same body in base and head)
            for i in range(12):
                e = Entity(
                    kind="CodeSymbol",
                    identity={
                        "tenant_id": TENANT,
                        "repo": "svc_prefilter",
                        "module": "svc_prefilter.handler",
                        "qualname": f"same_func_{i}",
                        "symbol_kind": "function",
                    },
                    properties={"path": f"same_{i}.py", "line": 1, "end_line": 3},
                )
                entity_objects.append(e)

            # 3 differing symbols (base and head body differ)
            for i in range(3):
                e = Entity(
                    kind="CodeSymbol",
                    identity={
                        "tenant_id": TENANT,
                        "repo": "svc_prefilter",
                        "module": "svc_prefilter.handler",
                        "qualname": f"diff_func_{i}",
                        "symbol_kind": "function",
                    },
                    properties={"path": f"diff_{i}.py", "line": 1, "end_line": 4},
                )
                entity_objects.append(e)

            snap_dir = root / "snap_prefilter"
            JsonlKgStore(snap_dir).write(
                entities=entity_objects,
                facts=[],
                evidence=[],
                coverage=[],
                manifest={"version": 1, "tenant_id": TENANT},
            )
            snap = KgSnapshot(snap_dir)
            entity_dicts = snap.entities

            base_dir = root / "base_pf"
            base_dir.mkdir()
            head_dir = root / "head_pf"
            head_dir.mkdir()

            # Identical bodies for same_func_* symbols
            same_body = "def f():\n    return 42\n"
            for i in range(12):
                (base_dir / f"same_{i}.py").write_text(same_body)
                (head_dir / f"same_{i}.py").write_text(same_body)

            # Differing bodies for diff_func_* symbols
            for i in range(3):
                (base_dir / f"diff_{i}.py").write_text(f"def diff_func_{i}():\n    if x: raise\n    return {i}\n")
                (head_dir / f"diff_{i}.py").write_text(f"def diff_func_{i}():\n    return {i}\n")

            counting_client = _FakeClient()
            _rows, _status = semantic_contract_diff(
                base_snapshot=snap,
                head_snapshot=snap,
                base_root=base_dir,
                head_root=head_dir,
                changed_symbols=entity_dicts,
                client=counting_client,
            )
            self.assertEqual(
                counting_client.call_count, 3,
                f"LLM must be called exactly 3 times (only for differing symbols); "
                f"was called {counting_client.call_count} times",
            )


# ---------------------------------------------------------------------------
# 6. Problem C: cause_line upper clamp (FW2)
# ---------------------------------------------------------------------------

class TestCauseLineUpperClamp(unittest.TestCase):
    """Problem C: body-derived upper bound for cause_line when end_line is None."""

    def _make_single_symbol_snap_no_end_line(self, root: Path) -> tuple[KgSnapshot, Path, Path]:
        """Symbol with line=5, end_line=None, body of 10 lines."""
        from source.kg.core.models import Entity
        from source.kg.core.store import JsonlKgStore

        e = Entity(
            kind="CodeSymbol",
            identity={
                "tenant_id": TENANT,
                "repo": "repo_clamp_ub",
                "module": "mod.handler",
                "qualname": "ub_func",
                "symbol_kind": "function",
            },
            # end_line intentionally absent → None in KG
            properties={"path": "handler.py", "line": 5},
        )
        snap_dir = root / "snap_clamp_ub"
        JsonlKgStore(snap_dir).write(
            entities=[e], facts=[], evidence=[], coverage=[],
            manifest={"version": 1, "tenant_id": TENANT},
        )
        snap = KgSnapshot(snap_dir)

        # Body: 10 lines starting at line 5 → valid range is [5, 14]
        body_10_lines = "\n".join(f"    line_{i} = {i}" for i in range(10)) + "\n"
        base_dir = root / "base_clamp_ub"
        base_dir.mkdir()
        (base_dir / "handler.py").write_text("# padding\n" * 4 + f"def ub_func():\n{body_10_lines}")
        head_dir = root / "head_clamp_ub"
        head_dir.mkdir()
        (head_dir / "handler.py").write_text("# padding\n" * 4 + f"def ub_func():\n    return 0\n")

        return snap, base_dir, head_dir

    def test_cause_line_999999_clamped_to_body_span(self) -> None:
        """LLM returns cause_line=999999 → cause dict has line_start <= symbol line + body line count."""
        from source.kg.query.semantic_contract_diff import semantic_contract_diff

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snap, base_dir, head_dir = self._make_single_symbol_snap_no_end_line(root)
            entity_dicts = [d for d in snap.entities if d.get("kind") == "CodeSymbol"]

            client = _FakeClient(response=[{
                "claim": "Function contract changed.",
                "cause_line": 999999,  # Out of range — must be clamped
                "consequence": "Some consequence.",
                "negative_check": "No check.",
                "category": "guard_removal",
            }])

            rows, status = semantic_contract_diff(
                base_snapshot=snap,
                head_snapshot=snap,
                base_root=base_dir,
                head_root=head_dir,
                changed_symbols=entity_dicts,
                client=client,
            )
            self.assertTrue(rows, "expected at least one row")
            row = rows[0]
            # With cause_line=999999 clamped to body span, cause dict should be absent
            # (out of file range after clamping). Check source_spans still present.
            self.assertIn("source_spans", row, "source_spans must be present regardless of clamp")
            # If cause is present, its line_start must be within [5, 5 + 10 body lines]
            if "cause" in row:
                line_start = row["cause"].get("line_start", 0)
                self.assertLessEqual(
                    line_start, 15,
                    f"cause.line_start must be <= 15 (line 5 + 10 body lines); got {line_start}",
                )

    def test_cause_line_in_range_produces_cause_dict(self) -> None:
        """cause_line within symbol span → cause dict present."""
        from source.kg.query.semantic_contract_diff import semantic_contract_diff

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snap, base_dir, head_dir = self._make_single_symbol_snap_no_end_line(root)
            entity_dicts = [d for d in snap.entities if d.get("kind") == "CodeSymbol"]

            client = _FakeClient(response=[{
                "claim": "Function contract changed.",
                "cause_line": 7,  # Within range [5, 14]
                "consequence": "Some consequence.",
                "negative_check": "No check.",
                "category": "guard_removal",
            }])

            rows, status = semantic_contract_diff(
                base_snapshot=snap,
                head_snapshot=snap,
                base_root=base_dir,
                head_root=head_dir,
                changed_symbols=entity_dicts,
                client=client,
            )
            self.assertTrue(rows, "expected at least one row")
            row = rows[0]
            self.assertIn("cause", row, "cause dict must be present for in-range cause_line")
            self.assertLessEqual(row["cause"]["line_start"], 14)


# ---------------------------------------------------------------------------
# 7. Problem B: derivation survives compaction (FW2)
# ---------------------------------------------------------------------------

class TestDerivationSurvivesCompact(unittest.TestCase):
    """Problem B: derivation=inferred_llm must survive _compact_review_hypothesis and _slim_mirror_hypothesis."""

    def test_compact_review_hypothesis_keeps_derivation(self) -> None:
        """_compact_review_hypothesis preserves derivation=inferred_llm."""
        from source.kg.product import output_budget as ob

        row = {
            "hypothesis_id": "hyp-001",
            "label": "H001",
            "risk_type": "contract_semantic_diff",
            "specificity": "high",
            "confidence": "medium",
            "why": "test why",
            "concrete_invariant": "test invariant",
            "postable_claim": "test claim",
            "derivation": "inferred_llm",
            "cause": {"path": "src/a.py", "line_start": 5},
            "consequence": {"path": "src/a.py", "line_start": 5},
            "evidence_refs": [],
            "source_spans": [],
            "source_checks": [],
            "negative_checks": [],
            "supporting_lead_ids": [],
        }
        compact = ob._compact_review_hypothesis(row)
        self.assertEqual(
            compact.get("derivation"), "inferred_llm",
            f"derivation must survive _compact_review_hypothesis; got {compact.get('derivation')}",
        )

    def test_slim_mirror_hypothesis_keeps_derivation(self) -> None:
        """_slim_mirror_hypothesis preserves derivation=inferred_llm."""
        from source.kg.product import output_budget as ob

        row = {
            "hypothesis_id": "hyp-002",
            "label": "H002",
            "risk_type": "contract_semantic_diff",
            "specificity": "high",
            "confidence": "medium",
            "postable_claim": "test claim",
            "derivation": "inferred_llm",
        }
        slim = ob._slim_mirror_hypothesis(row)
        self.assertEqual(
            slim.get("derivation"), "inferred_llm",
            f"derivation must survive _slim_mirror_hypothesis; got {slim.get('derivation')}",
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
            rows, status = semantic_contract_diff(
                base_snapshot=snap,
                head_snapshot=snap,
                base_root=base_dir,
                head_root=head_dir,
                changed_symbols=[e],
                client=client,
            )
            # Shape-only assertions
            self.assertIsInstance(rows, list, "result must be a list")
            self.assertIsInstance(status, str, "status must be a string")
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


# ---------------------------------------------------------------------------
# P1: Real SemanticDiffLlmClient with patched litellm — typed result
# ---------------------------------------------------------------------------

class TestRealClientTypedResult(unittest.TestCase):
    """P1 fix: real SemanticDiffLlmClient with litellm.completion patched at the import site.

    Tests exercise the REAL client path (not a fake client) so that the typed
    LlmResult mapping is verified end-to-end through semantic_contract_diff.
    litellm may not be installed in this venv — we inject a fake litellm module
    via sys.modules so the lazy import inside complete_json always succeeds.
    """

    def _make_single_symbol_snap(self, root: Path) -> tuple[KgSnapshot, Path, Path]:
        from source.kg.core.models import Entity
        from source.kg.core.store import JsonlKgStore

        e = Entity(
            kind="CodeSymbol",
            identity={
                "tenant_id": "default",
                "repo": "real_client_repo",
                "module": "mod.handler",
                "qualname": "real_func",
                "symbol_kind": "function",
            },
            properties={"path": "handler.py", "line": 1, "end_line": 3},
        )
        snap_dir = root / "snap_real"
        JsonlKgStore(snap_dir).write(
            entities=[e], facts=[], evidence=[], coverage=[],
            manifest={"version": 1, "tenant_id": "default"},
        )
        snap = KgSnapshot(snap_dir)
        base_dir = root / "base_real"
        base_dir.mkdir()
        (base_dir / "handler.py").write_text("def real_func():\n    if x: raise\n    return 42\n")
        head_dir = root / "head_real"
        head_dir.mkdir()
        (head_dir / "handler.py").write_text("def real_func():\n    return 42\n")
        return snap, base_dir, head_dir

    def _inject_fake_litellm(self, completion_side_effect) -> Any:
        """Return a fake litellm module whose .completion raises or returns per side_effect."""
        import types
        fake = types.ModuleType("litellm")

        def fake_completion(**kwargs):
            return completion_side_effect(**kwargs)

        fake.completion = fake_completion
        return fake

    def test_auth_error_produces_no_api_key_via_real_client(self) -> None:
        """Real client: litellm.completion raises 401 → LlmResult.kind == 'no_api_key'.

        Verified through semantic_contract_diff: status must be 'no_api_key', not 'active'.
        """
        import sys
        from unittest.mock import patch
        from source.kg.integrations.semantic_llm import SemanticDiffLlmClient
        from source.kg.query.semantic_contract_diff import semantic_contract_diff

        def _raise_auth(**kwargs):
            raise Exception("401 Unauthorized: invalid API key")

        fake_litellm = self._inject_fake_litellm(_raise_auth)
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snap, base_dir, head_dir = self._make_single_symbol_snap(root)
            entity_dicts = [d for d in snap.entities if d.get("kind") == "CodeSymbol"]

            with patch.dict(sys.modules, {"litellm": fake_litellm}):
                client = SemanticDiffLlmClient(model="fake-model")
                rows, status = semantic_contract_diff(
                    base_snapshot=snap,
                    head_snapshot=snap,
                    base_root=base_dir,
                    head_root=head_dir,
                    changed_symbols=entity_dicts,
                    client=client,
                )
        self.assertEqual(
            status, "no_api_key",
            f"auth failure via real client must produce 'no_api_key'; got {status!r}",
        )
        self.assertEqual(rows, [], "no rows must be produced on auth failure")

    def test_timeout_produces_llm_error_via_real_client(self) -> None:
        """Real client: litellm.completion raises RuntimeError('timeout') → status 'llm_error'.

        Verified through semantic_contract_diff: status must be 'llm_error', not 'active'.
        """
        import sys
        from unittest.mock import patch
        from source.kg.integrations.semantic_llm import SemanticDiffLlmClient
        from source.kg.query.semantic_contract_diff import semantic_contract_diff

        def _raise_timeout(**kwargs):
            raise RuntimeError("timeout")

        fake_litellm = self._inject_fake_litellm(_raise_timeout)
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snap, base_dir, head_dir = self._make_single_symbol_snap(root)
            entity_dicts = [d for d in snap.entities if d.get("kind") == "CodeSymbol"]

            with patch.dict(sys.modules, {"litellm": fake_litellm}):
                client = SemanticDiffLlmClient(model="fake-model")
                rows, status = semantic_contract_diff(
                    base_snapshot=snap,
                    head_snapshot=snap,
                    base_root=base_dir,
                    head_root=head_dir,
                    changed_symbols=entity_dicts,
                    client=client,
                )
        self.assertEqual(
            status, "llm_error",
            f"timeout via real client must produce 'llm_error'; got {status!r}",
        )
        self.assertEqual(rows, [], "no rows must be produced on llm_error")

    def test_absent_litellm_propagates_import_error(self) -> None:
        """Real client: litellm absent (ImportError) → propagated through semantic_contract_diff.

        The ImportError must propagate so _splice catches it as 'unavailable'.
        semantic_contract_diff does not swallow it.
        """
        import sys
        from unittest.mock import patch
        from source.kg.integrations.semantic_llm import SemanticDiffLlmClient
        from source.kg.query.semantic_contract_diff import semantic_contract_diff

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snap, base_dir, head_dir = self._make_single_symbol_snap(root)
            entity_dicts = [d for d in snap.entities if d.get("kind") == "CodeSymbol"]

            with patch.dict(sys.modules, {"litellm": None}):
                client = SemanticDiffLlmClient(model="fake-model")
                with self.assertRaises(ImportError):
                    semantic_contract_diff(
                        base_snapshot=snap,
                        head_snapshot=snap,
                        base_root=base_dir,
                        head_root=head_dir,
                        changed_symbols=entity_dicts,
                        client=client,
                    )


# ---------------------------------------------------------------------------
# P2: Composite (path, qualname) matching — duplicate qualname in different files
# ---------------------------------------------------------------------------

class TestSpliceDuplicateQualname(unittest.TestCase):
    """P2 fix: _splice_semantic_diff_hypotheses matches by (path, qualname) not qualname alone.

    Two CodeSymbol entities share the same qualname in different files.
    Only one file is in the changed_symbols list (with path).
    Assert: fake client called exactly once, for the correct file.
    """

    def test_duplicate_qualname_only_changed_file_analyzed(self) -> None:
        from source.kg.product.mcp_tools import _splice_semantic_diff_hypotheses
        from source.kg.core.models import Entity
        from source.kg.core.store import JsonlKgStore
        from source.kg.query.snapshot import KgSnapshot

        SHARED_QNAME = "process"

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            # Two entities: same qualname, different files
            entity_a = Entity(
                kind="CodeSymbol",
                identity={
                    "tenant_id": "default",
                    "repo": "dup_repo",
                    "module": "mod_a",
                    "qualname": SHARED_QNAME,
                    "symbol_kind": "function",
                },
                properties={"path": "pkg/a.py", "line": 1, "end_line": 4},
            )
            entity_b = Entity(
                kind="CodeSymbol",
                identity={
                    "tenant_id": "default",
                    "repo": "dup_repo",
                    "module": "mod_b",
                    "qualname": SHARED_QNAME,
                    "symbol_kind": "function",
                },
                properties={"path": "pkg/b.py", "line": 1, "end_line": 4},
            )

            snap_dir = root / "snap_dup"
            JsonlKgStore(snap_dir).write(
                entities=[entity_a, entity_b],
                facts=[], evidence=[], coverage=[],
                manifest={"version": 1, "tenant_id": "default"},
            )
            head_kg = KgSnapshot(snap_dir)

            base_dir = root / "base_dup"
            base_dir.mkdir()
            (base_dir / "pkg").mkdir()
            (base_dir / "pkg" / "a.py").write_text(
                "def process():\n    if x: raise\n    return 1\n"
            )
            (base_dir / "pkg" / "b.py").write_text(
                "def process():\n    if y: raise\n    return 2\n"
            )
            head_dir = root / "head_dup"
            head_dir.mkdir()
            (head_dir / "pkg").mkdir()
            (head_dir / "pkg" / "a.py").write_text("def process():\n    return 1\n")
            (head_dir / "pkg" / "b.py").write_text("def process():\n    return 2\n")

            # Only pkg/a.py is in the changed_symbols (with path)
            changed_symbols = [{"qualname": SHARED_QNAME, "path": "pkg/a.py"}]

            prompt_log: list[str] = []

            class _LoggingClient:
                def complete_json(self, prompt: str) -> LlmResult:
                    prompt_log.append(prompt)
                    return LlmResult.parsed([{
                        "claim": "Guard removed.",
                        "cause_line": 2,
                        "consequence": "Callers may pass None.",
                        "negative_check": "No check.",
                        "category": "guard_removal",
                    }])

            _, status = _splice_semantic_diff_hypotheses(
                base_snapshot_dir=str(snap_dir),
                head_kg=head_kg,
                base_checkout=str(base_dir),
                head_checkout=str(head_dir),
                changed_symbols=changed_symbols,
                review_hypotheses=[],
                _client=_LoggingClient(),
            )

            # Exactly one call — only pkg/a.py analyzed, not pkg/b.py
            self.assertEqual(
                len(prompt_log), 1,
                f"fake client must be called exactly once (only pkg/a.py); got {len(prompt_log)} calls",
            )
            # The prompt references pkg/a.py's qualname (SHARED_QNAME)
            self.assertIn(
                SHARED_QNAME, prompt_log[0],
                f"prompt must reference the changed symbol qualname {SHARED_QNAME!r}",
            )
            # Status reflects the call succeeded
            self.assertIn(status, ("active", "partial"),
                          f"unexpected status {status!r}")


# ---------------------------------------------------------------------------
# Fix wave: P1 — path escape prevention (_safe_resolve)
# ---------------------------------------------------------------------------

class TestSafeResolve(unittest.TestCase):
    """_safe_resolve rejects absolute paths, ../traversal, and symlinks escaping root."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)

    def tearDown(self) -> None:
        self._td.cleanup()

    def test_absolute_path_rejected(self) -> None:
        """Absolute path (/tmp/evil) is rejected — returns None, no read."""
        from source.kg.query.semantic_contract_diff import _safe_resolve

        # Create a real file outside the root so we can distinguish "exists but rejected"
        # from "does not exist".
        outside = self.root.parent / "evil_absolute.txt"
        outside.write_text("secret", encoding="utf-8")
        try:
            result = _safe_resolve(self.root, str(outside))
            self.assertIsNone(result, f"absolute path must be rejected; got {result!r}")
        finally:
            outside.unlink(missing_ok=True)

    def test_dotdot_traversal_rejected(self) -> None:
        """../escape relative path is rejected — returns None, no read."""
        from source.kg.query.semantic_contract_diff import _safe_resolve

        # ../escape would resolve to the parent of root
        result = _safe_resolve(self.root, "../escape.txt")
        self.assertIsNone(result, f"../traversal must be rejected; got {result!r}")

    def test_symlink_outside_root_rejected(self) -> None:
        """Symlink inside checkout pointing outside root is rejected by resolve()."""
        from source.kg.query.semantic_contract_diff import _safe_resolve

        outside = self.root.parent / "symlink_target.txt"
        outside.write_text("secret", encoding="utf-8")
        link = self.root / "evil_link.txt"
        try:
            link.symlink_to(outside)
            result = _safe_resolve(self.root, "evil_link.txt")
            self.assertIsNone(result, f"symlink escaping root must be rejected; got {result!r}")
        finally:
            link.unlink(missing_ok=True)
            outside.unlink(missing_ok=True)

    def test_safe_path_inside_root_accepted(self) -> None:
        """Normal relative path inside root is accepted — returns a Path."""
        from source.kg.query.semantic_contract_diff import _safe_resolve

        (self.root / "safe.py").write_text("ok", encoding="utf-8")
        result = _safe_resolve(self.root, "safe.py")
        self.assertIsNotNone(result, "safe relative path must be accepted")

    def test_read_body_does_not_read_absolute_path(self) -> None:
        """_read_body with an absolute path returns '' and the LLM is never called.

        Regression: a KG-supplied absolute path must not reach read_text.
        """
        from source.kg.query.semantic_contract_diff import _read_body

        # Use a tempfile inside a DIFFERENT tmpdir (simulates /etc/hosts-style path)
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            f.write(b"super secret contents")
            secret_path = f.name
        try:
            body = _read_body(self.root, secret_path, None, None)
            self.assertEqual(body, "", f"_read_body must return '' for absolute path; got {body[:40]!r}")
        finally:
            Path(secret_path).unlink(missing_ok=True)

    def test_read_body_does_not_read_dotdot_path(self) -> None:
        """_read_body with a ../escape path returns ''."""
        from source.kg.query.semantic_contract_diff import _read_body

        body = _read_body(self.root, "../some_secret.txt", None, None)
        self.assertEqual(body, "", f"_read_body must return '' for ../traversal; got {body[:40]!r}")


# ---------------------------------------------------------------------------
# Fix wave: P2 — mixed auth failure does not discard successful rows
# ---------------------------------------------------------------------------

class _MixedAuthClient:
    """First call returns valid JSON; second call returns LlmResult.call_failure('no_api_key')."""

    def __init__(self) -> None:
        self.call_count = 0

    def complete_json(self, prompt: str) -> "LlmResult":
        self.call_count += 1
        if self.call_count == 1:
            return LlmResult.parsed(_FAKE_RESPONSE)
        return LlmResult.call_failure("no_api_key")


class TestMixedAuthPartialPreservesRows(unittest.TestCase):
    """P2 regression: first call valid JSON, second call_failure('no_api_key') → row IS spliced, status 'partial'."""

    def test_valid_row_preserved_on_mixed_auth_failure(self) -> None:
        """One success + later auth failure → the valid row is spliced; status is 'partial'."""
        from source.kg.product.mcp_tools import _splice_semantic_diff_hypotheses

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            out_base, out_head, base_checkout, head_checkout = _build_two_symbol_snapshot_pair(root)
            head_kg = KgSnapshot(out_head)

            mixed_client = _MixedAuthClient()
            changed_symbols = [{"qualname": "func_a"}, {"qualname": "func_b"}]

            merged_rows, status = _splice_semantic_diff_hypotheses(
                base_snapshot_dir=str(out_base),
                head_kg=head_kg,
                base_checkout=str(base_checkout),
                head_checkout=str(head_checkout),
                changed_symbols=changed_symbols,
                review_hypotheses=[],
                _client=mixed_client,
            )

            if mixed_client.call_count < 2:
                self.skipTest(
                    f"fixture produced only {mixed_client.call_count} differing symbols — need >=2"
                )

            # Hard assert: the successful row must be present in the merged output
            semantic_rows = [h for h in merged_rows if h.get("risk_type") == "contract_semantic_diff"]
            self.assertGreater(
                len(semantic_rows), 0,
                f"valid row from first call must survive mixed auth failure; "
                f"got 0 semantic rows; status={status!r}",
            )

            # Status must be 'partial' (not 'no_api_key')
            self.assertEqual(
                status, "partial",
                f"mixed auth+success must yield 'partial'; got {status!r}",
            )


if __name__ == "__main__":
    unittest.main()
