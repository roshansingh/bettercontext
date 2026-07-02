"""Tests for Python swallowed-exception risk signal extraction (Q2).

TDD: these tests are written before the implementation. They verify:
- bare except: pass fires (positive)
- except Exception: pass fires (positive)
- except BaseException: pass fires (positive)
- except with reraise does NOT fire (negative)
- narrow exception type (except ValueError:) does NOT fire (negative)
- handler that logs (calls logging.*) does NOT fire (negative)
- handler that calls cleanup (any call) does NOT fire (negative)
- per-file cap at 20 (lowest lines selected; per-symbol bound of 3 at retrieval)
- bytes_ref is present and complete on every emitted signal
- qualifier contains risk_family, exception_type, detail
- determinism: two builds produce identical fact_ids
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

# Negative: attribute assignment (self.flag = True) — state-changing, must NOT fire
_HANDLER_SELF_ATTR_ASSIGN = """\
class Processor:
    def process(self, item):
        try:
            handle(item)
        except Exception:
            self.flag = True
"""

# Negative: subscript assignment (state["failed"] = True) — state-changing, must NOT fire
_HANDLER_SUBSCRIPT_ASSIGN = """\
def process(item, state):
    try:
        handle(item)
    except Exception:
        state["failed"] = True
"""

# Negative: non-constant RHS (result = other_name) — references variable, must NOT fire
_HANDLER_NAME_RHS_ASSIGN = """\
DEFAULT = "fallback"

def process(item):
    try:
        handle(item)
    except Exception:
        result = DEFAULT
"""

# Positive: simple name with constant RHS (ok = True) — still vacuous, must fire
_HANDLER_SIMPLE_NAME_CONST = """\
def process(item):
    try:
        handle(item)
    except Exception:
        ok = True
"""

# Positive: async method with swallowed exception — must fire with subject entity_id
# matching the main extractor's "method" kind (class-nested, not async_function).
_ASYNC_METHOD_SWALLOWED = """\
class Worker:
    async def run(self, task):
        try:
            await task.execute()
        except Exception:
            pass
"""

# Positive: except (Exception, BaseException): pass → all elements broad → fires as tuple_broad
_TUPLE_BROAD_PASS = """\
def store_item(item):
    try:
        db.insert(item)
    except (Exception, BaseException):
        pass
"""

# Negative: except (Exception, ValueError): pass → mixed (one narrow) → must NOT fire
_TUPLE_MIXED_SUPPRESSED = """\
def load_item(key):
    try:
        return store[key]
    except (Exception, ValueError):
        pass
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


class TupleBroadHandlerTest(unittest.TestCase):
    def test_tuple_all_broad_emits_tuple_broad_signal(self) -> None:
        sf, _, _ = _build({"worker.py": _TUPLE_BROAD_PASS})
        signals = _swallowed_signals(sf)
        self.assertGreater(len(signals), 0, "expected swallowed_exception signal for except (Exception, BaseException): pass")
        exc_types = {s["qualifier"]["exception_type"] for s in signals}
        self.assertIn("tuple_broad", exc_types, f"expected exception_type=tuple_broad, got: {exc_types}")

    def test_tuple_mixed_narrow_suppressed(self) -> None:
        sf, _, _ = _build({"worker.py": _TUPLE_MIXED_SUPPRESSED})
        signals = _swallowed_signals(sf)
        self.assertEqual(signals, [], f"except (Exception, ValueError): must not emit signal (one narrow element): {signals}")


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
    def test_file_cap_allows_all_four_from_one_symbol(self) -> None:
        # _CAP_EXCEEDED has 4 handlers in one symbol; file cap is 20 → all 4 emitted.
        sf, _, _ = _build({"worker.py": _CAP_EXCEEDED})
        signals = [
            s for s in _swallowed_signals(sf)
            if s["qualifier"].get("qualname") == "multi_try"
        ]
        self.assertEqual(len(signals), 4, f"expected all 4 (file cap=20), got {len(signals)}: {signals}")

    def test_all_four_lines_present_under_file_cap(self) -> None:
        sf, _, _ = _build({"worker.py": _CAP_EXCEEDED})
        signals = [
            s for s in _swallowed_signals(sf)
            if s["qualifier"].get("qualname") == "multi_try"
        ]
        lines = sorted(s["qualifier"]["line"] for s in signals)
        # _CAP_EXCEEDED: 4 except Exception: pass handlers at lines 4, 8, 12, 16.
        # File cap is 20 → all 4 lines must appear.
        self.assertEqual(lines, [4, 8, 12, 16], f"expected all 4 handler lines [4,8,12,16], got {lines}")
        # Inversion: previously line 16 was dropped by the old per-symbol cap of 3.
        self.assertIn(16, lines, "line 16 (4th handler) must now be present under file cap")


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


