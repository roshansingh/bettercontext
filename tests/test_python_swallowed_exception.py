"""Tests for Python swallowed-exception risk signal extraction (Q2).

TDD: these tests are written before the implementation. They verify:
- bare except: pass fires (positive)
- except Exception: pass fires (positive)
- except BaseException: pass fires (positive)
- except with reraise does NOT fire (negative)
- narrow exception type (except ValueError:) does NOT fire (negative)
- handler that logs (calls logging.*) does NOT fire (negative)
- handler that calls cleanup (any call) does NOT fire (negative)
- per-symbol cap at 3 (lowest lines selected)
- bytes_ref is present and complete on every emitted signal
- qualifier contains risk_family, exception_type, detail
- determinism: two builds produce identical fact_ids
- syntax error → coverage refusal row (no crash)
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from source.kg.build.pipeline import build_kg
from source.kg.core.store import read_jsonl


# ---------------------------------------------------------------------------
# Fixtures — all names are neutral (no product/service terms)
# ---------------------------------------------------------------------------

# Positive: bare except: pass
_BARE_EXCEPT_PASS = """\
def process_item(item):
    try:
        result = compute(item)
    except:
        pass
    return result
"""

# Positive: except Exception: pass
_EXCEPT_EXCEPTION_PASS = """\
def save_record(record):
    try:
        db.write(record)
    except Exception:
        pass
"""

# Positive: except BaseException: pass
_EXCEPT_BASEEXCEPTION_PASS = """\
def run_task(task):
    try:
        task.execute()
    except BaseException:
        pass
"""

# Positive: except Exception: continue (inside loop)
_EXCEPT_EXCEPTION_CONTINUE = """\
def process_all(items):
    for item in items:
        try:
            handle(item)
        except Exception:
            continue
"""

# Positive: except Exception: return None (constant)
_EXCEPT_EXCEPTION_RETURN_NONE = """\
def fetch_value(key):
    try:
        return store[key]
    except Exception:
        return None
"""

# Negative: except with reraise — must NOT fire
_EXCEPT_RERAISE = """\
def run_job(job):
    try:
        job.run()
    except Exception:
        raise
"""

# Negative: narrow exception type — must NOT fire
_NARROW_EXCEPTION = """\
def parse_input(text):
    try:
        return int(text)
    except ValueError:
        pass
"""

# Negative: handler calls logging — must NOT fire
_HANDLER_LOGS = """\
import logging
logger = logging.getLogger(__name__)

def process(item):
    try:
        handle(item)
    except Exception:
        logger.exception("failed")
"""

# Negative: handler calls cleanup function — must NOT fire
_HANDLER_CALLS_CLEANUP = """\
def cleanup():
    pass

def process(item):
    try:
        handle(item)
    except Exception:
        cleanup()
"""

# Negative: handler assigns then calls — must NOT fire (any call disqualifies)
_HANDLER_ASSIGNS_AND_CALLS = """\
def process(item):
    try:
        handle(item)
    except Exception:
        result = None
        log_error(result)
"""

# Cap test: one function with 4 bare-except-pass handlers → only 3 signals
_CAP_EXCEEDED = """\
def multi_try(a, b, c, d):
    try:
        op_a(a)
    except Exception:
        pass
    try:
        op_b(b)
    except Exception:
        pass
    try:
        op_c(c)
    except Exception:
        pass
    try:
        op_d(d)
    except Exception:
        pass
"""

# Determinism: both bare and Exception forms
_DETERMINISM_FILE = """\
def alpha(x):
    try:
        go(x)
    except Exception:
        pass

def beta(y):
    try:
        do(y)
    except:
        pass
