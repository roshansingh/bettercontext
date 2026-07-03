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

# Negative: Promise.all with parenthesized arrow body — must NOT fire
# ids.map(id => (processItem(id))) — the paren wraps the call inside the arrow
_PROMISE_ALL_PAREN_ARROW = """\
async function processItem(id: string): Promise<void> {
  await fetch('/api/' + id);
}

export async function processAll(ids: string[]): Promise<void> {
  await Promise.all(ids.map((id) => (processItem(id))));
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

# Cap test (mixed families): 2 forEach(async) + 2 unawaited calls — only 3 combined
# saveRecord is declared async at module level so unawaited_async_call fires for it.
_CAP_MIXED = """\
async function saveRecord(id: string): Promise<void> {
  await fetch('/api/' + id);
}

export async function mixedWork(ids: string[]): Promise<void> {
  ids.forEach(async (id) => { await fetch('/a/' + id); });
  ids.forEach(async (id) => { await fetch('/b/' + id); });
  saveRecord('x');
  saveRecord('y');
}
"""

# Negative: void operator — intentional discard, must not fire unawaited_async_call
_VOID_DISCARD = """\
async function save(id: string): Promise<void> {
  await fetch('/api/' + id);
}

export function triggerSave(id: string): void {
  void save(id);
}
"""

# Negative: void with parenthesized call — void (save(id)) — must NOT fire
_VOID_DISCARD_PAREN = """\
async function saveRecord(id: string): Promise<void> {
  await fetch('/api/' + id);
}

export function triggerSave(id: string): void {
  void (saveRecord(id));
}
"""

# Negative (for isAssignedContext fix): call on RHS of strict-equality — must still fire
_COMPARISON_CALL = """\
async function saveRecord(id: string): Promise<void> {
  await fetch('/api/' + id);
}

export function checkSave(id: string): boolean {
  return ready === saveRecord(id);
}
"""

# Negative (for isAssignedContext fix): call in logical OR — must still fire
_LOGICAL_OR_CALL = """\
async function saveRecord(id: string): Promise<void> {
  await fetch('/api/' + id);
}

export function maybeSave(id: string): void {
  const result = flag || saveRecord(id);
}
"""

# Positive (control): plain assignment suppresses — must NOT fire
_ASSIGNMENT_SUPPRESSED = """\
async function saveRecord(id: string): Promise<void> {
  await fetch('/api/' + id);
}

export function triggerSave(id: string): void {
  const rec = saveRecord(id);
}
"""

# Negative (parenthesized await): await (saveRecord(id)) — must NOT fire
_PAREN_AWAIT = """\
async function saveRecord(id: string): Promise<void> {
  await fetch('/api/' + id);
}

export async function triggerSave(id: string): Promise<void> {
  await (saveRecord(id));
}
"""

# Negative (parenthesized return): return (saveRecord(id)) — must NOT fire
_PAREN_RETURN = """\
async function saveRecord(id: string): Promise<void> {
  await fetch('/api/' + id);
}

export async function triggerSave(id: string): Promise<Promise<void>> {
  return (saveRecord(id));
}
"""

# Negative (parenthesized then-chain): (saveRecord(id)).then(() => {}) — must NOT fire
_PAREN_THEN = """\
async function saveRecord(id: string): Promise<void> {
  await fetch('/api/' + id);
}

export function triggerSave(id: string): void {
  (saveRecord(id)).then(() => {});
}
"""

# Negative (parenthesized assignment): const x = (saveRecord(id)) — must NOT fire
_PAREN_ASSIGN = """\
async function saveRecord(id: string): Promise<void> {
  await fetch('/api/' + id);
}

export function triggerSave(id: string): void {
  const p = (saveRecord(id));
}
"""

# Negative (double-paren await): await ((saveRecord(id))) — must NOT fire
_DOUBLE_PAREN_AWAIT = """\
async function saveRecord(id: string): Promise<void> {
  await fetch('/api/' + id);
}

export async function triggerSave(id: string): Promise<void> {
  await ((saveRecord(id)));
}
"""

# Positive: bare (saveRecord(id)); as expression statement — IS unawaited, MUST fire
_PAREN_BARE_STATEMENT = """\
async function saveRecord(id: string): Promise<void> {
  await fetch('/api/' + id);
}

export function triggerSave(id: string): void {
  (saveRecord(id));
}
"""

# Root cause B: signal inside a class method — subject entity_id must match main extractor
_CLASS_METHOD_UNAWAITED = """\
async function saveRecord(id: string): Promise<void> {
  await fetch('/api/' + id);
}

export class Saver {
  trigger(id: string): void {
    saveRecord(id);
  }
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

