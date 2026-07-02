from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from source.kg.core.repo_source import discover_repo
from source.kg.extraction.framework.adapter import Adapter, AdapterResult, ExtractionContext
from source.kg.extraction.framework.runner import run_adapters
from source.kg.file_formats import file_format_adapters
from source.kg.languages import language_adapters


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "adapters"
EXCLUDED_FROM_SPLIT_FIXTURE_CONTRACT = {"static-config", "python-ast", "typescript-compiler-api", "typescript-async-lifecycle"}
RESERVED_FIXTURE_NAMES = {"expected.json", "expected_coverage.json"}


class AdapterContractTest(unittest.TestCase):
    def test_required_split_adapters_have_fixture_directories(self) -> None:
        fixture_names = {path.name for path in FIXTURE_ROOT.iterdir() if path.is_dir()}
        required_adapters = {adapter.capability.name for adapter in _contract_adapters()}

        self.assertEqual(required_adapters - fixture_names, set())

    def test_adapter_fixture_contracts(self) -> None:
        adapters = {adapter.capability.name: adapter for adapter in _contract_adapters()}
        for fixture_dir in sorted(path for path in FIXTURE_ROOT.iterdir() if path.is_dir()):
            with self.subTest(adapter=fixture_dir.name):
                adapter = adapters.get(fixture_dir.name)
                self.assertIsNotNone(adapter, f"No registered adapter for fixture directory {fixture_dir.name}")
                if adapter is None:
                    continue
                self._assert_golden(adapter, fixture_dir / "golden")
                self._assert_false_positive(adapter, fixture_dir / "false_positive")
                self._assert_coverage(adapter, fixture_dir / "coverage")

    def _assert_golden(self, adapter: Adapter, fixture_dir: Path) -> None:
        expected = _load_json_object(fixture_dir / "expected.json")
        result = _run_adapter(adapter, fixture_dir)

        _assert_exact_counts(self, _entity_kind_counts(result), _required_object(expected, "entities"), "entity")
        _assert_exact_counts(self, _fact_predicate_counts(result), _required_object(expected, "facts"), "fact")

    def _assert_false_positive(self, adapter: Adapter, fixture_dir: Path) -> None:
        result = _run_adapter(adapter, fixture_dir)

        self.assertEqual(result.facts, [])
        self.assertEqual(result.entities, [])
        self.assertEqual(result.evidence, [])
        self.assertEqual(result.coverage, [])

    def _assert_coverage(self, adapter: Adapter, fixture_dir: Path) -> None:
        expected = _load_json_value(fixture_dir / "expected_coverage.json")
        result = _run_adapter(adapter, fixture_dir)

        if isinstance(expected, dict) and expected.get("unsupported") is True:
            self.assertEqual(result.coverage, [])
            self.assertNotEqual(_required_string(expected, "reason").strip(), "")
            return

        expected_rows = _as_json_object_list(expected, fixture_dir / "expected_coverage.json")
        self.assertGreater(len(expected_rows), 0, "coverage fixtures must assert rows or declare unsupported=true")

        for expected in expected_rows:
            actual = _matching_coverage_row(result, expected)
            self.assertIsNotNone(actual, f"Missing coverage row matching {expected}")
            self.assertIn(_required_string(expected, "state"), {"partially_instrumented", "uninstrumented"})


def _run_adapter(adapter: Adapter, fixture_dir: Path) -> AdapterResult:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        _copy_fixture_files(fixture_dir, root)
        repo = discover_repo(root)
        entities, facts, evidence, coverage, errors = run_adapters(repo, [adapter], ctx=ExtractionContext())
        if errors:
            raise AssertionError(f"Adapter contract errors for {adapter.capability.name}: {errors}")
        return AdapterResult(entities=entities, facts=facts, evidence=evidence, coverage=coverage)


def _copy_fixture_files(fixture_dir: Path, root: Path) -> None:
    for source in fixture_dir.rglob("*"):
        if not source.is_file():
            continue
        if "__pycache__" in source.parts:
            continue
        if source.name in RESERVED_FIXTURE_NAMES:
            continue
        target = root / source.relative_to(fixture_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def _contract_adapters() -> tuple[Adapter, ...]:
    return tuple(
        adapter
        for adapter in (*language_adapters(), *file_format_adapters())
        if adapter.capability.name not in EXCLUDED_FROM_SPLIT_FIXTURE_CONTRACT
    )


def _load_json_object(path: Path) -> dict[str, object]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise AssertionError(f"{path} must contain a JSON object")
    return data


def _load_json_value(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _as_json_object_list(data: object, path: Path) -> list[dict[str, object]]:
    if not isinstance(data, list):
        raise AssertionError(f"{path} must contain a JSON array")
    for index, row in enumerate(data):
        if not isinstance(row, dict):
            raise AssertionError(f"{path}[{index}] must be a JSON object")
    return data


def _entity_kind_counts(result: AdapterResult) -> Counter[str]:
    return Counter(entity.kind for entity in result.entities)


def _fact_predicate_counts(result: AdapterResult) -> Counter[str]:
    return Counter(fact.predicate for fact in result.facts)


def _required_object(data: dict[str, object], key: str) -> dict[str, object]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise AssertionError(f"Expected {key} must be a JSON object")
    return value


def _required_string(data: dict[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise AssertionError(f"Expected {key} must be a string")
    return value


def _assert_exact_counts(
    test_case: unittest.TestCase,
    actual: Counter[str],
    expected_counts: object,
    label: str,
) -> None:
    if not isinstance(expected_counts, dict):
        raise AssertionError(f"Expected {label} counts must be a JSON object")
    expected: Counter[str] = Counter()
    for key, count in expected_counts.items():
        if not isinstance(key, str) or not isinstance(count, int):
            raise AssertionError(f"Expected {label} count keys must be strings and values must be integers")
        expected[key] = count
    test_case.assertEqual(actual, expected, f"{label} counts")


def _matching_coverage_row(result: AdapterResult, expected: dict[str, object]) -> object | None:
    expected_scope_ref = expected.get("scope_ref", {})
    if not isinstance(expected_scope_ref, dict):
        raise AssertionError("Expected coverage scope_ref must be a JSON object")
    for row in result.coverage:
        if row.predicate != expected.get("predicate"):
            continue
        if row.state != expected.get("state"):
            continue
        if _dict_contains(row.scope_ref, expected_scope_ref):
            return row
    return None


def _dict_contains(actual: dict[str, object], expected_subset: dict[str, object]) -> bool:
    return all(actual.get(key) == value for key, value in expected_subset.items())


if __name__ == "__main__":
    unittest.main()