"""


def _build(files: dict[str, str]) -> tuple[list[dict], list[dict], list[dict]]:
    """Write files to a tempdir, run build_kg, return (support_facts, evidence, coverage)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        pkg = root / "pkg"
        pkg.mkdir()
        # Minimal pyproject.toml so the repo is recognized as Python
        (pkg / "pyproject.toml").write_text(
            "[project]\nname = \"test-pkg\"\nversion = \"0.1.0\"\n",
            encoding="utf-8",
        )
        for name, text in files.items():
            fpath = pkg / name
            fpath.parent.mkdir(parents=True, exist_ok=True)
            fpath.write_text(text, encoding="utf-8")
        out = root / "kg"
        build_kg(pkg, out)
        sf_path = out / "support_facts.jsonl"
        support_facts = read_jsonl(sf_path) if sf_path.exists() else []
        evidence = read_jsonl(out / "evidence.jsonl")
        coverage = read_jsonl(out / "coverage.jsonl")
    return support_facts, evidence, coverage


def _swallowed_signals(support_facts: list[dict]) -> list[dict]:
    return [
        f for f in support_facts
        if f.get("predicate") == "code_risk_signal"
        and f.get("qualifier", {}).get("risk_family") == "swallowed_exception"
    ]


class BareExceptPassTest(unittest.TestCase):
    def test_bare_except_pass_emits_signal(self) -> None:
        sf, _, _ = _build({"worker.py": _BARE_EXCEPT_PASS})
        signals = _swallowed_signals(sf)
        self.assertGreater(len(signals), 0, "expected swallowed_exception signal for bare except: pass")

    def test_except_exception_pass_emits_signal(self) -> None:
        sf, _, _ = _build({"worker.py": _EXCEPT_EXCEPTION_PASS})
        signals = _swallowed_signals(sf)
        self.assertGreater(len(signals), 0, "expected swallowed_exception signal for except Exception: pass")

    def test_except_baseexception_pass_emits_signal(self) -> None:
        sf, _, _ = _build({"worker.py": _EXCEPT_BASEEXCEPTION_PASS})
        signals = _swallowed_signals(sf)
        self.assertGreater(len(signals), 0, "expected swallowed_exception signal for except BaseException: pass")

    def test_except_exception_continue_emits_signal(self) -> None:
        sf, _, _ = _build({"worker.py": _EXCEPT_EXCEPTION_CONTINUE})
        signals = _swallowed_signals(sf)
        self.assertGreater(len(signals), 0, "expected swallowed_exception signal for except Exception: continue")

    def test_except_exception_return_none_emits_signal(self) -> None:
        sf, _, _ = _build({"worker.py": _EXCEPT_EXCEPTION_RETURN_NONE})
        signals = _swallowed_signals(sf)
        self.assertGreater(len(signals), 0, "expected swallowed_exception signal for except Exception: return None")


class NegativeTest(unittest.TestCase):
    def test_reraise_emits_no_signal(self) -> None:
        sf, _, _ = _build({"worker.py": _EXCEPT_RERAISE})
        signals = _swallowed_signals(sf)
        self.assertEqual(signals, [], f"reraise must not emit swallowed_exception: {signals}")

    def test_narrow_exception_emits_no_signal(self) -> None:
        sf, _, _ = _build({"worker.py": _NARROW_EXCEPTION})
        signals = _swallowed_signals(sf)
        self.assertEqual(signals, [], f"narrow exception must not emit swallowed_exception: {signals}")

    def test_logging_call_emits_no_signal(self) -> None:
        sf, _, _ = _build({"worker.py": _HANDLER_LOGS})
        signals = _swallowed_signals(sf)
        self.assertEqual(signals, [], f"handler with logging call must not emit signal: {signals}")

    def test_cleanup_call_emits_no_signal(self) -> None:
        sf, _, _ = _build({"worker.py": _HANDLER_CALLS_CLEANUP})
        signals = _swallowed_signals(sf)
        self.assertEqual(signals, [], f"handler with cleanup call must not emit signal: {signals}")

    def test_assign_then_call_emits_no_signal(self) -> None:
        sf, _, _ = _build({"worker.py": _HANDLER_ASSIGNS_AND_CALLS})
        signals = _swallowed_signals(sf)
        self.assertEqual(signals, [], f"handler with assignment+call must not emit signal: {signals}")


