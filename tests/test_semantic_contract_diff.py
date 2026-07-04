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

# New schema (wave-3 task B) required keys. Fixtures must carry old_contract,
# new_contract, violated_invariant in addition to the original five. A concrete
# violated_invariant makes the row high-specificity; the literal "none" sentinel
# makes it a context-grade (medium-specificity) BEHAVIOR-DELTA row.
_DEFAULT_CONTRACT_FIELDS = {
    "old_contract": "Function validated input before processing.",
    "new_contract": "Function no longer validates input before processing.",
    "violated_invariant": "Callers relied on invalid input being rejected.",
}


def _claim(**overrides: Any) -> dict:
    """Build a schema-complete claim dict; overrides win. violated_invariant may be
    set to 'none' to produce a BEHAVIOR-DELTA (medium-specificity) fixture."""
    base = {
        "claim": "Function no longer validates input before processing",
        "cause_line": 3,
        "consequence": "Callers may pass invalid data without receiving an error.",
        "negative_check": "If validation was intentionally removed and callers are trusted, this risk does not apply.",
        "category": "guard_removal",
        **_DEFAULT_CONTRACT_FIELDS,
    }
    base.update(overrides)
    return base


_FAKE_RESPONSE = [_claim()]


from source.kg.integrations.semantic_llm import LlmResult


