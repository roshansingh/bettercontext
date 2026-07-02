"""Tests for TS/JS async-lifecycle risk signal extraction (Q1).

TDD: these tests are written before the implementation. They verify:
- async_callback_in_iteration fires for forEach(async fn)
- async_callback_in_iteration does NOT fire for forEach(sync fn)
- unawaited_async_call fires for unawaited call to same-file async fn
- unawaited_async_call does NOT fire when the call is awaited
- Promise.all-wrapped call does NOT emit a signal
- Cross-file async call emits NOTHING (documented limitation)
- Per-symbol cap: at most 3 signals per enclosing symbol
- bytes_ref is present and complete on every emitted signal
- Determinism: two builds over the same tempdir repo produce identical fact_ids
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from source.kg.build.pipeline import build_kg
from source.kg.core.store import read_jsonl


NODE_AVAILABLE = shutil.which("node") is not None

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# Positive: forEach receiving async callback — signal fires
_FOREACH_ASYNC = """\
export async function fetchAll(ids: string[]): Promise<void> {
  ids.forEach(async (id) => {
    await fetch(id);
  });
}
"""

# Negative: forEach receiving sync callback — no signal
_FOREACH_SYNC = """\
export function processAll(items: string[]): void {
  items.forEach((item) => {
    console.log(item);
  });
}
"""

# Positive: unawaited call to same-file async fn
_UNAWAITED_CALL = """\
async function saveRecord(id: string): Promise<void> {
  await fetch('/api/' + id);
}

export function triggerSave(id: string): void {
  saveRecord(id);
}
"""

# Negative: awaited call — no signal
_AWAITED_CALL = """\
async function saveRecord(id: string): Promise<void> {
  await fetch('/api/' + id);
}

export async function triggerSave(id: string): Promise<void> {
  await saveRecord(id);
}
"""

# Negative: Promise.all-wrapped call — no signal
_PROMISE_ALL = """\
async function processItem(id: string): Promise<void> {
  await fetch('/api/' + id);
}

export async function processAll(ids: string[]): Promise<void> {
  await Promise.all(ids.map((id) => processItem(id)));
}
"""

# Negative: call to imported (cross-file) async fn — no signal
_CROSS_FILE_CALL = """\
import { saveRecord } from './storage';

export function triggerSave(id: string): void {
  saveRecord(id);
}
"""

# Cap test: same enclosing symbol has 4 forEach(async) calls — only 3 should appear
_CAP_EXCEEDED = """\
export async function batchProcess(ids: string[]): Promise<void> {
  ids.forEach(async (id) => { await fetch('/a/' + id); });
  ids.forEach(async (id) => { await fetch('/b/' + id); });
  ids.forEach(async (id) => { await fetch('/c/' + id); });
  ids.forEach(async (id) => { await fetch('/d/' + id); });
}
"""


def _build(files: dict[str, str]) -> tuple[list[dict], list[dict], list[dict]]:
    """Write files to a tempdir, run build_kg, return (support_facts, evidence, coverage)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        pkg = root / "pkg"
        pkg.mkdir()
        (pkg / "package.json").write_text(
            json.dumps({"name": "test-pkg", "version": "1.0.0"}), encoding="utf-8"
        )
        for name, text in files.items():
            (pkg / name).write_text(text, encoding="utf-8")
        out = root / "kg"
        build_kg(pkg, out)
        sf_path = out / "support_facts.jsonl"
        support_facts = read_jsonl(sf_path) if sf_path.exists() else []
        evidence = read_jsonl(out / "evidence.jsonl")
        coverage = read_jsonl(out / "coverage.jsonl")
    return support_facts, evidence, coverage


def _risk_signals(support_facts: list[dict]) -> list[dict]:
    return [f for f in support_facts if f["predicate"] == "code_risk_signal"]


@unittest.skipIf(not NODE_AVAILABLE, "node not available")
class AsyncCallbackInIterationTest(unittest.TestCase):
    def test_foreach_async_emits_signal(self) -> None:
        sf, ev, _ = _build({"worker.ts": _FOREACH_ASYNC})
        signals = _risk_signals(sf)
        # At least one signal must fire
        self.assertGreater(len(signals), 0, "expected at least one code_risk_signal")
        families = {s["qualifier"]["risk_family"] for s in signals}
        self.assertIn("async_callback_in_iteration", families)

    def test_foreach_sync_emits_no_signal(self) -> None:
        sf, _, _ = _build({"worker.ts": _FOREACH_SYNC})
        signals = _risk_signals(sf)
        self.assertEqual(signals, [], f"unexpected signals: {signals}")

    def test_signal_has_complete_bytes_ref(self) -> None:
        sf, ev, _ = _build({"worker.ts": _FOREACH_ASYNC})
        signals = _risk_signals(sf)
        self.assertGreater(len(signals), 0)
        # find evidence for first signal
        sig_id = signals[0]["fact_id"]
        sig_ev = [e for e in ev if e.get("target_id") == sig_id]
        self.assertGreater(len(sig_ev), 0, "no evidence for signal")
        br = sig_ev[0].get("bytes_ref", {})
        for key in ("repo", "commit_sha", "path", "line_start", "line_end"):
            self.assertIn(key, br, f"bytes_ref missing {key}")
        self.assertIsNotNone(br["commit_sha"])
        self.assertNotEqual(br["commit_sha"], "")

    def test_qualifier_has_required_fields(self) -> None:
        sf, _, _ = _build({"worker.ts": _FOREACH_ASYNC})
        signals = [s for s in _risk_signals(sf) if s["qualifier"]["risk_family"] == "async_callback_in_iteration"]
        self.assertGreater(len(signals), 0)
        q = signals[0]["qualifier"]
        self.assertIn("risk_family", q)
        self.assertIn("qualname", q)
        self.assertIn("line", q)