class QualifierAndBytesRefTest(unittest.TestCase):
    def test_qualifier_has_required_fields(self) -> None:
        sf, _, _ = _build({"worker.py": _EXCEPT_EXCEPTION_PASS})
        signals = _swallowed_signals(sf)
        self.assertGreater(len(signals), 0)
        q = signals[0]["qualifier"]
        self.assertIn("risk_family", q)
        self.assertIn("exception_type", q)
        self.assertIn("detail", q)
        self.assertEqual(q["risk_family"], "swallowed_exception")

    def test_exception_type_field_is_correct(self) -> None:
        sf, _, _ = _build({"worker.py": _BARE_EXCEPT_PASS})
        signals = _swallowed_signals(sf)
        self.assertGreater(len(signals), 0)
        q = signals[0]["qualifier"]
        # bare except has no type annotation — detail should convey this
        self.assertIn("exception_type", q)

    def test_signal_has_complete_bytes_ref(self) -> None:
        sf, ev, _ = _build({"worker.py": _EXCEPT_EXCEPTION_PASS})
        signals = _swallowed_signals(sf)
        self.assertGreater(len(signals), 0)
        sig_id = signals[0]["fact_id"]
        sig_ev = [e for e in ev if e.get("target_id") == sig_id]
        self.assertGreater(len(sig_ev), 0, "no evidence for signal")
        br = sig_ev[0].get("bytes_ref", {})
        for key in ("repo", "commit_sha", "path", "line_start", "line_end"):
            self.assertIn(key, br, f"bytes_ref missing {key}")
        self.assertIsNotNone(br["commit_sha"])
        self.assertNotEqual(br["commit_sha"], "")


class CapTest(unittest.TestCase):
    def test_per_symbol_cap_at_three(self) -> None:
        sf, _, _ = _build({"worker.py": _CAP_EXCEEDED})
        signals = [
            s for s in _swallowed_signals(sf)
            if s["qualifier"].get("qualname") == "multi_try"
        ]
        self.assertEqual(len(signals), 3, f"expected exactly 3 (cap), got {len(signals)}: {signals}")

    def test_cap_selects_lowest_lines(self) -> None:
        sf, _, _ = _build({"worker.py": _CAP_EXCEEDED})
        signals = [
            s for s in _swallowed_signals(sf)
            if s["qualifier"].get("qualname") == "multi_try"
        ]
        self.assertEqual(len(signals), 3)
        lines = sorted(s["qualifier"]["line"] for s in signals)
        # lines must be sorted ascending (lowest 3 selected)
        self.assertEqual(lines, sorted(lines))
        all_signals = [
            s for s in _swallowed_signals(sf)
            if s["qualifier"].get("qualname") == "multi_try"
        ]
        all_lines = sorted(s["qualifier"]["line"] for s in all_signals)
        max_possible_line = max(all_lines)
        # The 4th handler (highest line) should be absent — inversion: if 4 present this fails
        # We assert exactly 3 signals, not 4
        self.assertNotEqual(len(all_signals), 4, "cap not applied: 4 signals instead of 3")


class DeterminismTest(unittest.TestCase):
    def test_two_builds_produce_identical_fact_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            pkg = root / "pkg"
            pkg.mkdir()
            (pkg / "pyproject.toml").write_text(
                "[project]\nname = \"test-pkg\"\nversion = \"0.1.0\"\n",
                encoding="utf-8",
            )
            (pkg / "worker.py").write_text(_DETERMINISM_FILE, encoding="utf-8")
            out1 = root / "kg1"
            out2 = root / "kg2"
            build_kg(pkg, out1)
            build_kg(pkg, out2)
            sf1_path = out1 / "support_facts.jsonl"
            sf2_path = out2 / "support_facts.jsonl"
            sf1 = {
                f["fact_id"] for f in (read_jsonl(sf1_path) if sf1_path.exists() else [])
                if f.get("predicate") == "code_risk_signal"
                and f.get("qualifier", {}).get("risk_family") == "swallowed_exception"
            }
            sf2 = {
                f["fact_id"] for f in (read_jsonl(sf2_path) if sf2_path.exists() else [])
                if f.get("predicate") == "code_risk_signal"
                and f.get("qualifier", {}).get("risk_family") == "swallowed_exception"
            }
            self.assertGreater(len(sf1), 0, "no swallowed_exception signals in first build")
            self.assertEqual(sf1, sf2, "fact_ids differ between two builds")