    def test_void_discard_emits_no_signal(self) -> None:
        # void save(id) is an intentional discard — must not fire unawaited_async_call
        sf, _, _ = _build({"discard.ts": _VOID_DISCARD})
        signals = [s for s in _risk_signals(sf) if s["qualifier"]["risk_family"] == "unawaited_async_call"]
        self.assertEqual(signals, [], f"void discard must not emit unawaited_async_call: {signals}")

    def test_void_discard_paren_emits_no_signal(self) -> None:
        # void (saveRecord(id)) — paren-wrapped void discard — must not fire
        sf, _, _ = _build({"discard.ts": _VOID_DISCARD_PAREN})
        signals = [s for s in _risk_signals(sf) if s["qualifier"]["risk_family"] == "unawaited_async_call"]
        self.assertEqual(signals, [], f"void (f()) must not emit unawaited_async_call: {signals}")

    def test_promise_all_paren_arrow_emits_no_signal(self) -> None:
        # await Promise.all(ids.map(id => (processItem(id)))) — paren in arrow body — must not fire
        sf, _, _ = _build({"saver.ts": _PROMISE_ALL_PAREN_ARROW})
        signals = [s for s in _risk_signals(sf) if s["qualifier"]["risk_family"] == "unawaited_async_call"]
        self.assertEqual(signals, [], f"Promise.all with paren arrow must not emit unawaited_async_call: {signals}")

    def test_promise_all_paren_argument_emits_no_signal(self) -> None:
        # await Promise.all((ids.map(id => processItem(id)))) — paren AROUND the map
        # call, mid-walk — parens must be transparent anywhere in the upward walk
        source = _PROMISE_ALL.replace(
            "await Promise.all(ids.map((id) => processItem(id)));",
            "await Promise.all((ids.map((id) => processItem(id))));",
        )
        self.assertIn("Promise.all((ids.map", source)
        sf, _, _ = _build({"saver.ts": source})
        signals = [s for s in _risk_signals(sf) if s["qualifier"]["risk_family"] == "unawaited_async_call"]
        self.assertEqual(signals, [], f"Promise.all((...map(...))) must not emit unawaited_async_call: {signals}")


@unittest.skipIf(not NODE_AVAILABLE, "node not available")
class SignalCapTest(unittest.TestCase):
    def test_file_cap_allows_all_four_from_one_symbol(self) -> None:
        # _CAP_EXCEEDED has 4 forEach(async) in one symbol; file cap is 20 → all 4 emitted.
        sf, _, _ = _build({"batch.ts": _CAP_EXCEEDED})
        signals = [
            s for s in _risk_signals(sf)
            if s["qualifier"]["risk_family"] == "async_callback_in_iteration"
            and s["qualifier"]["qualname"] == "batchProcess"
        ]
        self.assertEqual(len(signals), 4, f"expected all 4 (file cap=20), got {len(signals)}: {signals}")

    def test_all_four_lines_present_under_file_cap(self) -> None:
        sf, _, _ = _build({"batch.ts": _CAP_EXCEEDED})
        signals = [
            s for s in _risk_signals(sf)
            if s["qualifier"]["risk_family"] == "async_callback_in_iteration"
            and s["qualifier"]["qualname"] == "batchProcess"
        ]
        lines = sorted(s["qualifier"]["line"] for s in signals)
        # _CAP_EXCEEDED: 4 forEach(async) calls; all 4 lines must appear under file cap of 20.
        self.assertEqual(len(lines), 4, f"expected 4 lines, got {lines}")
        # Inversion: previously line 5 was dropped by the old per-symbol cap of 3.
        self.assertIn(5, lines, "line 5 (4th forEach) must now be present under file cap")

    def test_all_four_signals_present_across_families(self) -> None:
        # 2 async_callback_in_iteration + 2 unawaited_async_call = 4 total; file cap=20 → all 4 present.
        sf, _, _ = _build({"mixed.ts": _CAP_MIXED})
        signals = [
            s for s in _risk_signals(sf)
            if s["qualifier"]["qualname"] == "mixedWork"
        ]
        self.assertEqual(
            len(signals), 4,
            f"expected 4 combined (file cap=20 allows all), got {len(signals)}: {signals}",
        )
        families_present = {s["qualifier"]["risk_family"] for s in signals}
        self.assertGreater(len(families_present), 0, "no signals at all")