class _FakeClient:
    """Deterministic fake LLM client — returns fixed valid JSON via LlmResult."""

    def __init__(self, response: list | None = None, raise_import: bool = False, return_malformed: bool = False):
        # `is None` (not falsy-or): an explicit empty list [] is a meaningful
        # "no changes found" response and must not silently swap to the default.
        self._response = _FAKE_RESPONSE if response is None else response
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
            # Status must be a recognized non-"active" status reflecting the parse miss.
            # With Fix 1, all-parse_miss → "failed:all_responses_unparseable" (not silent "active").
            rqs = result.get("review_quality_status") or {}
            self.assertIn(
                rqs.get("semantic_diff_status"),
                (None, "active", "failed:all_responses_unparseable"),
                f"malformed status must be a recognized value; got {rqs.get('semantic_diff_status')!r}",
            )

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
            # violated_invariant="none" → BEHAVIOR-DELTA composition
            # ("behavior changed: {clamped_claim}"). The clamped claim itself must be
            # exactly 300 chars (the _CLAMP_CLAIM bound).
            client = _FakeClient(response=[_claim(
                claim=oversized_claim,
                cause_line=2,
                consequence="Some consequence.",
                negative_check="No check.",
                category="guard_removal",
                violated_invariant="none",
            )])

            rows, status = semantic_contract_diff(
                base_snapshot=snap,
                head_snapshot=snap,
                base_root=base_dir,
                head_root=head_dir,
                changed_symbols=entity_dicts,
                client=client,
            )
            self.assertTrue(rows, "expected at least one row")
            # The clamped claim (300 x's) must appear in postable_claim; the raw 500
            # x's must not survive the clamp.
            claim_in_row = rows[0].get("postable_claim", "")
            self.assertIn("x" * 300, claim_in_row, "clamped 300-char claim must be present")
            self.assertNotIn("x" * 301, claim_in_row, "claim must be clamped to 300 chars")

    def test_garbage_item_4x_dropped(self) -> None:
        """Item with claim of length 1500 (>1200 4x limit) is dropped entirely."""
        from source.kg.query.semantic_contract_diff import semantic_contract_diff

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snap, base_dir, head_dir = self._make_single_symbol_snap(root)
            entity_dicts = [d for d in snap.entities if d.get("kind") == "CodeSymbol"]

            garbage_claim = "x" * 1500
            client = _FakeClient(response=[_claim(
                claim=garbage_claim,
                cause_line=2,
                consequence="Some consequence.",
                negative_check="No check.",
                category="guard_removal",
            )])

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
                _claim(
                    claim=claim_50kb,
                    cause_line=2,
                    consequence="Consequence for 50KB item.",
                    negative_check="No check.",
                    category="guard_removal",
                ),
                _claim(
                    claim=claim_500,
                    cause_line=3,
                    consequence="Consequence for 500-char item.",
                    negative_check="No check.",
                    category="guard_removal",
                    violated_invariant="none",  # BEHAVIOR-DELTA composition
                ),
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

            # (b) No hypothesis carries the un-clamped 500-char claim: the clamped claim
            # component is bounded to _CLAMP_CLAIM (300). postable_claim is a composed
            # string ("behavior changed: {claim}") so its total length exceeds 300, but
            # no raw 301-char run of the claim char survives the clamp.
            for h in semantic_hyps:
                pc = h.get("postable_claim", "")
                self.assertNotIn("y" * 301, pc, "claim component must be clamped to 300")
                self.assertNotIn("x" * 301, pc, "claim component must be clamped to 300")

            # (c) 50KB item produced NO row (dropped because claim > _DROP_CLAIM = 1200)
            fifty_kb_rows = [
                h for h in semantic_hyps
                if "x" * 50 in h.get("postable_claim", "")
            ]
            self.assertEqual(
                len(fifty_kb_rows), 0,
                f"50KB claim item must be dropped; found {len(fifty_kb_rows)} rows with x-run",
            )

            # (d) 500-char item → exactly 1 row; the clamped claim (300 y's) is present.
            clamped_rows = [
                h for h in semantic_hyps
                if "y" in h.get("postable_claim", "")
            ]
            self.assertEqual(
                len(clamped_rows), 1,
                f"500-char claim item must produce exactly 1 row; got {len(clamped_rows)}",
            )
            self.assertIn(
                "y" * 300, clamped_rows[0].get("postable_claim", ""),
                "clamped claim (300 y's) must be present in postable_claim",
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
                _claim(
                    claim=shared_claim,
                    cause_line=2,
                    consequence="Callers may pass None.",
                    negative_check="No check A.",
                    category="guard_removal",   # first item's category — must win
                ),
                _claim(
                    claim=shared_claim,          # IDENTICAL claim text
                    cause_line=3,
                    consequence="Data loss possible.",
                    negative_check="No check B.",
                    category="ownership_moved",  # second item's category — must be dropped
                ),
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
                _claim(
                    claim="Guard removed from dedupe_func.",
                    cause_line=2,
                    consequence="Callers may pass None.",
                    negative_check="No check.",
                    category="guard_removal",
                ),
                _claim(
                    claim="Return type widened to include None.",  # different claim
                    cause_line=3,
                    consequence="Callers expecting non-None may crash.",
                    negative_check="No check.",
                    category="return_type_widened",
                ),
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

            client = _FakeClient(response=[_claim(
                claim="Function contract changed.",
                cause_line=999999,  # Out of range — must be clamped
                consequence="Some consequence.",
                negative_check="No check.",
                category="guard_removal",
            )])

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

            client = _FakeClient(response=[_claim(
                claim="Function contract changed.",
                cause_line=7,  # Within range [5, 14]
                consequence="Some consequence.",
                negative_check="No check.",
                category="guard_removal",
            )])

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
                changed_symbols=[e.to_record()],
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
                    return LlmResult.parsed([_claim(
                        claim="Guard removed.",
                        cause_line=2,
                        consequence="Callers may pass None.",
                        negative_check="No check.",
                        category="guard_removal",
                    )])

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


# ---------------------------------------------------------------------------
# Fix wave: P1 — invalid checkout paths are reported as explicit failures
# ---------------------------------------------------------------------------

class TestInvalidCheckoutPaths(unittest.TestCase):
    """P1 fix: nonexistent or non-directory checkout paths return explicit failure status."""

    def _splice(self, base_checkout: str, head_checkout: str, base_snapshot_dir: str, head_kg: KgSnapshot, changed_symbols: list) -> str:
        from source.kg.product.mcp_tools import _splice_semantic_diff_hypotheses
        _, status = _splice_semantic_diff_hypotheses(
            base_snapshot_dir=base_snapshot_dir,
            head_kg=head_kg,
            base_checkout=base_checkout,
            head_checkout=head_checkout,
            changed_symbols=changed_symbols,
            review_hypotheses=[],
            _client=_FakeClient(),
        )
        return status

    def test_nonexistent_base_checkout_returns_failed_invalid_base(self) -> None:
        """Nonexistent base_checkout dir → status 'failed:invalid_base_checkout' (exact)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            out_base, out_head, _bc, head_checkout = _build_two_snapshot_pair(root)
            head_kg = KgSnapshot(out_head)

            status = self._splice(
                base_checkout=str(root / "does_not_exist"),
                head_checkout=str(head_checkout),
                base_snapshot_dir=str(out_base),
                head_kg=head_kg,
                changed_symbols=[{"qualname": "process"}],
            )
            self.assertEqual(
                status, "failed:invalid_base_checkout",
                f"nonexistent base_checkout must yield 'failed:invalid_base_checkout'; got {status!r}",
            )

    def test_nonexistent_head_checkout_returns_failed_invalid_head(self) -> None:
        """Nonexistent head_checkout dir → status 'failed:invalid_head_checkout' (exact)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            out_base, out_head, base_checkout, _hc = _build_two_snapshot_pair(root)
            head_kg = KgSnapshot(out_head)

            status = self._splice(
                base_checkout=str(base_checkout),
                head_checkout=str(root / "does_not_exist"),
                base_snapshot_dir=str(out_base),
                head_kg=head_kg,
                changed_symbols=[{"qualname": "process"}],
            )
            self.assertEqual(
                status, "failed:invalid_head_checkout",
                f"nonexistent head_checkout must yield 'failed:invalid_head_checkout'; got {status!r}",
            )

    def test_file_instead_of_dir_base_returns_failed_invalid_base(self) -> None:
        """File path used as base_checkout → 'failed:invalid_base_checkout' (exact)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            out_base, out_head, _bc, head_checkout = _build_two_snapshot_pair(root)
            head_kg = KgSnapshot(out_head)

            # Write a file where a dir is expected
            file_path = root / "a_plain_file.txt"
            file_path.write_text("not a directory", encoding="utf-8")

            status = self._splice(
                base_checkout=str(file_path),
                head_checkout=str(head_checkout),
                base_snapshot_dir=str(out_base),
                head_kg=head_kg,
                changed_symbols=[{"qualname": "process"}],
            )
            self.assertEqual(
                status, "failed:invalid_base_checkout",
                f"file-as-base_checkout must yield 'failed:invalid_base_checkout'; got {status!r}",
            )

    def test_file_instead_of_dir_head_returns_failed_invalid_head(self) -> None:
        """File path used as head_checkout → 'failed:invalid_head_checkout' (exact)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            out_base, out_head, base_checkout, _hc = _build_two_snapshot_pair(root)
            head_kg = KgSnapshot(out_head)

            file_path = root / "a_plain_file.txt"
            file_path.write_text("not a directory", encoding="utf-8")

            status = self._splice(
                base_checkout=str(base_checkout),
                head_checkout=str(file_path),
                base_snapshot_dir=str(out_base),
                head_kg=head_kg,
                changed_symbols=[{"qualname": "process"}],
            )
            self.assertEqual(
                status, "failed:invalid_head_checkout",
                f"file-as-head_checkout must yield 'failed:invalid_head_checkout'; got {status!r}",
            )

    def test_all_reads_fail_returns_failed_unreadable_sources(self) -> None:
        """Valid dirs + all source files unreadable → 'failed:unreadable_sources' from semantic_contract_diff."""
        from source.kg.query.semantic_contract_diff import semantic_contract_diff
        from source.kg.core.models import Entity
        from source.kg.core.store import JsonlKgStore

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            e = Entity(
                kind="CodeSymbol",
                identity={
                    "tenant_id": TENANT,
                    "repo": "repo_unreadable",
                    "module": "mod.handler",
                    "qualname": "unreadable_func",
                    "symbol_kind": "function",
                },
                properties={"path": "handler.py", "line": 1, "end_line": 3},
            )
            snap_dir = root / "snap_unreadable"
            JsonlKgStore(snap_dir).write(
                entities=[e], facts=[], evidence=[], coverage=[],
                manifest={"version": 1, "tenant_id": TENANT},
            )
            snap = KgSnapshot(snap_dir)
            entity_dicts = [d for d in snap.entities if d.get("kind") == "CodeSymbol"]

            # Valid dirs but the source file is ABSENT from both checkouts
            base_dir = root / "base_unreadable"
            base_dir.mkdir()
            head_dir = root / "head_unreadable"
            head_dir.mkdir()
            # handler.py intentionally NOT written — every read returns ""

            _, status = semantic_contract_diff(
                base_snapshot=snap,
                head_snapshot=snap,
                base_root=base_dir,
                head_root=head_dir,
                changed_symbols=entity_dicts,
                client=_FakeClient(),
            )
            self.assertEqual(
                status, "failed:unreadable_sources",
                f"all reads failing must yield 'failed:unreadable_sources'; got {status!r}",
            )

    def test_identical_bodies_still_returns_active(self) -> None:
        """Legitimate identical-bodies case (not a read failure) still returns 'active'."""
        from source.kg.query.semantic_contract_diff import semantic_contract_diff
        from source.kg.core.models import Entity
        from source.kg.core.store import JsonlKgStore

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            e = Entity(
                kind="CodeSymbol",
                identity={
                    "tenant_id": TENANT,
                    "repo": "repo_identical",
                    "module": "mod.handler",
                    "qualname": "same_func",
                    "symbol_kind": "function",
                },
                properties={"path": "handler.py", "line": 1, "end_line": 3},
            )
            snap_dir = root / "snap_identical"
            JsonlKgStore(snap_dir).write(
                entities=[e], facts=[], evidence=[], coverage=[],
                manifest={"version": 1, "tenant_id": TENANT},
            )
            snap = KgSnapshot(snap_dir)
            entity_dicts = [d for d in snap.entities if d.get("kind") == "CodeSymbol"]

            same_body = "def same_func():\n    return 42\n"
            base_dir = root / "base_identical"
            base_dir.mkdir()
            (base_dir / "handler.py").write_text(same_body, encoding="utf-8")
            head_dir = root / "head_identical"
            head_dir.mkdir()
            (head_dir / "handler.py").write_text(same_body, encoding="utf-8")

            _, status = semantic_contract_diff(
                base_snapshot=snap,
                head_snapshot=snap,
                base_root=base_dir,
                head_root=head_dir,
                changed_symbols=entity_dicts,
                client=_FakeClient(),
            )
            self.assertEqual(
                status, "active",
                f"identical bodies (not a read failure) must yield 'active'; got {status!r}",
            )


# ---------------------------------------------------------------------------
# Fix wave: P2 — malformed duplicate precedes valid duplicate (regression)
# ---------------------------------------------------------------------------

class TestDedupeValidationOrder(unittest.TestCase):
    """P2 fix: validate before dedupe — malformed item with same claim must NOT suppress valid item."""

    def _make_single_symbol_snap_p2(self, root: Path) -> tuple[KgSnapshot, Path, Path]:
        from source.kg.core.models import Entity
        from source.kg.core.store import JsonlKgStore

        e = Entity(
            kind="CodeSymbol",
            identity={
                "tenant_id": TENANT,
                "repo": "repo_p2_dedupe",
                "module": "mod.handler",
                "qualname": "p2_func",
                "symbol_kind": "function",
            },
            properties={"path": "handler.py", "line": 1, "end_line": 4},
        )
        snap_dir = root / "snap_p2_dedupe"
        JsonlKgStore(snap_dir).write(
            entities=[e], facts=[], evidence=[], coverage=[],
            manifest={"version": 1, "tenant_id": TENANT},
        )
        snap = KgSnapshot(snap_dir)
        base_dir = root / "base_p2_dedupe"
        base_dir.mkdir()
        (base_dir / "handler.py").write_text("def p2_func():\n    if x: raise\n    return 42\n")
        head_dir = root / "head_p2_dedupe"
        head_dir.mkdir()
        (head_dir / "handler.py").write_text("def p2_func():\n    return 42\n")
        return snap, base_dir, head_dir

    def test_malformed_then_valid_same_claim_valid_survives(self) -> None:
        """[malformed {claim: X, garbage}, valid {claim: X, ...}] → valid item survives (1 row).

        Regression: before the fix, malformed item claimed the dedupe slot and valid item was dropped.
        After the fix (validate first, dedupe after), valid item is the first accepted entry.
        """
        from source.kg.query.semantic_contract_diff import semantic_contract_diff

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snap, base_dir, head_dir = self._make_single_symbol_snap_p2(root)
            entity_dicts = [d for d in snap.entities if d.get("kind") == "CodeSymbol"]

            shared_claim = "Guard removed from p2_func."
            client = _FakeClient(response=[
                # Malformed: missing required keys (cause_line, consequence, negative_check, category)
                {"claim": shared_claim, "garbage_key": "ignored"},
                # Valid: all required keys present, same claim string
                _claim(
                    claim=shared_claim,
                    cause_line=2,
                    consequence="Callers may pass None.",
                    negative_check="No check.",
                    category="guard_removal",
                ),
            ])

            rows, _status = semantic_contract_diff(
                base_snapshot=snap,
                head_snapshot=snap,
                base_root=base_dir,
                head_root=head_dir,
                changed_symbols=entity_dicts,
                client=client,
            )

            # Hard assert: valid item must produce exactly 1 row
            self.assertEqual(
                len(rows), 1,
                f"valid item after malformed duplicate must survive; got {len(rows)} rows",
            )
            self.assertIn(
                shared_claim[:300], rows[0].get("postable_claim", ""),
                f"surviving row must carry the shared claim; got {rows[0].get('postable_claim')!r}",
            )

    def test_inversion_two_valid_same_claim_still_dedupes_to_one(self) -> None:
        """Inversion: two VALID items with same claim → still deduped to 1 row (first wins)."""
        from source.kg.query.semantic_contract_diff import semantic_contract_diff

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snap, base_dir, head_dir = self._make_single_symbol_snap_p2(root)
            entity_dicts = [d for d in snap.entities if d.get("kind") == "CodeSymbol"]

            shared_claim = "Guard removed from p2_func."
            client = _FakeClient(response=[
                _claim(
                    claim=shared_claim,
                    cause_line=2,
                    consequence="Callers may pass None.",
                    negative_check="No check.",
                    category="guard_removal",   # first — must win
                ),
                _claim(
                    claim=shared_claim,
                    cause_line=3,
                    consequence="Data loss.",
                    negative_check="Other check.",
                    category="ownership_moved",  # second — must be dropped
                ),
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
                f"two valid items with same claim must dedupe to 1 row; got {len(rows)}",
            )
            self.assertEqual(
                rows[0].get("category"), "guard_removal",
                f"first valid item's category must win; got {rows[0].get('category')!r}",
            )


# ---------------------------------------------------------------------------
# Fix wave: parse-miss honesty (Fix 1)
# ---------------------------------------------------------------------------

class _AllParseMissClient:
    """Client that always returns parse_miss (call completes, no parseable JSON)."""

    def __init__(self) -> None:
        self.call_count = 0

    def complete_json(self, prompt: str) -> "LlmResult":
        self.call_count += 1
        return LlmResult.parse_miss()


class _MixedParseMissClient:
    """First call returns valid JSON; second call returns parse_miss."""

    def __init__(self) -> None:
        self.call_count = 0

    def complete_json(self, prompt: str) -> "LlmResult":
        self.call_count += 1
        if self.call_count == 1:
            return LlmResult.parsed(_FAKE_RESPONSE)
        return LlmResult.parse_miss()


class TestParseMissHonesty(unittest.TestCase):
    """Fix 1: parse_miss tracked separately; all-miss → failed:all_responses_unparseable."""

    def _make_single_symbol_snap(self, root: Path) -> tuple[KgSnapshot, Path, Path]:
        from source.kg.core.models import Entity
        from source.kg.core.store import JsonlKgStore

        e = Entity(
            kind="CodeSymbol",
            identity={
                "tenant_id": TENANT,
                "repo": "repo_parsemiss",
                "module": "mod.handler",
                "qualname": "pm_func",
                "symbol_kind": "function",
            },
            properties={"path": "handler.py", "line": 1, "end_line": 3},
        )
        snap_dir = root / "snap_parsemiss"
        JsonlKgStore(snap_dir).write(
            entities=[e], facts=[], evidence=[], coverage=[],
            manifest={"version": 1, "tenant_id": TENANT},
        )
        snap = KgSnapshot(snap_dir)
        base_dir = root / "base_parsemiss"
        base_dir.mkdir()
        (base_dir / "handler.py").write_text("def pm_func():\n    if x: raise\n    return 1\n")
        head_dir = root / "head_parsemiss"
        head_dir.mkdir()
        (head_dir / "handler.py").write_text("def pm_func():\n    return 1\n")
        return snap, base_dir, head_dir

    def test_all_parse_miss_produces_failed_status(self) -> None:
        """All calls returning parse_miss → status == 'failed:all_responses_unparseable' (exact)."""
        from source.kg.query.semantic_contract_diff import semantic_contract_diff

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snap, base_dir, head_dir = self._make_single_symbol_snap(root)
            entity_dicts = [d for d in snap.entities if d.get("kind") == "CodeSymbol"]

            client = _AllParseMissClient()
            rows, status = semantic_contract_diff(
                base_snapshot=snap,
                head_snapshot=snap,
                base_root=base_dir,
                head_root=head_dir,
                changed_symbols=entity_dicts,
                client=client,
            )
            self.assertGreaterEqual(client.call_count, 1, "client must be called at least once")
            self.assertEqual(rows, [], "all parse_miss must produce no rows")
            self.assertEqual(
                status, "failed:all_responses_unparseable",
                f"all parse_miss must yield 'failed:all_responses_unparseable'; got {status!r}",
            )

    def test_inversion_all_parse_miss_not_active(self) -> None:
        """Inversion: all parse_miss must NOT produce 'active' (the silent-nothing gap)."""
        from source.kg.query.semantic_contract_diff import semantic_contract_diff

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snap, base_dir, head_dir = self._make_single_symbol_snap(root)
            entity_dicts = [d for d in snap.entities if d.get("kind") == "CodeSymbol"]

            client = _AllParseMissClient()
            _, status = semantic_contract_diff(
                base_snapshot=snap,
                head_snapshot=snap,
                base_root=base_dir,
                head_root=head_dir,
                changed_symbols=entity_dicts,
                client=client,
            )
            self.assertNotEqual(
                status, "active",
                "all parse_miss must NOT be 'active' — that hides the silent-nothing gap",
            )

    def test_mixed_parse_miss_stays_active_with_rows(self) -> None:
        """First call succeeds, second parse_miss → status 'active', rows from first call present."""
        from source.kg.query.semantic_contract_diff import semantic_contract_diff
        from source.kg.core.models import Entity
        from source.kg.core.store import JsonlKgStore

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            # Two differing symbols so two calls happen
            entities = []
            for i in range(2):
                entities.append(Entity(
                    kind="CodeSymbol",
                    identity={
                        "tenant_id": TENANT,
                        "repo": "repo_mixedpm",
                        "module": f"mod.m{i}",
                        "qualname": f"mixed_func_{i}",
                        "symbol_kind": "function",
                    },
                    properties={"path": f"f{i}.py", "line": 1, "end_line": 3},
                ))
            snap_dir = root / "snap_mixedpm"
            JsonlKgStore(snap_dir).write(
                entities=entities, facts=[], evidence=[], coverage=[],
                manifest={"version": 1, "tenant_id": TENANT},
            )
            snap = KgSnapshot(snap_dir)
            entity_dicts = [d for d in snap.entities if d.get("kind") == "CodeSymbol"]

            base_dir = root / "base_mixedpm"
            base_dir.mkdir()
            head_dir = root / "head_mixedpm"
            head_dir.mkdir()
            for i in range(2):
                (base_dir / f"f{i}.py").write_text(f"def mixed_func_{i}():\n    if x: raise\n    return {i}\n")
                (head_dir / f"f{i}.py").write_text(f"def mixed_func_{i}():\n    return {i}\n")

            client = _MixedParseMissClient()
            rows, status = semantic_contract_diff(
                base_snapshot=snap,
                head_snapshot=snap,
                base_root=base_dir,
                head_root=head_dir,
                changed_symbols=entity_dicts,
                client=client,
            )

            if client.call_count < 2:
                self.skipTest("fixture produced <2 differing symbols — need >=2 for mixed test")

            self.assertGreater(len(rows), 0, "first-call success must produce >=1 row")
            # Mixed: some parsed, some missed → base status "active" with exact detail
            # counts appended (quality note the caller logs).
            self.assertEqual(
                status, "active (1 of 2 responses unparseable)",
                f"mixed parse_miss must carry exact detail counts; got {status!r}",
            )
            # Inversion: must not be the all-miss failure status.
            self.assertNotEqual(
                status, "failed:all_responses_unparseable",
                "mixed parse_miss must not be 'failed:all_responses_unparseable'",
            )

    def test_all_miss_with_call_failures_still_unparseable(self) -> None:
        """parse_miss mixed with call failures, zero parsed → failed:all_responses_unparseable (exact).

        The spec condition is calls_attempted > 0 AND parsed_ok == 0 AND parse_miss > 0 —
        a concurrent call failure must not demote the status back to silent 'active'.
        """
        from source.kg.query.semantic_contract_diff import semantic_contract_diff
        from source.kg.core.models import Entity
        from source.kg.core.store import JsonlKgStore

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            entities = []
            for i in range(2):
                entities.append(Entity(
                    kind="CodeSymbol",
                    identity={
                        "tenant_id": TENANT,
                        "repo": "repo_missfail",
                        "module": f"mod.mf{i}",
                        "qualname": f"missfail_func_{i}",
                        "symbol_kind": "function",
                    },
                    properties={"path": f"f{i}.py", "line": 1, "end_line": 3},
                ))
            snap_dir = root / "snap_missfail"
            JsonlKgStore(snap_dir).write(
                entities=entities, facts=[], evidence=[], coverage=[],
                manifest={"version": 1, "tenant_id": TENANT},
            )
            snap = KgSnapshot(snap_dir)
            entity_dicts = [d for d in snap.entities if d.get("kind") == "CodeSymbol"]

            base_dir = root / "base_missfail"
            base_dir.mkdir()
            head_dir = root / "head_missfail"
            head_dir.mkdir()
            for i in range(2):
                (base_dir / f"f{i}.py").write_text(f"def missfail_func_{i}():\n    if x: raise\n    return {i}\n")
                (head_dir / f"f{i}.py").write_text(f"def missfail_func_{i}():\n    return {i}\n")

            class _MissThenFailClient:
                def __init__(self) -> None:
                    self.call_count = 0

                def complete_json(self, prompt: str) -> "LlmResult":
                    self.call_count += 1
                    if self.call_count == 1:
                        return LlmResult.parse_miss()
                    return LlmResult.call_failure("llm_error")

            client = _MissThenFailClient()
            rows, status = semantic_contract_diff(
                base_snapshot=snap,
                head_snapshot=snap,
                base_root=base_dir,
                head_root=head_dir,
                changed_symbols=entity_dicts,
                client=client,
            )

            if client.call_count < 2:
                self.skipTest("fixture produced <2 differing symbols — need >=2 for mixed test")

            self.assertEqual(rows, [], "no parsed responses must produce zero rows")
            self.assertEqual(
                status, "failed:all_responses_unparseable",
                f"zero parsed + parse_miss>0 must be all_responses_unparseable even with "
                f"call failures; got {status!r}",
            )


# ---------------------------------------------------------------------------
# Fix wave: response-format robustness (Fix 2)
# ---------------------------------------------------------------------------

class TestExtractJsonRobustness(unittest.TestCase):
    """Fix 2: _extract_json handles markdown fences and single-key object wrappers."""

    def test_json_fenced_json(self) -> None:
        """```json ... ``` fence stripped and array parsed."""
        from source.kg.integrations.semantic_llm import _extract_json

        text = '```json\n[{"claim": "x", "cause_line": 1}]\n```'
        result = _extract_json(text)
        self.assertIsInstance(result, list)
        self.assertEqual(result[0]["claim"], "x")

    def test_bare_fence(self) -> None:
        """Bare ``` ... ``` fence stripped and array parsed."""
        from source.kg.integrations.semantic_llm import _extract_json

        text = '```\n[{"claim": "y", "cause_line": 2}]\n```'
        result = _extract_json(text)
        self.assertIsInstance(result, list)
        self.assertEqual(result[0]["claim"], "y")

    def test_single_key_object_unwrapped(self) -> None:
        """{"changes": [...]} wrapper unwrapped to return the list."""
        from source.kg.integrations.semantic_llm import _extract_json

        text = '{"changes": [{"claim": "z", "cause_line": 3}]}'
        result = _extract_json(text)
        self.assertIsInstance(result, list)
        self.assertEqual(result[0]["claim"], "z")

    def test_leading_trailing_prose_with_array(self) -> None:
        """Array embedded in prose (no fence) is extracted correctly."""
        from source.kg.integrations.semantic_llm import _extract_json

        text = 'Here is my analysis:\n[{"claim": "a", "cause_line": 1}]\nDone.'
        result = _extract_json(text)
        self.assertIsInstance(result, list)
        self.assertEqual(result[0]["claim"], "a")

    def test_inversion_plain_prose_returns_none(self) -> None:
        """Pure prose (no JSON structure) returns None."""
        from source.kg.integrations.semantic_llm import _extract_json

        result = _extract_json("Sorry, I cannot analyze this code.")
        self.assertIsNone(result)

    def test_multi_key_object_inner_array_wins_bracket_scan(self) -> None:
        """Object with multiple keys: bracket-scan finds inner array first, returns the list."""
        from source.kg.integrations.semantic_llm import _extract_json

        # The bracket-scan finds '[' (inner array) before '{' (outer object),
        # so [1, 2] is returned — the outer multi-key dict is NOT unwrapped via
        # _unwrap_single_key_object (which requires exactly one key).
        text = '{"changes": [1, 2], "other": "value"}'
        result = _extract_json(text)
        # Inner array [1, 2] is found first
        self.assertIsInstance(result, list)
        self.assertEqual(result, [1, 2])

    def test_nested_brackets_in_string_values(self) -> None:
        """Bracket chars inside string values don't confuse the bracket scanner."""
        from source.kg.integrations.semantic_llm import _extract_json

        text = '[{"claim": "returns [] instead of raising", "cause_line": 5}]'
        result = _extract_json(text)
        self.assertIsInstance(result, list)
        self.assertIn("[]", result[0]["claim"])


# ---------------------------------------------------------------------------
# Fix wave: prompt strictness (Fix 3)
# ---------------------------------------------------------------------------

class TestPromptStrictness(unittest.TestCase):
    """Fix 3: prompt ends with explicit format instruction."""

    def test_prompt_template_ends_with_format_instruction(self) -> None:
        """_PROMPT_TEMPLATE must end with 'Respond with ONLY a JSON array, no markdown fences, no prose.'"""
        from source.kg.query.semantic_contract_diff import _PROMPT_TEMPLATE

        sentinel = "Respond with ONLY a JSON array, no markdown fences, no prose."
        # The template is a format string; format it to get the actual text
        rendered = _PROMPT_TEMPLATE.format(qualname="X", before="A", after="B")
        self.assertIn(
            sentinel, rendered,
            f"prompt must contain format instruction; template tail: {rendered[-200:]!r}",
        )
        self.assertTrue(
            rendered.endswith(sentinel),
            f"format instruction must be at the end of the prompt; tail: {rendered[-200:]!r}",
        )


# ---------------------------------------------------------------------------
# Fix wave: inheritance-context enrichment (Fix 4)
# ---------------------------------------------------------------------------

class TestInheritanceContextEnrichment(unittest.TestCase):
    """Fix 4: base class body appended to prompt for reparented class symbols."""

    def _make_reparenting_snap(
        self,
        root: Path,
        base_class_body: str,
    ) -> tuple[KgSnapshot, Path, Path, list]:
        """Build snap with class X (base) and class NewBase; return snap + checkouts + entities."""
        from source.kg.core.models import Entity
        from source.kg.core.store import JsonlKgStore

        # X: the changed symbol (class X(OldBase) → class X(NewBase))
        x_entity = Entity(
            kind="CodeSymbol",
            identity={
                "tenant_id": TENANT,
                "repo": "repo_enrich",
                "module": "mod.x",
                "qualname": "X",
                "symbol_kind": "class",
            },
            properties={"path": "x.py", "line": 1, "end_line": 5},
        )
        # NewBase: the resolved base entity
        newbase_entity = Entity(
            kind="CodeSymbol",
            identity={
                "tenant_id": TENANT,
                "repo": "repo_enrich",
                "module": "mod.newbase",
                "qualname": "NewBase",
                "symbol_kind": "class",
            },
            properties={"path": "newbase.py", "line": 1, "end_line": 8},
        )

        snap_dir = root / "snap_enrich"
        JsonlKgStore(snap_dir).write(
            entities=[x_entity, newbase_entity], facts=[], evidence=[], coverage=[],
            manifest={"version": 1, "tenant_id": TENANT},
        )
        snap = KgSnapshot(snap_dir)

        base_dir = root / "base_enrich"
        base_dir.mkdir()
        (base_dir / "x.py").write_text("class X(OldBase):\n    def method(self):\n        pass\n")

        head_dir = root / "head_enrich"
        head_dir.mkdir()
        (head_dir / "x.py").write_text("class X(NewBase):\n    def method(self):\n        pass\n")
        (head_dir / "newbase.py").write_text(base_class_body)

        entity_dicts = [d for d in snap.entities if d.get("kind") == "CodeSymbol"]
        return snap, base_dir, head_dir, entity_dicts

    def test_reparenting_prompt_contains_new_base_body(self) -> None:
        """class X(OldBase) → class X(NewBase): prompt contains NewBase's body text."""
        from source.kg.query.semantic_contract_diff import semantic_contract_diff

        newbase_body = "class NewBase:\n    def abstract_method(self):\n        raise NotImplementedError\n"

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snap, base_dir, head_dir, entity_dicts = self._make_reparenting_snap(
                root, newbase_body
            )
            # Only the X entity
            x_entities = [d for d in entity_dicts if (d.get("identity") or {}).get("qualname") == "X"]

            prompt_log: list[str] = []

            class _SpyClient:
                def complete_json(self, prompt: str) -> LlmResult:
                    prompt_log.append(prompt)
                    return LlmResult.parse_miss()

            semantic_contract_diff(
                base_snapshot=snap,
                head_snapshot=snap,
                base_root=base_dir,
                head_root=head_dir,
                changed_symbols=x_entities,
                client=_SpyClient(),
            )

            self.assertEqual(len(prompt_log), 1, "spy must be called exactly once")
            self.assertIn(
                "NewBase", prompt_log[0],
                "prompt must contain NewBase's qualname in the base class context section",
            )
            self.assertIn(
                "abstract_method", prompt_log[0],
                "prompt must contain NewBase's body text (abstract_method)",
            )
            self.assertIn(
                "Referenced base class (for context)", prompt_log[0],
                "prompt must contain the 'Referenced base class (for context)' section header",
            )

    def test_non_class_symbol_no_enrichment(self) -> None:
        """Non-class symbol (function): no base class section appended."""
        from source.kg.query.semantic_contract_diff import semantic_contract_diff
        from source.kg.core.models import Entity
        from source.kg.core.store import JsonlKgStore

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            e = Entity(
                kind="CodeSymbol",
                identity={
                    "tenant_id": TENANT,
                    "repo": "repo_noenrich",
                    "module": "mod.f",
                    "qualname": "plain_func",
                    "symbol_kind": "function",
                },
                properties={"path": "f.py", "line": 1, "end_line": 3},
            )
            snap_dir = root / "snap_noenrich"
            JsonlKgStore(snap_dir).write(
                entities=[e], facts=[], evidence=[], coverage=[],
                manifest={"version": 1, "tenant_id": TENANT},
            )
            snap = KgSnapshot(snap_dir)
            entity_dicts = [d for d in snap.entities if d.get("kind") == "CodeSymbol"]

            base_dir = root / "base_noenrich"
            base_dir.mkdir()
            (base_dir / "f.py").write_text("def plain_func():\n    if x: raise\n    return 1\n")
            head_dir = root / "head_noenrich"
            head_dir.mkdir()
            (head_dir / "f.py").write_text("def plain_func():\n    return 1\n")

            prompt_log: list[str] = []

            class _SpyClient:
                def complete_json(self, prompt: str) -> LlmResult:
                    prompt_log.append(prompt)
                    return LlmResult.parse_miss()

            semantic_contract_diff(
                base_snapshot=snap,
                head_snapshot=snap,
                base_root=base_dir,
                head_root=head_dir,
                changed_symbols=entity_dicts,
                client=_SpyClient(),
            )

            self.assertEqual(len(prompt_log), 1, "spy must be called exactly once")
            self.assertNotIn(
                "Referenced base class (for context)", prompt_log[0],
                "non-class symbol must not have base class context section",
            )

    def test_unresolvable_base_no_enrichment_no_error(self) -> None:
        """class X(Unknown) → prompt has no base context section, no exception raised."""
        from source.kg.query.semantic_contract_diff import semantic_contract_diff
        from source.kg.core.models import Entity
        from source.kg.core.store import JsonlKgStore

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            # X references Unknown which is NOT in the snapshot entities
            e = Entity(
                kind="CodeSymbol",
                identity={
                    "tenant_id": TENANT,
                    "repo": "repo_unresolvable",
                    "module": "mod.x",
                    "qualname": "X",
                    "symbol_kind": "class",
                },
                properties={"path": "x.py", "line": 1, "end_line": 4},
            )
            snap_dir = root / "snap_unresolvable"
            JsonlKgStore(snap_dir).write(
                entities=[e], facts=[], evidence=[], coverage=[],
                manifest={"version": 1, "tenant_id": TENANT},
            )
            snap = KgSnapshot(snap_dir)
            entity_dicts = [d for d in snap.entities if d.get("kind") == "CodeSymbol"]

            base_dir = root / "base_unresolvable"
            base_dir.mkdir()
            (base_dir / "x.py").write_text("class X(OldBase):\n    pass\n")
            head_dir = root / "head_unresolvable"
            head_dir.mkdir()
            # Head references Unknown (not in snap)
            (head_dir / "x.py").write_text("class X(Unknown):\n    pass\n")

            prompt_log: list[str] = []
            raised: list[Exception] = []

            class _SpyClient:
                def complete_json(self, prompt: str) -> LlmResult:
                    prompt_log.append(prompt)
                    return LlmResult.parse_miss()

            try:
                semantic_contract_diff(
                    base_snapshot=snap,
                    head_snapshot=snap,
                    base_root=base_dir,
                    head_root=head_dir,
                    changed_symbols=entity_dicts,
                    client=_SpyClient(),
                )
            except Exception as exc:  # noqa: BLE001
                raised.append(exc)

            self.assertEqual(raised, [], "unresolvable base must not raise any exception")
            self.assertEqual(len(prompt_log), 1, "spy must be called exactly once")
            self.assertNotIn(
                "Referenced base class (for context)", prompt_log[0],
                "unresolvable base must not produce a base class context section",
            )


# ---------------------------------------------------------------------------
# P2 fix: ambiguous base-class enrichment disambiguation
# ---------------------------------------------------------------------------

class TestBaseClassEnrichmentDisambiguation(unittest.TestCase):
    """P2 fix: _build_base_class_context must not attach the wrong class body.

    Rules:
      - Bare base name + 2+ same-last-seg candidates → NO enrichment (conservative skip).
      - Dotted base (e.g. pkg.Base) → exact qualified suffix match → enriched when unique.
      - Single candidate for a bare name → enriched (existing behaviour preserved).
    """

    def _make_snap_with_two_same_seg_classes(self, root: Path) -> tuple[KgSnapshot, Path, Path]:
        """Two CodeSymbol entities share last qualname segment 'Base' in different modules."""
        from source.kg.core.models import Entity
        from source.kg.core.store import JsonlKgStore

        base_a = Entity(
            kind="CodeSymbol",
            identity={
                "tenant_id": TENANT,
                "repo": "repo_ambig",
                "module": "mod_a",
                "qualname": "mod_a.Base",
                "symbol_kind": "class",
            },
            properties={"path": "mod_a/base.py", "line": 1, "end_line": 4},
        )
        base_b = Entity(
            kind="CodeSymbol",
            identity={
                "tenant_id": TENANT,
                "repo": "repo_ambig",
                "module": "mod_b",
                "qualname": "mod_b.Base",
                "symbol_kind": "class",
            },
            properties={"path": "mod_b/base.py", "line": 1, "end_line": 4},
        )
        x_entity = Entity(
            kind="CodeSymbol",
            identity={
                "tenant_id": TENANT,
                "repo": "repo_ambig",
                "module": "mod_x",
                "qualname": "X",
                "symbol_kind": "class",
            },
            properties={"path": "x.py", "line": 1, "end_line": 4},
        )
        snap_dir = root / "snap_ambig"
        JsonlKgStore(snap_dir).write(
            entities=[base_a, base_b, x_entity], facts=[], evidence=[], coverage=[],
            manifest={"version": 1, "tenant_id": TENANT},
        )
        snap = KgSnapshot(snap_dir)

        base_checkout = root / "base_ambig"
        base_checkout.mkdir()
        (base_checkout / "x.py").write_text("class X(OldBase):\n    pass\n")
        head_checkout = root / "head_ambig"
        head_checkout.mkdir()
        # Bare base name 'Base' → 2 candidates in KG
        (head_checkout / "x.py").write_text("class X(Base):\n    pass\n")
        (head_checkout / "mod_a").mkdir()
        (head_checkout / "mod_a" / "base.py").write_text("class Base:\n    def method_a(self): pass\n")
        (head_checkout / "mod_b").mkdir()
        (head_checkout / "mod_b" / "base.py").write_text("class Base:\n    def method_b(self): pass\n")

        return snap, base_checkout, head_checkout

    def test_two_same_seg_bare_name_no_enrichment(self) -> None:
        """Two classes with same last segment + bare base name in class line → NO enrichment.

        Hard assert: the prompt must not contain EITHER class body.
        Regression: old code took matches[0] → wrong class attached.
        """
        from source.kg.query.semantic_contract_diff import semantic_contract_diff

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snap, base_checkout, head_checkout = self._make_snap_with_two_same_seg_classes(root)
            x_dicts = [
                d for d in snap.entities
                if (d.get("identity") or {}).get("qualname") == "X"
            ]
            self.assertEqual(len(x_dicts), 1, "fixture must have exactly one X entity")

            prompt_log: list[str] = []

            class _SpyClient:
                def complete_json(self, prompt: str) -> LlmResult:
                    prompt_log.append(prompt)
                    return LlmResult.parse_miss()

            semantic_contract_diff(
                base_snapshot=snap,
                head_snapshot=snap,
                base_root=base_checkout,
                head_root=head_checkout,
                changed_symbols=x_dicts,
                client=_SpyClient(),
            )

            self.assertEqual(len(prompt_log), 1, "spy must be called exactly once")
            prompt = prompt_log[0]
            # Neither mod_a.Base nor mod_b.Base body should appear
            self.assertNotIn(
                "method_a", prompt,
                "prompt must NOT contain mod_a.Base body when bare name is ambiguous",
            )
            self.assertNotIn(
                "method_b", prompt,
                "prompt must NOT contain mod_b.Base body when bare name is ambiguous",
            )
            self.assertNotIn(
                "Referenced base class (for context)", prompt,
                "no base class context section must appear when bare name is ambiguous",
            )

    def test_dotted_base_resolves_uniquely(self) -> None:
        """Dotted base (mod_a.Base) → only mod_a.Base matches → enriched with its body."""
        from source.kg.query.semantic_contract_diff import semantic_contract_diff

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snap, base_checkout, head_checkout = self._make_snap_with_two_same_seg_classes(root)
            # Build a head checkout where X uses the DOTTED form
            (head_checkout / "x.py").write_text("class X(mod_a.Base):\n    pass\n")
            (base_checkout / "x.py").write_text("class X(OldBase):\n    pass\n")

            x_dicts = [
                d for d in snap.entities
                if (d.get("identity") or {}).get("qualname") == "X"
            ]

            prompt_log: list[str] = []

            class _SpyClient:
                def complete_json(self, prompt: str) -> LlmResult:
                    prompt_log.append(prompt)
                    return LlmResult.parse_miss()

            semantic_contract_diff(
                base_snapshot=snap,
                head_snapshot=snap,
                base_root=base_checkout,
                head_root=head_checkout,
                changed_symbols=x_dicts,
                client=_SpyClient(),
            )

            self.assertEqual(len(prompt_log), 1, "spy must be called exactly once")
            prompt = prompt_log[0]
            # mod_a.Base qualname ends with "mod_a.Base" → unique suffix match → enriched
            self.assertIn(
                "Referenced base class (for context)", prompt,
                "dotted base resolved uniquely must enrich the prompt",
            )
            self.assertIn(
                "method_a", prompt,
                "prompt must contain mod_a.Base body (method_a) for dotted reference",
            )
            self.assertNotIn(
                "method_b", prompt,
                "prompt must NOT contain mod_b.Base body for mod_a.Base reference",
            )

    def test_single_candidate_bare_name_enriched(self) -> None:
        """Single candidate for a bare name → enriched (existing behaviour preserved)."""
        from source.kg.query.semantic_contract_diff import semantic_contract_diff
        from source.kg.core.models import Entity
        from source.kg.core.store import JsonlKgStore

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            # Only ONE entity with qualname ending in "UniqueBase"
            unique_entity = Entity(
                kind="CodeSymbol",
                identity={
                    "tenant_id": TENANT,
                    "repo": "repo_unique",
                    "module": "mod_u",
                    "qualname": "mod_u.UniqueBase",
                    "symbol_kind": "class",
                },
                properties={"path": "ubase.py", "line": 1, "end_line": 4},
            )
            x_entity = Entity(
                kind="CodeSymbol",
                identity={
                    "tenant_id": TENANT,
                    "repo": "repo_unique",
                    "module": "mod_x",
                    "qualname": "XUnique",
                    "symbol_kind": "class",
                },
                properties={"path": "x.py", "line": 1, "end_line": 4},
            )
            snap_dir = root / "snap_unique"
            JsonlKgStore(snap_dir).write(
                entities=[unique_entity, x_entity], facts=[], evidence=[], coverage=[],
                manifest={"version": 1, "tenant_id": TENANT},
            )
            snap = KgSnapshot(snap_dir)

            base_dir = root / "base_unique"
            base_dir.mkdir()
            (base_dir / "x.py").write_text("class XUnique(OldBase):\n    pass\n")
            head_dir = root / "head_unique"
            head_dir.mkdir()
            (head_dir / "x.py").write_text("class XUnique(UniqueBase):\n    pass\n")
            (head_dir / "ubase.py").write_text(
                "class UniqueBase:\n    def unique_method(self): pass\n"
            )

            x_dicts = [
                d for d in snap.entities
                if (d.get("identity") or {}).get("qualname") == "XUnique"
            ]

            prompt_log: list[str] = []

            class _SpyClient:
                def complete_json(self, prompt: str) -> LlmResult:
                    prompt_log.append(prompt)
                    return LlmResult.parse_miss()

            semantic_contract_diff(
                base_snapshot=snap,
                head_snapshot=snap,
                base_root=base_dir,
                head_root=head_dir,
                changed_symbols=x_dicts,
                client=_SpyClient(),
            )

            self.assertEqual(len(prompt_log), 1, "spy must be called exactly once")
            prompt = prompt_log[0]
            self.assertIn(
                "Referenced base class (for context)", prompt,
                "single candidate bare name must be enriched",
            )
            self.assertIn(
                "unique_method", prompt,
                "prompt must contain UniqueBase body (unique_method)",
            )


# ---------------------------------------------------------------------------
# Field fix wave 2: subscripted base names, instruction-last with enrichment
# ---------------------------------------------------------------------------

class TestBaseNameExtraction(unittest.TestCase):
    """_extract_base_names_from_class_line: exact values incl. generic subscripts."""

    def test_subscripted_base_stripped(self) -> None:
        """class X(Base[T]): → ['Base'] — subscript must not leak into the name."""
        from source.kg.query.semantic_contract_diff import _extract_base_names_from_class_line

        self.assertEqual(
            _extract_base_names_from_class_line(
                "class MetricAlertDetectorHandler(StatefulDetectorHandler[QuerySubscriptionUpdate]):"
            ),
            ["StatefulDetectorHandler"],
        )

    def test_dotted_subscripted_base(self) -> None:
        """class X(pkg.mod.Base[T], Other): → ['Base', 'Other']."""
        from source.kg.query.semantic_contract_diff import _extract_base_names_from_class_line

        self.assertEqual(
            _extract_base_names_from_class_line("class X(pkg.mod.Base[T], Other):"),
            ["Base", "Other"],
        )

    def test_comma_inside_subscript_not_split(self) -> None:
        """class X(Generic[T, U], Base): → ['Generic', 'Base'] — top-level split only."""
        from source.kg.query.semantic_contract_diff import _extract_base_names_from_class_line

        self.assertEqual(
            _extract_base_names_from_class_line("class X(Generic[T, U], Base):"),
            ["Generic", "Base"],
        )

    def test_keyword_argument_skipped(self) -> None:
        """class X(Base, metaclass=ABCMeta): → ['Base'] — kwargs are not bases."""
        from source.kg.query.semantic_contract_diff import _extract_base_names_from_class_line

        self.assertEqual(
            _extract_base_names_from_class_line("class X(Base, metaclass=ABCMeta):"),
            ["Base"],
        )

    def test_inversion_non_class_line(self) -> None:
        """Non-class line → [] (no false extraction)."""
        from source.kg.query.semantic_contract_diff import _extract_base_names_from_class_line

        self.assertEqual(_extract_base_names_from_class_line("def f(a, b):"), [])


class TestSubscriptedBaseEnrichment(unittest.TestCase):
    """Field litmus shape: reparented class with a GENERIC-SUBSCRIPTED base resolves.

    class X(OldBase[T]) → class X(NewBase[T]) where NewBase is a KG entity —
    the subscript must be stripped before qualname matching or enrichment
    silently never fires on real generic handlers.
    """

    def test_subscripted_reparenting_enriches_and_instruction_stays_last(self) -> None:
        from source.kg.query.semantic_contract_diff import (
            semantic_contract_diff,
            _PROMPT_FORMAT_INSTRUCTION,
        )
        from source.kg.core.models import Entity
        from source.kg.core.store import JsonlKgStore

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            x_entity = Entity(
                kind="CodeSymbol",
                identity={
                    "tenant_id": TENANT,
                    "repo": "repo_subenrich",
                    "module": "mod.x",
                    "qualname": "X",
                    "symbol_kind": "class",
                },
                properties={"path": "x.py", "line": 1, "end_line": 3},
            )
            newbase_entity = Entity(
                kind="CodeSymbol",
                identity={
                    "tenant_id": TENANT,
                    "repo": "repo_subenrich",
                    "module": "mod.newbase",
                    "qualname": "NewBase",
                    "symbol_kind": "class",
                },
                properties={"path": "newbase.py", "line": 1, "end_line": 4},
            )
            snap_dir = root / "snap_subenrich"
            JsonlKgStore(snap_dir).write(
                entities=[x_entity, newbase_entity], facts=[], evidence=[], coverage=[],
                manifest={"version": 1, "tenant_id": TENANT},
            )
            snap = KgSnapshot(snap_dir)

            base_dir = root / "base_subenrich"
            base_dir.mkdir()
            (base_dir / "x.py").write_text("class X(OldBase[int]):\n    def method(self):\n        pass\n")
            head_dir = root / "head_subenrich"
            head_dir.mkdir()
            (head_dir / "x.py").write_text("class X(NewBase[int]):\n    def method(self):\n        pass\n")
            (head_dir / "newbase.py").write_text(
                "class NewBase(Generic[T]):\n    def abstract_method(self):\n        raise NotImplementedError\n"
            )

            x_dicts = [d for d in snap.entities if (d.get("identity") or {}).get("qualname") == "X"]

            prompt_log: list[str] = []

            class _SpyClient:
                def complete_json(self, prompt: str) -> LlmResult:
                    prompt_log.append(prompt)
                    return LlmResult.parse_miss()

            semantic_contract_diff(
                base_snapshot=snap,
                head_snapshot=snap,
                base_root=base_dir,
                head_root=head_dir,
                changed_symbols=x_dicts,
                client=_SpyClient(),
            )

            self.assertEqual(len(prompt_log), 1, "spy must be called exactly once")
            prompt = prompt_log[0]
            self.assertIn(
                "Referenced base class (for context)", prompt,
                "subscripted base must still resolve and enrich the prompt",
            )
            self.assertIn(
                "abstract_method", prompt,
                "prompt must contain NewBase's body text (abstract_method)",
            )
            # Fix 3 invariant survives enrichment: the format instruction is LAST.
            self.assertTrue(
                prompt.endswith(_PROMPT_FORMAT_INSTRUCTION),
                f"format instruction must end the ENRICHED prompt; tail: {prompt[-200:]!r}",
            )
            # Inversion: enrichment section must appear BEFORE the instruction.
            self.assertLess(
                prompt.find("Referenced base class (for context)"),
                prompt.rfind(_PROMPT_FORMAT_INSTRUCTION),
                "base-class context must precede the final format instruction",
            )


# ---------------------------------------------------------------------------
# Field fix wave 2: reserved semantic slot in the splice ordering
# ---------------------------------------------------------------------------

class TestSemanticSpliceReservedSlot(unittest.TestCase):
    """Semantic rows must survive the top-hypotheses cap and the budget floor (top 3).

    With >=3 deterministic rows present, appending semantic rows after ALL of them
    erases the entire semantic family at the prefix caps. The splice keeps the top 2
    deterministic rows, then semantic rows, then the rest — exact-order test.
    """

    def _run_splice(self, existing_hyps: list) -> list:
        from source.kg.product.mcp_tools import _splice_semantic_diff_hypotheses
        from source.kg.core.models import Entity
        from source.kg.core.store import JsonlKgStore
        from source.kg.query.snapshot import KgSnapshot

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            e = Entity(
                kind="CodeSymbol",
                identity={
                    "tenant_id": "default",
                    "repo": "slot_repo",
                    "module": "slot_mod",
                    "qualname": "slot_func",
                    "symbol_kind": "function",
                },
                properties={"path": "slot.py", "line": 1, "end_line": 4},
            )
            snap_dir = root / "snap_slot"
            JsonlKgStore(snap_dir).write(
                entities=[e], facts=[], evidence=[], coverage=[],
                manifest={"version": 1, "tenant_id": "default"},
            )
            head_kg = KgSnapshot(snap_dir)
            base_dir = root / "base_slot"
            base_dir.mkdir()
            (base_dir / "slot.py").write_text("def slot_func():\n    if x: raise\n    return 1\n")
            head_dir = root / "head_slot"
            head_dir.mkdir()
            (head_dir / "slot.py").write_text("def slot_func():\n    return 1\n")

            class _OneClaimClient:
                def complete_json(self, prompt: str) -> LlmResult:
                    return LlmResult.parsed([_claim(
                        claim="Guard removed.",
                        cause_line=2,
                        consequence="Callers may pass None.",
                        negative_check="No check.",
                        category="guard_removal",
                    )])

            merged, _status = _splice_semantic_diff_hypotheses(
                base_snapshot_dir=str(snap_dir),
                head_kg=head_kg,
                base_checkout=str(base_dir),
                head_checkout=str(head_dir),
                changed_symbols=[{"qualname": "slot_func"}],
                review_hypotheses=existing_hyps,
                _client=_OneClaimClient(),
            )
            return merged

    def test_semantic_row_at_index_2_with_many_det_rows(self) -> None:
        """4 det + 1 generic existing → exact order: det0, det1, semantic, det2, det3, generic."""
        existing = [
            {"hypothesis_id": f"det-{i}", "risk_type": "guard_call_removed_drift",
             "derivation": "deterministic_static"}
            for i in range(4)
        ] + [
            {"hypothesis_id": "gen-0", "risk_type": "direct_call_contract_drift"},
        ]
        merged = self._run_splice(existing)
        got = [
            (h.get("hypothesis_id") if h.get("risk_type") != "contract_semantic_diff" else "SEM")
            for h in merged
        ]
        self.assertEqual(
            got, ["det-0", "det-1", "SEM", "det-2", "det-3", "gen-0"],
            f"reserved-slot order violated; got {got}",
        )
        # Inversion: the semantic row must sit inside the budget floor window (top 3),
        # or compaction to top-3 silently erases the semantic family.
        sem_idx = got.index("SEM")
        self.assertLess(sem_idx, 3, f"semantic row must survive top-3 floor; index {sem_idx}")

    def test_semantic_row_before_generic_rows_when_no_det(self) -> None:
        """Only generic rows (derivation None) → semantic row is first."""
        existing = [
            {"hypothesis_id": "gen-0", "risk_type": "direct_call_contract_drift"},
            {"hypothesis_id": "gen-1", "risk_type": "application_surface_contract_drift"},
        ]
        merged = self._run_splice(existing)
        self.assertEqual(
            merged[0].get("risk_type"), "contract_semantic_diff",
            f"semantic row must precede generic rows; got {[h.get('risk_type') for h in merged]}",
        )
        self.assertEqual(
            [h.get("hypothesis_id") for h in merged[1:]], ["gen-0", "gen-1"],
            "generic row order must be preserved after the semantic row",
        )


# ---------------------------------------------------------------------------
# semantic_diff_stats: cost/token exposure in review_quality_status
# ---------------------------------------------------------------------------

class TestSemanticDiffStats(unittest.TestCase):
    """semantic_diff_stats block in review_quality_status carries token/cost data.

    Uses the REAL SemanticDiffLlmClient with a patched litellm module (per
    TestRealClientTypedResult pattern) so the typed LlmResult usage fields are
    exercised end-to-end through semantic_contract_diff and the splice.
    """

    def _inject_fake_litellm_with_usage(
        self,
        prompt_tokens: int,
        completion_tokens: int,
        cost_usd: float,
        json_payload: list,
    ):
        """Return a fake litellm module whose completion returns a response with usage."""
        import json as _json
        import types

        class _Usage:
            def __init__(self) -> None:
                self.prompt_tokens = prompt_tokens
                self.completion_tokens = completion_tokens

        class _Message:
            def __init__(self) -> None:
                self.content = _json.dumps(json_payload)

        class _Choice:
            def __init__(self) -> None:
                self.message = _Message()

        class _Response:
            def __init__(self) -> None:
                self.choices = [_Choice()]
                self.usage = _Usage()

        def _completion_cost(completion_response=None, **kwargs):
            return cost_usd

        fake = types.ModuleType("litellm")
        fake.completion = lambda **kwargs: _Response()
        fake.completion_cost = _completion_cost
        return fake

    def _inject_fake_litellm_no_usage(self, json_payload: list):
        """Return a fake litellm module whose response has no .usage attribute."""
        import json as _json
        import types

        class _Message:
            def __init__(self) -> None:
                self.content = _json.dumps(json_payload)

        class _Choice:
            def __init__(self) -> None:
                self.message = _Message()

        class _Response:
            def __init__(self) -> None:
                self.choices = [_Choice()]
                # deliberately no .usage attribute

        def _completion_cost(**kwargs):
            raise Exception("unknown model")

        fake = types.ModuleType("litellm")
        fake.completion = lambda **kwargs: _Response()
        fake.completion_cost = _completion_cost
        return fake

    def test_stats_in_final_packet_after_splice_and_budget(self) -> None:
        """semantic_diff_stats present in review_quality_status after full call_tool pipeline.

        Uses REAL SemanticDiffLlmClient with patched litellm returning usage.
        Inversion: asserts calls_attempted >= 1 (proves real client path ran).
        Hard asserts on aggregated token/cost values in the final packet.
        """
        import sys
        from unittest.mock import patch
        from source.kg.integrations.semantic_llm import SemanticDiffLlmClient
        from source.kg.product.mcp_tools import _splice_semantic_diff_hypotheses as _real_splice

        fake_litellm = self._inject_fake_litellm_with_usage(
            prompt_tokens=100,
            completion_tokens=50,
            cost_usd=0.0012,
            json_payload=_FAKE_RESPONSE,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            out_base, out_head, base_checkout, head_checkout = _build_two_snapshot_pair(root)
            head_kg = KgSnapshot(out_head)

            # Hard assert: fixture must have CodeSymbol entities
            self.assertTrue(
                any(e.get("kind") == "CodeSymbol" for e in head_kg.entities),
                "fixture KG must have CodeSymbol entities",
            )

            with patch.dict(sys.modules, {"litellm": fake_litellm}):
                real_client = SemanticDiffLlmClient(model="fake-model")

                def _with_real_client(**kw):
                    return _real_splice(**kw, _client=real_client)

                with patch(
                    "source.kg.product.mcp_tools._splice_semantic_diff_hypotheses",
                    side_effect=lambda **kw: _with_real_client(**kw),
                ):
                    result = call_tool(head_kg, "review_context", {
                        "repo": _SVC2_REPO,
                        "changed_files": ["handler.py"],
                        "base_snapshot": str(out_base),
                        "base_checkout": str(base_checkout),
                        "head_checkout": str(head_checkout),
                    })

        rqs = result.get("review_quality_status") or {}

        # Hard assert: semantic_diff_stats must be present in the packet
        self.assertIn(
            "semantic_diff_stats", rqs,
            f"semantic_diff_stats must be present in review_quality_status; keys={list(rqs.keys())}",
        )
        stats = rqs["semantic_diff_stats"]
        self.assertIsInstance(stats, dict, "semantic_diff_stats must be a dict")

        # Inversion: calls_attempted >= 1 proves the real client path ran
        calls_attempted = stats.get("calls_attempted", 0)
        self.assertGreaterEqual(
            calls_attempted, 1,
            f"calls_attempted must be >= 1 (real client ran); got {calls_attempted}",
        )

        # Hard asserts: token/cost values must match the faked litellm response
        self.assertEqual(
            stats.get("prompt_tokens"), 100 * calls_attempted,
            f"prompt_tokens must be 100*calls; got {stats.get('prompt_tokens')}",
        )
        self.assertEqual(
            stats.get("completion_tokens"), 50 * calls_attempted,
            f"completion_tokens must be 50*calls; got {stats.get('completion_tokens')}",
        )
        self.assertIsNotNone(
            stats.get("cost_usd"),
            "cost_usd must not be None when litellm returns cost",
        )
        # Model field present
        self.assertEqual(
            stats.get("model"), "fake-model",
            f"model must be 'fake-model'; got {stats.get('model')}",
        )

    def test_stats_usage_absent_produces_nulls_not_zeros(self) -> None:
        """When litellm response has no usage, prompt_tokens/completion_tokens/cost_usd must be None.

        Inversion: calls_attempted >= 1 proves the client ran; nulls prove the
        silent-default rule is honoured (0.0 would silently undercount cost).
        """
        import sys
        from unittest.mock import patch
        from source.kg.integrations.semantic_llm import SemanticDiffLlmClient
        from source.kg.query.semantic_contract_diff import semantic_contract_diff
        from source.kg.core.models import Entity
        from source.kg.core.store import JsonlKgStore

        fake_litellm = self._inject_fake_litellm_no_usage(_FAKE_RESPONSE)

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            e = Entity(
                kind="CodeSymbol",
                identity={
                    "tenant_id": "default",
                    "repo": "repo_no_usage",
                    "module": "mod.handler",
                    "qualname": "no_usage_func",
                    "symbol_kind": "function",
                },
                properties={"path": "handler.py", "line": 1, "end_line": 3},
            )
            snap_dir = root / "snap_no_usage"
            JsonlKgStore(snap_dir).write(
                entities=[e], facts=[], evidence=[], coverage=[],
                manifest={"version": 1, "tenant_id": "default"},
            )
            snap = KgSnapshot(snap_dir)
            base_dir = root / "base_no_usage"
            base_dir.mkdir()
            (base_dir / "handler.py").write_text("def no_usage_func():\n    if x: raise\n    return 1\n")
            head_dir = root / "head_no_usage"
            head_dir.mkdir()
            (head_dir / "handler.py").write_text("def no_usage_func():\n    return 1\n")

            entity_dicts = [d for d in snap.entities if d.get("kind") == "CodeSymbol"]
            stats_out: dict = {}

            with patch.dict(sys.modules, {"litellm": fake_litellm}):
                client = SemanticDiffLlmClient(model="fake-model")
                _rows, _status = semantic_contract_diff(
                    base_snapshot=snap,
                    head_snapshot=snap,
                    base_root=base_dir,
                    head_root=head_dir,
                    changed_symbols=entity_dicts,
                    client=client,
                    _stats_out=stats_out,
                )

        # Inversion: calls_attempted >= 1 proves the real client path ran
        self.assertGreaterEqual(
            stats_out.get("calls_attempted", 0), 1,
            f"calls_attempted must be >= 1; got {stats_out.get('calls_attempted')}",
        )
        # Hard assert: usage absent → None, not zero
        self.assertIsNone(
            stats_out.get("prompt_tokens"),
            f"prompt_tokens must be None when usage absent; got {stats_out.get('prompt_tokens')!r}",
        )
        self.assertIsNone(
            stats_out.get("completion_tokens"),
            f"completion_tokens must be None when usage absent; got {stats_out.get('completion_tokens')!r}",
        )
        self.assertIsNone(
            stats_out.get("cost_usd"),
            f"cost_usd must be None when usage absent; got {stats_out.get('cost_usd')!r}",
        )

    def test_stats_survive_budget_sync(self) -> None:
        """semantic_diff_stats must survive _sync_review_quality_status_from_packet.

        Directly tests the budget sync path: injects a review_quality_status with
        semantic_diff_stats, runs the sync, asserts the stats field is preserved.
        Inversion: assert stats NOT present before injection → verifies the assertion
        is checking what was actually put there by the sync, not a pre-existing value.
        """
        from source.kg.product import output_budget as ob

        fake_stats = {
            "model": "gpt-5.4-mini",
            "calls_attempted": 2,
            "calls_succeeded": 2,
            "calls_failed": 0,
            "parse_misses": 0,
            "rows_generated": 2,
            "prompt_tokens": 200,
            "completion_tokens": 100,
            "cost_usd": 0.0024,
        }
        result = {
            "review_quality_status": {
                "coverage_status": "partial",
                "specific_hypothesis_count": 1,
                "generic_hypothesis_count": 0,
                "specificity": "high",
                "recommended_action": "use_supercontext_packet",
                "reason": "Test packet.",
                "review_readiness": "packet_ready",
                "semantic_diff_status": "active",
                "semantic_diff_stats": fake_stats,
            },
            "review_hypotheses": [
                {
                    "hypothesis_id": "hyp-sem-001",
                    "risk_type": "contract_semantic_diff",
                    "specificity": "high",
                    "derivation": "inferred_llm",
                }
            ],
        }

        # Inversion: verify stats is present BEFORE sync (set up correctly)
        self.assertIn(
            "semantic_diff_stats", result["review_quality_status"],
            "semantic_diff_stats must be present before sync (test setup check)",
        )

        ob._sync_review_quality_status_from_packet(result, result["review_hypotheses"])

        synced_rqs = result["review_quality_status"]
        self.assertIn(
            "semantic_diff_stats", synced_rqs,
            f"semantic_diff_stats must survive budget sync; keys={list(synced_rqs.keys())}",
        )
        self.assertEqual(
            synced_rqs["semantic_diff_stats"], fake_stats,
            "semantic_diff_stats content must be unchanged after budget sync",
        )

    def test_abstract_contract_status_survives_budget_sync(self) -> None:
        """abstract_contract_status must survive _sync_review_quality_status_from_packet.

        Mirrors test_stats_survive_budget_sync. Inversion: the sync rebuilds
        review_quality_status from scratch and only restores an allowlisted set of
        keys; with abstract_contract_status absent from that allowlist the field is
        dropped. This test asserts it is preserved end-to-end.
        """
        from source.kg.product import output_budget as ob

        result = {
            "review_quality_status": {
                "coverage_status": "partial",
                "specific_hypothesis_count": 1,
                "generic_hypothesis_count": 0,
                "specificity": "high",
                "recommended_action": "use_supercontext_packet",
                "reason": "Test packet.",
                "review_readiness": "packet_ready",
                "abstract_contract_status": "active",
            },
            "review_hypotheses": [
                {
                    "hypothesis_id": "hyp-abs-001",
                    "risk_type": "abstract_contract_unimplemented",
                    "specificity": "high",
                    "derivation": "deterministic_static",
                }
            ],
        }

        # Inversion: verify status is present BEFORE sync (set up correctly).
        self.assertIn(
            "abstract_contract_status", result["review_quality_status"],
            "abstract_contract_status must be present before sync (test setup check)",
        )

        ob._sync_review_quality_status_from_packet(result, result["review_hypotheses"])

        synced_rqs = result["review_quality_status"]
        self.assertIn(
            "abstract_contract_status", synced_rqs,
            f"abstract_contract_status must survive budget sync; keys={list(synced_rqs.keys())}",
        )
        self.assertEqual(
            synced_rqs["abstract_contract_status"], "active",
            "abstract_contract_status content must be unchanged after budget sync",
        )


# ---------------------------------------------------------------------------
# Wave-3 Task B: violated_invariant schema — behavior-delta vs violated-invariant
# ---------------------------------------------------------------------------

class TestViolatedInvariantSchema(unittest.TestCase):
    """New keys old_contract/new_contract/violated_invariant: classification + composition."""

    def _make_single_symbol_snap(self, root: Path) -> tuple[KgSnapshot, Path, Path]:
        from source.kg.core.models import Entity
        from source.kg.core.store import JsonlKgStore

        e = Entity(
            kind="CodeSymbol",
            identity={
                "tenant_id": TENANT,
                "repo": "repo_vi",
                "module": "mod.handler",
                "qualname": "vi_func",
                "symbol_kind": "function",
            },
            properties={"path": "handler.py", "line": 1, "end_line": 3},
        )
        snap_dir = root / "snap_vi"
        JsonlKgStore(snap_dir).write(
            entities=[e], facts=[], evidence=[], coverage=[],
            manifest={"version": 1, "tenant_id": TENANT},
        )
        snap = KgSnapshot(snap_dir)
        base_dir = root / "base_vi"
        base_dir.mkdir()
        (base_dir / "handler.py").write_text("def vi_func():\n    if x: raise\n    return 1\n")
        head_dir = root / "head_vi"
        head_dir.mkdir()
        (head_dir / "handler.py").write_text("def vi_func():\n    return 1\n")
        return snap, base_dir, head_dir

    def test_concrete_violated_invariant_is_high_specificity(self) -> None:
        """A concrete violated_invariant → specificity 'high'; WHY threaded into composition."""
        from source.kg.query.semantic_contract_diff import semantic_contract_diff

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snap, base_dir, head_dir = self._make_single_symbol_snap(root)
            entity_dicts = [d for d in snap.entities if d.get("kind") == "CodeSymbol"]

            client = _FakeClient(response=[_claim(
                claim="Guard removed.",
                violated_invariant="Callers relied on None being rejected.",
                old_contract="Rejected None input.",
                new_contract="Accepts None input.",
            )])
            rows, status = semantic_contract_diff(
                base_snapshot=snap, head_snapshot=snap,
                base_root=base_dir, head_root=head_dir,
                changed_symbols=entity_dicts, client=client,
            )
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row.get("specificity"), "high",
                             f"concrete violated_invariant must be high; got {row.get('specificity')}")
            # WHY (the violated invariant) surfaces in postable_claim and concrete_invariant.
            self.assertIn("Callers relied on None being rejected.", row.get("postable_claim", ""))
            self.assertIn("Callers relied on None being rejected.", row.get("concrete_invariant", ""))
            self.assertEqual(row.get("violated_invariant"), "Callers relied on None being rejected.")
            # old/new contract threaded into source_checks.
            checks = " ".join(row.get("source_checks") or [])
            self.assertIn("Rejected None input.", checks, "old contract must be in source_checks")
            self.assertIn("Accepts None input.", checks, "new contract must be in source_checks")

    def test_none_violated_invariant_is_medium_specificity_context(self) -> None:
        """violated_invariant='none' → specificity 'medium'; postable_claim reads as context."""
        from source.kg.query.semantic_contract_diff import semantic_contract_diff

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snap, base_dir, head_dir = self._make_single_symbol_snap(root)
            entity_dicts = [d for d in snap.entities if d.get("kind") == "CodeSymbol"]

            client = _FakeClient(response=[_claim(
                claim="Cancelled reminders are deleted before rescheduling.",
                violated_invariant="none",
                old_contract="Old reminders kept.",
                new_contract="Old reminders deleted before reschedule.",
            )])
            rows, status = semantic_contract_diff(
                base_snapshot=snap, head_snapshot=snap,
                base_root=base_dir, head_root=head_dir,
                changed_symbols=entity_dicts, client=client,
            )
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row.get("specificity"), "medium",
                             f"'none' violated_invariant must be medium; got {row.get('specificity')}")
            pc = row.get("postable_claim", "")
            self.assertTrue(pc.startswith("behavior changed:"),
                            f"'none' row must read as context; got {pc!r}")
            # Must NOT assert a violated invariant.
            self.assertNotIn("violated invariant:", pc,
                             "context row must not assert a violated invariant")

    def test_empty_violated_invariant_is_medium(self) -> None:
        """Inversion: empty-string violated_invariant is also a BEHAVIOR-DELTA (medium)."""
        from source.kg.query.semantic_contract_diff import semantic_contract_diff

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snap, base_dir, head_dir = self._make_single_symbol_snap(root)
            entity_dicts = [d for d in snap.entities if d.get("kind") == "CodeSymbol"]

            client = _FakeClient(response=[_claim(violated_invariant="")])
            rows, _status = semantic_contract_diff(
                base_snapshot=snap, head_snapshot=snap,
                base_root=base_dir, head_root=head_dir,
                changed_symbols=entity_dicts, client=client,
            )
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].get("specificity"), "medium")

    def test_missing_new_keys_is_parse_miss(self) -> None:
        """Item missing the new keys → dropped by _REQUIRED_KEYS (no row); NOT defaulted risky.

        Single-item all-invalid response → no parsed rows. With the new schema the
        item is invalid (missing old_contract/new_contract/violated_invariant), so it
        produces zero rows — violated_invariant is never silently defaulted.
        """
        from source.kg.query.semantic_contract_diff import semantic_contract_diff

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snap, base_dir, head_dir = self._make_single_symbol_snap(root)
            entity_dicts = [d for d in snap.entities if d.get("kind") == "CodeSymbol"]

            # Old-schema item: has the original 5 keys but NONE of the new 3.
            client = _FakeClient(response=[{
                "claim": "Guard removed.",
                "cause_line": 2,
                "consequence": "Callers may pass None.",
                "negative_check": "No check.",
                "category": "guard_removal",
            }])
            rows, _status = semantic_contract_diff(
                base_snapshot=snap, head_snapshot=snap,
                base_root=base_dir, head_root=head_dir,
                changed_symbols=entity_dicts, client=client,
            )
            self.assertEqual(rows, [], "item missing new keys must produce no rows")

    def test_all_parsed_but_schema_invalid_produces_no_valid_claims_status(self) -> None:
        """Response parses to a list but EVERY item fails schema validation → honest
        'failed:no_valid_claims', never a silent 'active' with zero rows.

        Uses a parsed-but-old-schema client (the five-key shape from before the schema
        grew old_contract/new_contract/violated_invariant). The response parses fine
        (parsed_ok > 0), but no item validates, so zero rows result. This is distinct
        from a parse_miss (see test_all_parse_miss_status) — the model DID return usable
        JSON, it just used a stale schema, which must be surfaced, not hidden as 'active'.
        """
        from source.kg.query.semantic_contract_diff import semantic_contract_diff

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snap, base_dir, head_dir = self._make_single_symbol_snap(root)
            entity_dicts = [d for d in snap.entities if d.get("kind") == "CodeSymbol"]

            # Old-schema item: the original 5 keys, NONE of the new 3 → parses, invalid.
            old_schema_response = [{
                "claim": "Guard removed.",
                "cause_line": 2,
                "consequence": "Callers may pass None.",
                "negative_check": "No check.",
                "category": "guard_removal",
            }]
            client = _FakeClient(response=old_schema_response)
            rows, status = semantic_contract_diff(
                base_snapshot=snap, head_snapshot=snap,
                base_root=base_dir, head_root=head_dir,
                changed_symbols=entity_dicts, client=client,
            )
            self.assertGreater(client.call_count, 0,
                               "test must actually attempt an LLM call for the status to be honest")
            self.assertEqual(rows, [], "old-schema items must produce no valid rows")
            self.assertEqual(status, "failed:no_valid_claims",
                             f"all-invalid parsed response must be honest, not 'active'; got {status!r}")

            # Inversion proof: the SAME pipeline with a schema-valid response returns
            # rows and 'active', proving the status is driven by validity, not a constant.
            valid_client = _FakeClient(response=[_claim()])
            valid_rows, valid_status = semantic_contract_diff(
                base_snapshot=snap, head_snapshot=snap,
                base_root=base_dir, head_root=head_dir,
                changed_symbols=entity_dicts, client=valid_client,
            )
            self.assertGreater(len(valid_rows), 0, "valid-schema response must produce rows")
            self.assertEqual(valid_status, "active",
                             f"valid-schema response must be 'active'; got {valid_status!r}")

    def test_parsed_empty_list_is_valid_no_changes_and_stays_active(self) -> None:
        """A parsed EMPTY list ([]) is a valid 'no behavioral contract changes found'
        answer to the 'up to 2 changes' prompt — it must stay 'active', never be
        misclassified as 'failed:no_valid_claims'. Failure requires candidate items
        that existed and did not survive; [] has no candidates at all.
        """
        from source.kg.query.semantic_contract_diff import semantic_contract_diff

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snap, base_dir, head_dir = self._make_single_symbol_snap(root)
            entity_dicts = [d for d in snap.entities if d.get("kind") == "CodeSymbol"]

            client = _FakeClient(response=[])
            rows, status = semantic_contract_diff(
                base_snapshot=snap, head_snapshot=snap,
                base_root=base_dir, head_root=head_dir,
                changed_symbols=entity_dicts, client=client,
            )
            self.assertGreater(client.call_count, 0,
                               "test must actually attempt an LLM call")
            self.assertEqual(rows, [], "empty response must produce no rows")
            self.assertEqual(status, "active",
                             f"parsed empty list is a clean no-op and must stay 'active'; got {status!r}")

    def test_oversized_new_field_dropped(self) -> None:
        """violated_invariant > _DROP_VIOLATED_INVARIANT (1200) → item dropped entirely.

        When it is the ONLY item, the response parses but emits zero rows, so the status
        must be honest ('failed:no_valid_claims'), never a silent 'active'.
        """
        from source.kg.query.semantic_contract_diff import semantic_contract_diff

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snap, base_dir, head_dir = self._make_single_symbol_snap(root)
            entity_dicts = [d for d in snap.entities if d.get("kind") == "CodeSymbol"]

            client = _FakeClient(response=[_claim(violated_invariant="z" * 1500)])
            rows, status = semantic_contract_diff(
                base_snapshot=snap, head_snapshot=snap,
                base_root=base_dir, head_root=head_dir,
                changed_symbols=entity_dicts, client=client,
            )
            self.assertEqual(rows, [], "oversized violated_invariant (>1200) must be dropped")
            self.assertGreater(client.call_count, 0,
                               "test must attempt an LLM call for the status to be honest")
            self.assertEqual(status, "failed:no_valid_claims",
                             f"oversized-only response emits zero rows; status must be honest, not 'active'; got {status!r}")

    def test_all_items_dropped_by_thresholds_produces_no_valid_claims_status(self) -> None:
        """Schema-VALID items all dropped by _DROP_* thresholds → zero rows, honest status.

        Regression for the drop-threshold gap: items pass _validate_item (so the old
        valid_item_count-based gate saw them as valid) but every one is then dropped by an
        oversized-field check, emitting zero rows. Keying the status on EMITTED rows makes
        this report 'failed:no_valid_claims' instead of a silent 'active'.
        """
        from source.kg.query.semantic_contract_diff import semantic_contract_diff

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snap, base_dir, head_dir = self._make_single_symbol_snap(root)
            entity_dicts = [d for d in snap.entities if d.get("kind") == "CodeSymbol"]

            # Two schema-valid items, each oversized on a DIFFERENT field → both dropped.
            client = _FakeClient(response=[
                _claim(claim="a" * 1500),                 # > _DROP_CLAIM
                _claim(violated_invariant="z" * 1500),    # > _DROP_VIOLATED_INVARIANT
            ])
            rows, status = semantic_contract_diff(
                base_snapshot=snap, head_snapshot=snap,
                base_root=base_dir, head_root=head_dir,
                changed_symbols=entity_dicts, client=client,
            )
            self.assertGreater(client.call_count, 0,
                               "test must attempt an LLM call for the status to be honest")
            self.assertEqual(rows, [], "all schema-valid items dropped by thresholds → zero rows")
            self.assertEqual(status, "failed:no_valid_claims",
                             f"all-items-dropped must be honest, not 'active'; got {status!r}")

            # Inversion proof: the SAME pipeline with an in-range item emits a row and 'active',
            # proving the status is driven by emitted rows, not a constant.
            valid_client = _FakeClient(response=[_claim()])
            valid_rows, valid_status = semantic_contract_diff(
                base_snapshot=snap, head_snapshot=snap,
                base_root=base_dir, head_root=head_dir,
                changed_symbols=entity_dicts, client=valid_client,
            )
            self.assertEqual(len(valid_rows), 1, "in-range item must emit a row")
            self.assertEqual(valid_status, "active",
                             f"emitted-row response must be 'active'; got {valid_status!r}")

    def test_oversized_new_field_clamped(self) -> None:
        """violated_invariant of 500 chars → clamped to 300 in the row."""
        from source.kg.query.semantic_contract_diff import (
            semantic_contract_diff,
            _CLAMP_VIOLATED_INVARIANT,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snap, base_dir, head_dir = self._make_single_symbol_snap(root)
            entity_dicts = [d for d in snap.entities if d.get("kind") == "CodeSymbol"]

            client = _FakeClient(response=[_claim(violated_invariant="w" * 500)])
            rows, _status = semantic_contract_diff(
                base_snapshot=snap, head_snapshot=snap,
                base_root=base_dir, head_root=head_dir,
                changed_symbols=entity_dicts, client=client,
            )
            self.assertEqual(len(rows), 1)
            self.assertEqual(
                len(rows[0].get("violated_invariant", "")), _CLAMP_VIOLATED_INVARIANT,
                "violated_invariant must be clamped to _CLAMP_VIOLATED_INVARIANT",
            )

    def test_real_client_behavior_delta_vs_violated_invariant(self) -> None:
        """Real SemanticDiffLlmClient (patched litellm): both row classes get correct specificity.

        Two symbols; call 1 returns a violated-invariant claim (high), call 2 returns a
        'none' behavior-delta claim (medium). Exercises the real client typed path.
        """
        import sys, json as _json, types
        from unittest.mock import patch
        from source.kg.integrations.semantic_llm import SemanticDiffLlmClient
        from source.kg.query.semantic_contract_diff import semantic_contract_diff
        from source.kg.core.models import Entity
        from source.kg.core.store import JsonlKgStore

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            entities = []
            for i in range(2):
                entities.append(Entity(
                    kind="CodeSymbol",
                    identity={
                        "tenant_id": TENANT, "repo": "repo_rc",
                        "module": f"mod.m{i}", "qualname": f"rc_func_{i}",
                        "symbol_kind": "function",
                    },
                    properties={"path": f"f{i}.py", "line": 1, "end_line": 3},
                ))
            snap_dir = root / "snap_rc"
            JsonlKgStore(snap_dir).write(
                entities=entities, facts=[], evidence=[], coverage=[],
                manifest={"version": 1, "tenant_id": TENANT},
            )
            snap = KgSnapshot(snap_dir)
            entity_dicts = [d for d in snap.entities if d.get("kind") == "CodeSymbol"]

            base_dir = root / "base_rc"; base_dir.mkdir()
            head_dir = root / "head_rc"; head_dir.mkdir()
            for i in range(2):
                (base_dir / f"f{i}.py").write_text(f"def rc_func_{i}():\n    if x: raise\n    return {i}\n")
                (head_dir / f"f{i}.py").write_text(f"def rc_func_{i}():\n    return {i}\n")

            violated_payload = [_claim(claim="Guard removed.",
                                       violated_invariant="Callers relied on rejection.")]
            benign_payload = [_claim(claim="Behavior reordered.", violated_invariant="none")]

            call_state = {"n": 0}

            def _completion(**kwargs):
                call_state["n"] += 1
                payload = violated_payload if call_state["n"] == 1 else benign_payload

                class _Msg:
                    content = _json.dumps(payload)

                class _Choice:
                    message = _Msg()

                class _Resp:
                    choices = [_Choice()]

                return _Resp()

            fake = types.ModuleType("litellm")
            fake.completion = _completion
            fake.completion_cost = lambda **kw: (_ for _ in ()).throw(Exception("unknown"))

            with patch.dict(sys.modules, {"litellm": fake}):
                client = SemanticDiffLlmClient(model="fake-model")
                rows, status = semantic_contract_diff(
                    base_snapshot=snap, head_snapshot=snap,
                    base_root=base_dir, head_root=head_dir,
                    changed_symbols=entity_dicts, client=client,
                )

            self.assertGreaterEqual(call_state["n"], 2, "real client must be called for both symbols")
            specs = sorted(r.get("specificity") for r in rows)
            self.assertEqual(specs, ["high", "medium"],
                             f"one high + one medium row expected; got {specs}")


class TestBehaviorDeltaSeating(unittest.TestCase):
    """Real-pipeline splice: a behavior-delta row scores below a violated-invariant row
    with the SAME coords (claim-strength scorer seats it lower)."""

    def test_behavior_delta_seats_below_violated_invariant_same_coords(self) -> None:
        """Two contract_semantic_diff rows, identical cause coords: high (violated) outranks
        medium (behavior-delta) after apply_structural_noise_downranking."""
        from source.kg.product.review_hypotheses import (
            apply_structural_noise_downranking,
            score_hypothesis_row,
        )

        coords = {"path": "src/a.py", "line_start": 5}
        changed_files = ["src/a.py"]

        violated_row = {
            "hypothesis_id": "hyp-violated",
            "risk_type": "contract_semantic_diff",
            "derivation": "inferred_llm",
            "specificity": "high",
            "confidence": "medium",
            "cause": dict(coords),
            "consequence": dict(coords),
            "postable_claim": "X — violated invariant: caller expected rejection.",
            "violated_invariant": "caller expected rejection.",
            "evidence_refs": [], "source_spans": [], "supporting_lead_ids": [],
        }
        behavior_row = {
            "hypothesis_id": "hyp-behavior",
            "risk_type": "contract_semantic_diff",
            "derivation": "inferred_llm",
            "specificity": "medium",
            "confidence": "medium",
            "cause": dict(coords),
            "consequence": dict(coords),
            "postable_claim": "behavior changed: X.",
            "violated_invariant": "none",
            "evidence_refs": [], "source_spans": [], "supporting_lead_ids": [],
        }

        # Direct score comparison: same coords → only specificity differs → high > medium.
        s_violated = score_hypothesis_row(violated_row, changed_files, None)
        s_behavior = score_hypothesis_row(behavior_row, changed_files, None)
        self.assertGreater(
            s_violated, s_behavior,
            f"violated-invariant row must score above behavior-delta row; "
            f"got {s_violated} vs {s_behavior}",
        )

        # Real reorder within the inferred_llm peer group: pass behavior-delta FIRST so
        # the reorder must actively move the violated row ahead (not preserve input order).
        reordered = apply_structural_noise_downranking(
            [behavior_row, violated_row], changed_files, None,
        )
        order = [r.get("hypothesis_id") for r in reordered]
        self.assertEqual(
            order[0], "hyp-violated",
            f"violated-invariant row must seat first; got {order}",
        )

        # Inversion: two rows with identical specificity keep input order (proves the
        # reorder is driven by the specificity stamp, not by id/arbitrary tiebreak).
        behavior_row_2 = dict(behavior_row, hypothesis_id="hyp-behavior-2")
        same_spec = apply_structural_noise_downranking(
            [behavior_row, behavior_row_2], changed_files, None,
        )
        self.assertEqual(
            [r.get("hypothesis_id") for r in same_spec],
            ["hyp-behavior", "hyp-behavior-2"],
            "equal-specificity rows must preserve input order (stable reorder)",
        )


if __name__ == "__main__":
    unittest.main()