class VacuousAssignmentNegativeTest(unittest.TestCase):
    """P2: state-changing assignments must not be treated as vacuous."""

    def test_self_attr_assign_emits_no_signal(self) -> None:
        sf, _, _ = _build({"worker.py": _HANDLER_SELF_ATTR_ASSIGN})
        signals = _swallowed_signals(sf)
        self.assertEqual(
            signals, [],
            f"self.flag = True is a state-changing assignment; must not emit signal: {signals}",
        )

    def test_subscript_assign_emits_no_signal(self) -> None:
        sf, _, _ = _build({"worker.py": _HANDLER_SUBSCRIPT_ASSIGN})
        signals = _swallowed_signals(sf)
        self.assertEqual(
            signals, [],
            f'state["failed"] = True is a state-changing assignment; must not emit signal: {signals}',
        )

    def test_name_rhs_variable_assign_emits_no_signal(self) -> None:
        sf, _, _ = _build({"worker.py": _HANDLER_NAME_RHS_ASSIGN})
        signals = _swallowed_signals(sf)
        self.assertEqual(
            signals, [],
            f"result = other_name (non-constant RHS) must not emit signal: {signals}",
        )

    def test_simple_name_constant_rhs_still_emits_signal(self) -> None:
        """Inversion: Name target + Constant RHS is genuinely vacuous → must still fire."""
        sf, _, _ = _build({"worker.py": _HANDLER_SIMPLE_NAME_CONST})
        signals = _swallowed_signals(sf)
        self.assertGreater(
            len(signals), 0,
            "ok = True in except body is vacuous; must still emit signal",
        )


class AsyncMethodKindTest(unittest.TestCase):
    """P2: async method inside a class must produce symbol_kind='method', matching main extractor."""

    def test_async_class_method_signal_subject_is_method_kind(self) -> None:
        """Signal subject entity_id must use symbol_kind='method', not 'async_function'."""
        import tempfile
        from pathlib import Path
        from source.kg.build.pipeline import build_kg
        from source.kg.core.store import read_jsonl
        from source.kg.core.models import Entity

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            pkg = root / "pkg"
            pkg.mkdir()
            (pkg / "pyproject.toml").write_text(
                "[project]\nname = \"test-pkg\"\nversion = \"0.1.0\"\n",
                encoding="utf-8",
            )
            (pkg / "worker.py").write_text(_ASYNC_METHOD_SWALLOWED, encoding="utf-8")
            out = root / "kg"
            build_kg(pkg, out)

            sf_path = out / "support_facts.jsonl"
            support_facts = read_jsonl(sf_path) if sf_path.exists() else []
            entities_all = read_jsonl(out / "entities.jsonl")

        signals = _swallowed_signals(support_facts)
        self.assertGreater(len(signals), 0, "async method swallowed exception must emit a signal")

        # Build entity_id index from the main extractor's entities.
        entities_by_id = {e["entity_id"]: e for e in entities_all}

        # The signal's subject must be the 'method' entity, not an 'async_function' entity.
        sig = signals[0]
        subject_id = sig["subject_id"]
        subject = entities_by_id.get(subject_id)
        self.assertIsNotNone(subject, f"subject entity_id {subject_id!r} not in main extractor entities")
        identity = subject.get("identity", {})
        self.assertEqual(
            identity.get("symbol_kind"), "method",
            f"async class method must have symbol_kind='method' (main extractor precedence), got: {identity}",
        )
        # Inversion: no entity with qualname 'Worker.run' should have symbol_kind='async_function'.
        async_fn_entities = [
            e for e in entities_all
            if e.get("identity", {}).get("qualname") == "Worker.run"
            and e.get("identity", {}).get("symbol_kind") == "async_function"
        ]
        self.assertEqual(
            async_fn_entities, [],
            f"no entity for Worker.run should have symbol_kind='async_function': {async_fn_entities}",
        )


_NESTED_FUNCTION_SWALLOWED = """\
def outer():
    def inner():
        try:
            go()
        except Exception:
            pass
"""

# Bug-fix fixtures: nested-function misattribution (5dd9f1b / ast.walk regression)

# Handler inside nested function → NO signal (previously attributed to outer)
_NESTED_FN_HANDLER_ONLY = """\
def outer():
    def inner():
        try:
            go()
        except Exception:
            pass
"""

