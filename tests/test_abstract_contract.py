"""Deterministic abstract-base reparenting detector tests.

Standing rule (vacuous-test-assertions memory): every test has inversion evidence
(break the code → the test fails), hard asserts (no ``if rows:`` guards, no
``next(..., None)`` soft handling), and at least one REAL-pipeline fixture that
builds tiny synthetic base+head repos on disk, runs the REAL build_kg +
call_tool("review_context") splice path, and asserts the row appears in the FINAL
packet with the correct risk_type / claim content / member names.

Cases:
  * positive        — reparent → abstract base, missing members, pass body → row in packet
  * negative_impl    — subclass implements ALL abstract members → no row
  * negative_concrete— new base is NOT abstract → no row
  * negative_same    — base list unchanged → no row
  * ambiguity        — two same-named base candidates, bare name → skip, no row
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from source.kg.build.pipeline import build_kg
from source.kg.core.models import JsonObject
from source.kg.product.mcp_tools import call_tool
from source.kg.query.snapshot import KgSnapshot

TENANT = "default"
_REPO = "pkg"
_RISK = "abstract_contract_unimplemented"

# Abstract base declaring two abstract members: a property and a method.
_ABSTRACT_BASE = (
    "import abc\n"
    "\n"
    "\n"
    "class BaseThing(abc.ABC):\n"
    "    @property\n"
    "    @abc.abstractmethod\n"
    "    def counter_names(self):\n"
    "        ...\n"
    "\n"
    "    @abc.abstractmethod\n"
    "    def build_it(self, x):\n"
    "        ...\n"
)

# Concrete (non-abstract) base with a usable method.
_CONCRETE_BASE = (
    "class Concrete:\n"
    "    def evaluate(self):\n"
    "        return 1\n"
)


def _build_pair(
    tmpdir: Path,
    base_core: str,
    head_core: str,
) -> tuple[Path, Path, Path, Path]:
    """Build base+head snapshots and matching on-disk checkouts from core.py content.

    Returns (out_base, out_head, base_checkout, head_checkout).
    """
    base_pkg = tmpdir / "base" / "pkg"
    head_pkg = tmpdir / "head" / "pkg"
    base_pkg.mkdir(parents=True)
    head_pkg.mkdir(parents=True)
    (base_pkg / "__init__.py").write_text("", encoding="utf-8")
    (head_pkg / "__init__.py").write_text("", encoding="utf-8")
    (base_pkg / "core.py").write_text(base_core, encoding="utf-8")
    (head_pkg / "core.py").write_text(head_core, encoding="utf-8")

    out_base = tmpdir / "kg_base"
    out_head = tmpdir / "kg_head"
    build_kg(base_pkg, out_base, tenant_id=TENANT)
    build_kg(head_pkg, out_head, tenant_id=TENANT)
    return out_base, out_head, base_pkg, head_pkg


def _review_context(
    out_base: Path,
    out_head: Path,
    base_checkout: Path,
    head_checkout: Path,
) -> JsonObject:
    """Run the REAL review_context pipeline (no LLM client needed for this detector)."""
    head_kg = KgSnapshot(out_head)
    return call_tool(
        head_kg,
        "review_context",
        {
            "repo": _REPO,
            "changed_files": ["core.py"],
            "base_snapshot": str(out_base),
            "base_checkout": str(base_checkout),
            "head_checkout": str(head_checkout),
        },
    )


def _abstract_rows(result: JsonObject) -> list[JsonObject]:
    hyps = result.get("review_hypotheses") or []
    return [h for h in hyps if isinstance(h, dict) and h.get("risk_type") == _RISK]


class TestAbstractContractPositive(unittest.TestCase):
    """Reparent onto abstract base with unimplemented members → row in FINAL packet."""

    def test_row_in_final_packet_with_member_names(self) -> None:
        base_core = _ABSTRACT_BASE + _CONCRETE_BASE + (
            "\n\nclass Widget(Concrete):\n"
            "    def evaluate(self):\n"
            "        return 2\n"
            "\n\ndef make():\n"
            "    return Widget()\n"
        )
        head_core = _ABSTRACT_BASE + _CONCRETE_BASE + (
            "\n\nclass Widget(BaseThing):\n"
            "    pass\n"
            "\n\ndef make():\n"
            "    return Widget()\n"
        )
        with tempfile.TemporaryDirectory() as td:
            tmpdir = Path(td)
            out_base, out_head, base_ck, head_ck = _build_pair(tmpdir, base_core, head_core)
            head_kg = KgSnapshot(out_head)
            # Hard precondition: fixture KG must carry the reparented class entity.
            self.assertTrue(
                any(
                    e.get("kind") == "CodeSymbol"
                    and (e.get("identity") or {}).get("symbol_kind") == "class"
                    and str((e.get("identity") or {}).get("qualname") or "").endswith("Widget")
                    for e in head_kg.entities
                ),
                "fixture KG must contain the Widget class entity — check build_kg output",
            )

            result = _review_context(out_base, out_head, base_ck, head_ck)
            rows = _abstract_rows(result)
            self.assertEqual(
                len(rows), 1,
                f"expected exactly 1 abstract_contract row in FINAL packet; "
                f"got risk_types={[h.get('risk_type') for h in (result.get('review_hypotheses') or [])]}",
            )
            row = rows[0]
            self.assertEqual(row.get("derivation"), "deterministic_static")
            self.assertEqual(row.get("specificity"), "high")
            self.assertEqual(row.get("confidence"), "high")
            # Exact member names present in the claim.
            claim = str(row.get("postable_claim") or "")
            self.assertIn("counter_names", claim, f"claim must name counter_names; got {claim!r}")
            self.assertIn("build_it", claim, f"claim must name build_it; got {claim!r}")
            self.assertIn("BaseThing", claim, f"claim must name the abstract base; got {claim!r}")
            self.assertIn("Widget", claim, f"claim must name the subclass; got {claim!r}")
            self.assertIn("TypeError", claim, f"claim must state the TypeError failure mode; got {claim!r}")
            self.assertIn("pass", claim.lower(), f"empty-body signal expected in claim; got {claim!r}")
            # cause = subclass class-def coordinate.
            self.assertEqual((row.get("cause") or {}).get("path"), "core.py")
            self.assertIsNotNone((row.get("cause") or {}).get("line_start"))
            # instantiation evidence captured from make() → Widget() CALLS edge.
            consequence = row.get("consequence") or {}
            self.assertIn(
                "instantiation_site", consequence,
                f"instantiation site must be captured from make()→Widget(); got {consequence!r}",
            )
            # Status surfaced.
            rqs = result.get("review_quality_status") or {}
            self.assertEqual(rqs.get("abstract_contract_status"), "active")
            # available_risk_types reflects the new family end-to-end.
            rhs = result.get("review_hypothesis_status") or {}
            self.assertIn(
                _RISK, rhs.get("available_risk_types") or [],
                f"risk type must appear in available_risk_types; got {rhs.get('available_risk_types')}",
            )


class TestAbstractContractNegatives(unittest.TestCase):
    """Cases that must NOT emit a row."""

    def _run(self, base_core: str, head_core: str) -> list[JsonObject]:
        with tempfile.TemporaryDirectory() as td:
            tmpdir = Path(td)
            out_base, out_head, base_ck, head_ck = _build_pair(tmpdir, base_core, head_core)
            result = _review_context(out_base, out_head, base_ck, head_ck)
            return _abstract_rows(result)

    def test_subclass_implements_all_members(self) -> None:
        """Subclass implements EVERY abstract member → no row (inversion of positive)."""
        base_core = _ABSTRACT_BASE + _CONCRETE_BASE + (
            "\n\nclass Widget(Concrete):\n"
            "    def evaluate(self):\n"
            "        return 2\n"
        )
        head_core = _ABSTRACT_BASE + _CONCRETE_BASE + (
            "\n\nclass Widget(BaseThing):\n"
            "    counter_names = ('a',)\n"
            "    def build_it(self, x):\n"
            "        return x\n"
        )
        rows = self._run(base_core, head_core)
        self.assertEqual(rows, [], f"all members implemented → no row; got {rows}")

    def test_new_base_not_abstract(self) -> None:
        """Reparent onto a concrete (non-abstract) base → no row."""
        base_core = _ABSTRACT_BASE + _CONCRETE_BASE + (
            "\n\nclass Widget(BaseThing):\n"
            "    counter_names = ('a',)\n"
            "    def build_it(self, x):\n"
            "        return x\n"
        )
        head_core = _ABSTRACT_BASE + _CONCRETE_BASE + (
            "\n\nclass Widget(Concrete):\n"
            "    pass\n"
        )
        rows = self._run(base_core, head_core)
        self.assertEqual(rows, [], f"non-abstract new base → no row; got {rows}")

    def test_base_list_unchanged(self) -> None:
        """Base list identical between base and head → no reparent trigger, no row."""
        cls = (
            "\n\nclass Widget(BaseThing):\n"
            "    pass\n"
        )
        core = _ABSTRACT_BASE + cls
        # Same base list; only an unrelated function body differs.
        base_core = core + "\n\ndef helper():\n    return 1\n"
        head_core = core + "\n\ndef helper():\n    return 2\n"
        rows = self._run(base_core, head_core)
        self.assertEqual(rows, [], f"unchanged base list → no row; got {rows}")

    def test_ambiguous_bare_base_name_skipped(self) -> None:
        """Two class candidates share the bare base name → fail-closed skip, no row.

        Base module ``core.py`` and a sibling ``other.py`` both define ``BaseThing``;
        the reparent uses the bare name ``BaseThing`` so resolution finds 2 candidates
        in the same repo and skips (conservative).
        """
        with tempfile.TemporaryDirectory() as td:
            tmpdir = Path(td)
            base_pkg = tmpdir / "base" / "pkg"
            head_pkg = tmpdir / "head" / "pkg"
            base_pkg.mkdir(parents=True)
            head_pkg.mkdir(parents=True)
            for pkg in (base_pkg, head_pkg):
                (pkg / "__init__.py").write_text("", encoding="utf-8")
                # A second file defining an identically-named abstract base.
                (pkg / "other.py").write_text(_ABSTRACT_BASE, encoding="utf-8")

            base_core = _ABSTRACT_BASE + _CONCRETE_BASE + (
                "\n\nclass Widget(Concrete):\n"
                "    def evaluate(self):\n"
                "        return 2\n"
            )
            head_core = _ABSTRACT_BASE + _CONCRETE_BASE + (
                "\n\nclass Widget(BaseThing):\n"
                "    pass\n"
            )
            (base_pkg / "core.py").write_text(base_core, encoding="utf-8")
            (head_pkg / "core.py").write_text(head_core, encoding="utf-8")

            out_base = tmpdir / "kg_base"
            out_head = tmpdir / "kg_head"
            build_kg(base_pkg, out_base, tenant_id=TENANT)
            build_kg(head_pkg, out_head, tenant_id=TENANT)

            head_kg = KgSnapshot(out_head)
            # Hard precondition: two BaseThing class candidates exist in the repo.
            base_things = [
                e for e in head_kg.entities
                if e.get("kind") == "CodeSymbol"
                and (e.get("identity") or {}).get("symbol_kind") == "class"
                and str((e.get("identity") or {}).get("qualname") or "").rsplit(".", 1)[-1] == "BaseThing"
            ]
            self.assertEqual(
                len(base_things), 2,
                f"fixture must produce 2 BaseThing candidates for the ambiguity test; got {len(base_things)}",
            )

            result = _review_context(out_base, out_head, base_pkg, head_pkg)
            rows = _abstract_rows(result)
            self.assertEqual(rows, [], f"ambiguous bare base name → fail-closed skip; got {rows}")


if __name__ == "__main__":
    unittest.main()