    def test_file_level_cap_at_twenty(self) -> None:
        # 21 forEach(async) calls in one file — file cap is 20, so exactly 20 emitted.
        lines_code = "\n".join(
            f"  ids.forEach(async (id) => {{ await fetch('/{i}/' + id); }});"
            for i in range(21)
        )
        source = f"export async function bigRouter(ids: string[]): Promise<void> {{\n{lines_code}\n}}\n"
        sf, _, _ = _build({"router.ts": source})
        signals = [
            s for s in _risk_signals(sf)
            if s["qualifier"]["risk_family"] == "async_callback_in_iteration"
        ]
        self.assertEqual(len(signals), 20, f"expected 20 (file cap), got {len(signals)}")
        lines_present = sorted(s["qualifier"]["line"] for s in signals)
        self.assertEqual(len(lines_present), 20)
        # Inversion: 21st forEach is on line 22; it must be absent.
        self.assertNotIn(22, lines_present, "21st signal (line 22) must be dropped by file cap")


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


@unittest.skipIf(not NODE_AVAILABLE, "node not available")
class AssignmentContextDiscriminatorTest(unittest.TestCase):
    """P2: isAssignedContext must only suppress true assignments, not comparisons/logicals."""

    def test_strict_equality_rhs_still_emits_unawaited(self) -> None:
        """ready === saveRecord(id) — comparison, not assignment → must emit unawaited_async_call."""
        sf, _, _ = _build({"check.ts": _COMPARISON_CALL})
        signals = [s for s in _risk_signals(sf) if s["qualifier"]["risk_family"] == "unawaited_async_call"]
        self.assertGreater(
            len(signals), 0,
            "call on RHS of === is not an assignment; unawaited_async_call must fire",
        )

    def test_logical_or_rhs_still_emits_unawaited(self) -> None:
        """flag || saveRecord(id) — logical, not assignment → must emit unawaited_async_call."""
        sf, _, _ = _build({"or.ts": _LOGICAL_OR_CALL})
        signals = [s for s in _risk_signals(sf) if s["qualifier"]["risk_family"] == "unawaited_async_call"]
        self.assertGreater(
            len(signals), 0,
            "call on RHS of || is not an assignment; unawaited_async_call must fire",
        )

    def test_variable_declaration_still_suppressed(self) -> None:
        """const rec = saveRecord(id) — true assignment → must NOT emit unawaited_async_call."""
        sf, _, _ = _build({"assign.ts": _ASSIGNMENT_SUPPRESSED})
        signals = [s for s in _risk_signals(sf) if s["qualifier"]["risk_family"] == "unawaited_async_call"]
        self.assertEqual(
            signals, [],
            f"call in variable declaration must be suppressed; no unawaited_async_call: {signals}",
        )


@unittest.skipIf(not NODE_AVAILABLE, "node not available")
class ParenthesizedSuppressionTest(unittest.TestCase):
    """Root cause A: parenthesized forms must be treated like their unparenthesized counterparts."""

    def test_paren_await_no_signal(self) -> None:
        """await (saveRecord(id)) — awaited via paren → must NOT fire."""
        sf, _, _ = _build({"p.ts": _PAREN_AWAIT})
        signals = [s for s in _risk_signals(sf) if s["qualifier"]["risk_family"] == "unawaited_async_call"]
        self.assertEqual(signals, [], f"await (f()) must be suppressed: {signals}")

    def test_paren_return_no_signal(self) -> None:
        """return (saveRecord(id)) — returned via paren → must NOT fire."""
        sf, _, _ = _build({"p.ts": _PAREN_RETURN})
        signals = [s for s in _risk_signals(sf) if s["qualifier"]["risk_family"] == "unawaited_async_call"]
        self.assertEqual(signals, [], f"return (f()) must be suppressed: {signals}")

    def test_paren_then_chain_no_signal(self) -> None:
        """(saveRecord(id)).then(() => {}) — then-chained via paren → must NOT fire."""
        sf, _, _ = _build({"p.ts": _PAREN_THEN})
        signals = [s for s in _risk_signals(sf) if s["qualifier"]["risk_family"] == "unawaited_async_call"]
        self.assertEqual(signals, [], f"(f()).then() must be suppressed: {signals}")

    def test_paren_assign_no_signal(self) -> None:
        """const x = (saveRecord(id)) — assigned via paren → must NOT fire."""
        sf, _, _ = _build({"p.ts": _PAREN_ASSIGN})
        signals = [s for s in _risk_signals(sf) if s["qualifier"]["risk_family"] == "unawaited_async_call"]
        self.assertEqual(signals, [], f"const x = (f()) must be suppressed: {signals}")