# Handler directly in outer alongside a nested function → still fires on outer
_OUTER_AND_NESTED_FN = """\
def outer():
    try:
        op()
    except Exception:
        pass
    def inner():
        try:
            go()
        except Exception:
            pass
"""

# Handler inside a class-in-function → NO signal
_CLASS_IN_FUNCTION_HANDLER = """\
def outer():
    class Inner:
        def method(self):
            try:
                go()
            except Exception:
                pass
"""


class NestedFunctionMisattributionTest(unittest.TestCase):
    """Regression: ast.walk caused handlers in nested fns to be attributed to outer.

    After fix (explicit traversal with fn_stack): emit ONLY when handler's
    innermost enclosing FunctionDef IS a collected symbol.
    """

    def test_nested_fn_handler_emits_no_signal(self) -> None:
        """Handler inside inner() must NOT emit any signal (outer is not the enclosing fn)."""
        sf, _, _ = _build({"worker.py": _NESTED_FN_HANDLER_ONLY})
        signals = _swallowed_signals(sf)
        self.assertEqual(
            signals, [],
            f"handler inside nested fn must not emit signal (would misattribute to outer): {signals}",
        )

    def test_outer_handler_fires_despite_nested_fn(self) -> None:
        """Handler directly in outer() must still fire; handler in inner() must not."""
        sf, _, _ = _build({"worker.py": _OUTER_AND_NESTED_FN})
        signals = _swallowed_signals(sf)
        outer_signals = [s for s in signals if s["qualifier"].get("qualname") == "outer"]
        inner_signals = [s for s in signals if s["qualifier"].get("qualname") == "outer.inner"]
        self.assertGreater(len(outer_signals), 0, "outer handler must still emit a signal")
        self.assertEqual(inner_signals, [], f"inner handler must not emit signal: {inner_signals}")
        # Inversion: only outer, not inner
        all_qualnames = {s["qualifier"].get("qualname") for s in signals}
        self.assertNotIn("outer.inner", all_qualnames, "outer.inner must never appear as signal subject")

    def test_class_in_function_handler_emits_no_signal(self) -> None:
        """Handler inside a class defined inside a function → no signal (opaque scope)."""
        sf, _, _ = _build({"worker.py": _CLASS_IN_FUNCTION_HANDLER})
        signals = _swallowed_signals(sf)
        self.assertEqual(
            signals, [],
            f"handler inside class-in-function must not emit signal: {signals}",
        )


class NestedFunctionNotEmittedTest(unittest.TestCase):
    """Nested function symbols must not be emitted by the detector (mirrors main extractor).

    ast_extractor.py:578-590 does not recurse into FunctionDef bodies, so
    outer.inner is never emitted by the main extractor.  The detector must
    mirror this: entity_ids produced by _collect_function_symbols must align
    with those in entities.jsonl, and no signal subject may reference a
    qualname that the main extractor never wrote.
    """

    def test_nested_function_not_in_main_extractor_entities(self) -> None:
        """Main extractor must not emit a CodeSymbol for outer.inner."""
        sf, _, _ = _build({"worker.py": _NESTED_FUNCTION_SWALLOWED})
        # _build returns support_facts; we also need entities from the same run.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            pkg = root / "pkg"
            pkg.mkdir()
            (pkg / "pyproject.toml").write_text(
                "[project]\nname = \"test-pkg\"\nversion = \"0.1.0\"\n",
                encoding="utf-8",
            )
            (pkg / "worker.py").write_text(_NESTED_FUNCTION_SWALLOWED, encoding="utf-8")
            out = root / "kg"
            build_kg(pkg, out)
            entities_all = read_jsonl(out / "entities.jsonl")
            support_facts = read_jsonl(out / "support_facts.jsonl") if (out / "support_facts.jsonl").exists() else []

        nested_entities = [
            e for e in entities_all
            if e.get("identity", {}).get("qualname") == "outer.inner"
        ]
        self.assertEqual(
            nested_entities, [],
            f"main extractor must not emit outer.inner (ast_extractor.py:578-590); got: {nested_entities}",
        )

        # No signal subject should reference outer.inner.
        by_id = {e["entity_id"]: e for e in entities_all}
        for sig in _swallowed_signals(support_facts):
            subject = by_id.get(sig.get("subject_id", ""), {})
            self.assertNotEqual(
                subject.get("identity", {}).get("qualname"), "outer.inner",
                f"signal subject must not be outer.inner (detector diverged from main extractor): {sig}",
            )