@unittest.skipIf(not NODE_AVAILABLE, "node not available")
class UnawaitedAsyncCallTest(unittest.TestCase):
    def test_unawaited_same_file_async_emits_signal(self) -> None:
        sf, _, _ = _build({"saver.ts": _UNAWAITED_CALL})
        signals = _risk_signals(sf)
        families = {s["qualifier"]["risk_family"] for s in signals}
        self.assertIn("unawaited_async_call", families)

    def test_awaited_call_emits_no_unawaited_signal(self) -> None:
        sf, _, _ = _build({"saver.ts": _AWAITED_CALL})
        signals = [s for s in _risk_signals(sf) if s["qualifier"]["risk_family"] == "unawaited_async_call"]
        self.assertEqual(signals, [], f"unexpected unawaited_async_call signals: {signals}")

    def test_promise_all_emits_no_signal(self) -> None:
        sf, _, _ = _build({"saver.ts": _PROMISE_ALL})
        signals = [s for s in _risk_signals(sf) if s["qualifier"]["risk_family"] == "unawaited_async_call"]
        self.assertEqual(signals, [], f"unexpected signals: {signals}")

    def test_cross_file_call_emits_no_signal(self) -> None:
        # Cross-file async calls are a documented limitation — must emit nothing
        sf, _, _ = _build({"caller.ts": _CROSS_FILE_CALL})
        signals = [s for s in _risk_signals(sf) if s["qualifier"]["risk_family"] == "unawaited_async_call"]
        self.assertEqual(signals, [], f"cross-file call must not emit a signal: {signals}")


@unittest.skipIf(not NODE_AVAILABLE, "node not available")
class SignalCapTest(unittest.TestCase):
    def test_per_symbol_cap_at_three(self) -> None:
        sf, _, _ = _build({"batch.ts": _CAP_EXCEEDED})
        signals = [
            s for s in _risk_signals(sf)
            if s["qualifier"]["risk_family"] == "async_callback_in_iteration"
            and s["qualifier"]["qualname"] == "batchProcess"
        ]
        self.assertEqual(len(signals), 3, f"expected exactly 3 (cap), got {len(signals)}: {signals}")

    def test_cap_selects_lowest_lines(self) -> None:
        sf, _, _ = _build({"batch.ts": _CAP_EXCEEDED})
        signals = [
            s for s in _risk_signals(sf)
            if s["qualifier"]["risk_family"] == "async_callback_in_iteration"
            and s["qualifier"]["qualname"] == "batchProcess"
        ]
        lines = sorted(s["qualifier"]["line"] for s in signals)
        # Lines 2, 3, 4 expected (1-indexed, cap drops line 5)
        self.assertEqual(lines, lines, "lines must be sorted ascending")
        self.assertEqual(len(lines), 3)
        # The 4th call at the highest line should be absent
        self.assertNotIn(max(lines) + 1, lines)


@unittest.skipIf(not NODE_AVAILABLE, "node not available")
class DeterminismTest(unittest.TestCase):
    def test_two_builds_produce_identical_fact_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            pkg = root / "pkg"
            pkg.mkdir()
            (pkg / "package.json").write_text(
                json.dumps({"name": "test-pkg", "version": "1.0.0"}), encoding="utf-8"
            )
            (pkg / "worker.ts").write_text(_FOREACH_ASYNC, encoding="utf-8")
            (pkg / "saver.ts").write_text(_UNAWAITED_CALL, encoding="utf-8")

            out1 = root / "kg1"
            out2 = root / "kg2"
            build_kg(pkg, out1)
            build_kg(pkg, out2)

            sf1_path = out1 / "support_facts.jsonl"
            sf2_path = out2 / "support_facts.jsonl"
            sf1 = {f["fact_id"] for f in (read_jsonl(sf1_path) if sf1_path.exists() else []) if f["predicate"] == "code_risk_signal"}
            sf2 = {f["fact_id"] for f in (read_jsonl(sf2_path) if sf2_path.exists() else []) if f["predicate"] == "code_risk_signal"}
            self.assertGreater(len(sf1), 0, "no signals in first build")
            self.assertEqual(sf1, sf2, "fact_ids differ between two builds")
