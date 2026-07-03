"""Regression tests for CALLS extraction from variable-declared functions.

collectCallsForSymbol in ts_parser.mjs previously pruned any node whose range
extended beyond the symbol range.  For `export const fn = async () => {...}`
the symbol range is the VariableDeclaration, but the walk reaches the wrapping
VariableStatement first (statement.pos < declaration.pos), so the entire
subtree was pruned and variable-declared functions never produced any calls.
Function/class declarations were unaffected (the statement IS the symbol).

These tests pin the fixed behavior:
- variable-declared arrow function emits CALLS with qualifier.call
- function declaration still emits CALLS (no regression)
- calls outside the symbol range are not attributed to the symbol
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


_DB_MODULE = """\
export const prisma = { thing: { deleteMany: async (args: object) => 0 } };
"""

# Variable-declared arrow function calling through an imported binding.
_ARROW_HANDLER = """\
import { prisma } from "./db";

export const removeThing = async (id: number): Promise<void> => {
  await prisma.thing.deleteMany({ where: { id } });
};
"""

# Function declaration calling through an imported binding (pre-fix behavior).
_FUNCTION_HANDLER = """\
import { prisma } from "./db";

export async function removeThingFn(id: number): Promise<void> {
  await prisma.thing.deleteMany({ where: { id } });
}
"""

# Two variable-declared functions in one file: calls must attribute to the
# declaration that contains them, not leak across sibling symbols.
_TWO_ARROWS = """\
import { prisma } from "./db";

export const firstHandler = async (): Promise<void> => {
  await prisma.thing.deleteMany({ where: { kind: "first" } });
};

export const secondHandler = async (): Promise<void> => {
  await prisma.thing.count({ where: { kind: "second" } });
};
"""


def _build(files: dict[str, str]) -> tuple[list[dict], list[dict]]:
    """Write files to a tempdir, run build_kg, return (entities, facts)."""
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
        entities = read_jsonl(out / "entities.jsonl")
        facts = read_jsonl(out / "facts.jsonl")
    return entities, facts


def _calls_by_subject_qualname(entities: list[dict], facts: list[dict]) -> dict[str, list[str]]:
    """Map subject qualname -> list of qualifier.call strings for CALLS facts."""
    by_id = {e["entity_id"]: e for e in entities}
    result: dict[str, list[str]] = {}
    for fact in facts:
        if fact.get("predicate") != "CALLS":
            continue
        subject = by_id.get(fact.get("subject_id"), {})
        qualname = str(subject.get("identity", {}).get("qualname", ""))
        call = str((fact.get("qualifier") or {}).get("call", ""))
        result.setdefault(qualname, []).append(call)
    return result


@unittest.skipIf(not NODE_AVAILABLE, "node not available")
class VariableDeclaredCallsTest(unittest.TestCase):
    def test_arrow_function_emits_calls_fact(self) -> None:
        entities, facts = _build({"db.ts": _DB_MODULE, "handler.ts": _ARROW_HANDLER})
        calls = _calls_by_subject_qualname(entities, facts)
        self.assertIn("removeThing", calls)
        self.assertIn("prisma.thing.deleteMany", calls["removeThing"])

    def test_function_declaration_still_emits_calls_fact(self) -> None:
        entities, facts = _build({"db.ts": _DB_MODULE, "handler.ts": _FUNCTION_HANDLER})
        calls = _calls_by_subject_qualname(entities, facts)
        self.assertIn("removeThingFn", calls)
        self.assertIn("prisma.thing.deleteMany", calls["removeThingFn"])

    def test_calls_do_not_leak_across_sibling_symbols(self) -> None:
        entities, facts = _build({"db.ts": _DB_MODULE, "handlers.ts": _TWO_ARROWS})
        calls = _calls_by_subject_qualname(entities, facts)
        self.assertEqual(calls.get("firstHandler"), ["prisma.thing.deleteMany"])
        self.assertEqual(calls.get("secondHandler"), ["prisma.thing.count"])


if __name__ == "__main__":
    unittest.main()