    def test_double_paren_await_no_signal(self) -> None:
        """await ((saveRecord(id))) — double-paren awaited → must NOT fire."""
        sf, _, _ = _build({"p.ts": _DOUBLE_PAREN_AWAIT})
        signals = [s for s in _risk_signals(sf) if s["qualifier"]["risk_family"] == "unawaited_async_call"]
        self.assertEqual(signals, [], f"await ((f())) must be suppressed: {signals}")

    def test_paren_bare_statement_fires(self) -> None:
        """(saveRecord(id)); — parenthesized expression statement, genuinely unawaited → MUST fire."""
        sf, _, _ = _build({"p.ts": _PAREN_BARE_STATEMENT})
        signals = [s for s in _risk_signals(sf) if s["qualifier"]["risk_family"] == "unawaited_async_call"]
        self.assertGreater(len(signals), 0, "(f()); is genuinely unawaited; must fire")


def _entities_from_build(files: dict[str, str]) -> list[dict]:
    """Run build_kg and return all emitted entities."""
    import json as _json
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        pkg = root / "pkg"
        pkg.mkdir()
        (pkg / "package.json").write_text(
            _json.dumps({"name": "test-pkg", "version": "1.0.0"}), encoding="utf-8"
        )
        for name, text in files.items():
            (pkg / name).write_text(text, encoding="utf-8")
        out = root / "kg"
        build_kg(pkg, out)
        entities_path = out / "entities.jsonl"
        return read_jsonl(entities_path) if entities_path.exists() else []


@unittest.skipIf(not NODE_AVAILABLE, "node not available")
class ClassMethodSymbolKindTest(unittest.TestCase):
    """Root cause B: signal inside a class method → subject entity_id must align with main extractor."""

    def test_class_method_signal_subject_matches_main_extractor(self) -> None:
        """Signal inside a class method: subject entity_id must equal the main extraction's entity
        for that class (kind='class'), OR no signal if main extraction has no such entity.
        Conservatively: must NOT produce a CodeSymbol entity with symbol_kind='function' for 'Saver'
        (the enclosing class); if a signal fires for 'Saver', its entity must have symbol_kind='class'
        matching the main extractor's emission."""
        sf, ev, _ = _build({"cls.ts": _CLASS_METHOD_UNAWAITED})
        entities = _entities_from_build({"cls.ts": _CLASS_METHOD_UNAWAITED})
        signals = [s for s in _risk_signals(sf) if s["qualifier"]["risk_family"] == "unawaited_async_call"]

        # Build entity_id → identity map from emitted entities
        entity_map = {e["entity_id"]: e for e in entities if e.get("kind") == "CodeSymbol"}

        for sig in signals:
            qualname = sig["qualifier"]["qualname"]
            if qualname == "Saver":
                subject_id = sig["subject_id"]
                self.assertIn(
                    subject_id, entity_map,
                    f"Signal subject_id {subject_id!r} not found among emitted CodeSymbol entities",
                )
                actual_kind = entity_map[subject_id].get("identity", {}).get("symbol_kind")
                self.assertEqual(
                    actual_kind, "class",
                    f"Enclosing class 'Saver' must have symbol_kind='class', got {actual_kind!r}",
                )
            # If no signals for 'Saver' the adapter conservatively skipped it — also valid


class RetrievalCapTest(unittest.TestCase):
    """Unit tests for per-subject retrieval bound in _cap_risk_signals_per_subject."""

    def test_changed_range_overlap_wins_retrieval_cap(self) -> None:
        """4 signals for one subject; only the 4th (line 40) overlaps changed range → 4th survives."""
        from source.kg.product.mcp_tools import _cap_risk_signals_per_subject
        subject_id = "entity:test:subject1"
        signals = [
            {
                "subject_id": subject_id,
                "fact_id": f"fact:{i}",
                "_evidence": [{"bytes_ref": {"path": "router.ts", "line_start": line, "line_end": line + 2}}],
            }
            for i, line in enumerate([10, 20, 30, 40])
        ]
        range_filters = {"router.ts": [(38, 42)]}
        result = _cap_risk_signals_per_subject(signals, range_filters=range_filters, limit_per_subject=3)
        self.assertEqual(len(result), 3, f"expected 3 (per-subject cap), got {len(result)}")
        fact_ids = {s["fact_id"] for s in result}
        # Priority: line40 (overlap) → (0, 40); line10 → (1, 10); line20 → (1, 20); line30 → (1, 30)
        # Top 3: fact:3 (line40), fact:0 (line10), fact:1 (line20)
        self.assertIn("fact:3", fact_ids, "changed-range overlapping signal (line40) must survive")
        # Inversion: fact:2 (line30) is the one dropped
        self.assertNotIn("fact:2", fact_ids, "lowest non-overlapping 3rd signal (line30) must be dropped")
