from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from source.kg.build.pipeline import build_kg
from source.kg.product.mcp_tools import call_tool
from source.kg.query.snapshot import KgSnapshot


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _no_semantic_splice(**kwargs):
    stats = kwargs.get("_stats_out")
    if isinstance(stats, dict):
        stats.update({"rows_generated": 0, "rows_verified": 0, "rows_unverified": 0})
    return kwargs["review_hypotheses"], "active"


def _build_python_async_fixture(root: Path, *, awaited: bool) -> tuple[Path, Path, Path, Path]:
    import shutil

    repo = root / "repo"
    _write(repo / "pkg" / "__init__.py", "")
    _write(
        repo / "pkg" / "worker.py",
        "def make_widget(name):\n"
        "    return name.upper()\n\n"
        "def use_widget():\n"
        "    value = make_widget('a')\n"
        "    return value.lower()\n",
    )
    base_checkout = root / "base_checkout"
    shutil.copytree(repo, base_checkout)
    out_base = root / "kg_base"
    build_kg(repo, out_base)

    if awaited:
        head_source = (
            "async def make_widget(name):\n"
            "    return name.upper()\n\n"
            "async def use_widget():\n"
            "    value = await make_widget('a')\n"
            "    return value.lower()\n"
        )
    else:
        head_source = (
            "async def make_widget(name):\n"
            "    return name.upper()\n\n"
            "def use_widget():\n"
            "    value = make_widget('a')\n"
            "    return value.lower()\n"
        )
    _write(repo / "pkg" / "worker.py", head_source)
    out_head = root / "kg_head"
    build_kg(repo, out_head)
    return out_base, out_head, base_checkout, repo


def _build_python_async_scheduler_fixture(root: Path) -> tuple[Path, Path, Path, Path]:
    import shutil

    repo = root / "repo"
    _write(repo / "pkg" / "__init__.py", "")
    _write(
        repo / "pkg" / "worker.py",
        "def make_widget(name):\n"
        "    return name.upper()\n\n"
        "def use_widget():\n"
        "    return 'queued'\n",
    )
    base_checkout = root / "base_checkout"
    shutil.copytree(repo, base_checkout)
    out_base = root / "kg_base"
    build_kg(repo, out_base)

    _write(
        repo / "pkg" / "worker.py",
        "import asyncio\n\n"
        "async def make_widget(name):\n"
        "    return name.upper()\n\n"
        "def use_widget():\n"
        "    task = asyncio.create_task(make_widget('a'))\n"
        "    return task\n",
    )
    out_head = root / "kg_head"
    build_kg(repo, out_head)
    return out_base, out_head, base_checkout, repo


def _build_python_asyncio_run_fixture(root: Path) -> tuple[Path, Path, Path, Path]:
    import shutil

    repo = root / "repo"
    _write(repo / "pkg" / "__init__.py", "")
    _write(
        repo / "pkg" / "worker.py",
        "def make_widget(name):\n"
        "    return name.upper()\n\n"
        "def use_widget():\n"
        "    return make_widget('a')\n",
    )
    base_checkout = root / "base_checkout"
    shutil.copytree(repo, base_checkout)
    out_base = root / "kg_base"
    build_kg(repo, out_base)

    _write(
        repo / "pkg" / "worker.py",
        "import asyncio\n\n"
        "async def make_widget(name):\n"
        "    return name.upper()\n\n"
        "def use_widget():\n"
        "    return asyncio.run(make_widget('a'))\n",
    )
    out_head = root / "kg_head"
    build_kg(repo, out_head)
    return out_base, out_head, base_checkout, repo


def _build_python_wait_for_fixture(root: Path) -> tuple[Path, Path, Path, Path]:
    import shutil

    repo = root / "repo"
    _write(repo / "pkg" / "__init__.py", "")
    _write(
        repo / "pkg" / "worker.py",
        "def make_widget(name):\n"
        "    return name.upper()\n\n"
        "def use_widget():\n"
        "    return make_widget('a')\n",
    )
    base_checkout = root / "base_checkout"
    shutil.copytree(repo, base_checkout)
    out_base = root / "kg_base"
    build_kg(repo, out_base)

    _write(
        repo / "pkg" / "worker.py",
        "import asyncio\n\n"
        "async def make_widget(name):\n"
        "    return name.upper()\n\n"
        "def use_widget():\n"
        "    return asyncio.wait_for(make_widget('a'), timeout=5)\n",
    )
    out_head = root / "kg_head"
    build_kg(repo, out_head)
    return out_base, out_head, base_checkout, repo


def _build_python_custom_gather_fixture(root: Path) -> tuple[Path, Path, Path, Path]:
    import shutil

    repo = root / "repo"
    _write(repo / "pkg" / "__init__.py", "")
    _write(
        repo / "pkg" / "worker.py",
        "class Pool:\n"
        "    def gather(self, value):\n"
        "        return value\n\n"
        "pool = Pool()\n\n"
        "def make_widget(name):\n"
        "    return name.upper()\n\n"
        "def use_widget():\n"
        "    return pool.gather(make_widget('a'))\n",
    )
    base_checkout = root / "base_checkout"
    shutil.copytree(repo, base_checkout)
    out_base = root / "kg_base"
    build_kg(repo, out_base)

    _write(
        repo / "pkg" / "worker.py",
        "class Pool:\n"
        "    def gather(self, value):\n"
        "        return value\n\n"
        "pool = Pool()\n\n"
        "async def make_widget(name):\n"
        "    return name.upper()\n\n"
        "def use_widget():\n"
        "    return pool.gather(make_widget('a'))\n",
    )
    out_head = root / "kg_head"
    build_kg(repo, out_head)
    return out_base, out_head, base_checkout, repo


def _build_python_argument_fixture(root: Path) -> tuple[Path, Path, Path, Path]:
    import shutil

    repo = root / "repo"
    _write(repo / "pkg" / "__init__.py", "")
    _write(
        repo / "pkg" / "worker.py",
        "def make_widget(name):\n"
        "    return name.upper()\n\n"
        "def use_widget(sink):\n"
        "    return sink(make_widget('a'))\n",
    )
    base_checkout = root / "base_checkout"
    shutil.copytree(repo, base_checkout)
    out_base = root / "kg_base"
    build_kg(repo, out_base)

    _write(
        repo / "pkg" / "worker.py",
        "async def make_widget(name):\n"
        "    return name.upper()\n\n"
        "def use_widget(sink):\n"
        "    return sink(make_widget('a'))\n",
    )
    out_head = root / "kg_head"
    build_kg(repo, out_head)
    return out_base, out_head, base_checkout, repo


def _build_python_return_fixture(root: Path) -> tuple[Path, Path, Path, Path]:
    import shutil

    repo = root / "repo"
    _write(repo / "pkg" / "__init__.py", "")
    _write(
        repo / "pkg" / "worker.py",
        "def make_widget(name):\n"
        "    return name.upper()\n\n"
        "def use_widget():\n"
        "    return make_widget('a')\n",
    )
    base_checkout = root / "base_checkout"
    shutil.copytree(repo, base_checkout)
    out_base = root / "kg_base"
    build_kg(repo, out_base)

    _write(
        repo / "pkg" / "worker.py",
        "async def make_widget(name):\n"
        "    return name.upper()\n\n"
        "def use_widget():\n"
        "    return make_widget('a')\n",
    )
    out_head = root / "kg_head"
    build_kg(repo, out_head)
    return out_base, out_head, base_checkout, repo


def _build_python_same_leaf_fixture(root: Path) -> tuple[Path, Path, Path, Path]:
    import shutil

    repo = root / "repo"
    _write(repo / "pkg" / "__init__.py", "")
    _write(
        repo / "pkg" / "worker.py",
        "def make_widget(name):\n"
        "    return name.upper()\n\n"
        "class Other:\n"
        "    def make_widget(self):\n"
        "        return 'other'\n\n"
        "def use_widget(other):\n"
        "    value = other.make_widget()\n"
        "    result = make_widget('a')\n"
        "    return value + result\n",
    )
    base_checkout = root / "base_checkout"
    shutil.copytree(repo, base_checkout)
    out_base = root / "kg_base"
    build_kg(repo, out_base)

    _write(
        repo / "pkg" / "worker.py",
        "async def make_widget(name):\n"
        "    return name.upper()\n\n"
        "class Other:\n"
        "    def make_widget(self):\n"
        "        return 'other'\n\n"
        "async def use_widget(other):\n"
        "    value = other.make_widget()\n"
        "    result = await make_widget('a')\n"
        "    return value + result\n",
    )
    out_head = root / "kg_head"
    build_kg(repo, out_head)
    return out_base, out_head, base_checkout, repo


def _build_typescript_async_fixture(root: Path, *, awaited: bool) -> tuple[Path, Path, Path, Path]:
    import shutil

    repo = root / "ts_repo"
    _write(repo / "package.json", '{"name":"ts-repo","version":"1.0.0"}\n')
    _write(
        repo / "src" / "worker.ts",
        "export function makeThing(name: string): string {\n"
        "  return name.toUpperCase();\n"
        "}\n\n"
        "export function useThing(): string {\n"
        "  const value = makeThing('a');\n"
        "  return value.toLowerCase();\n"
        "}\n",
    )
    base_checkout = root / "ts_base_checkout"
    shutil.copytree(repo, base_checkout)
    out_base = root / "ts_kg_base"
    build_kg(repo, out_base)

    if awaited:
        head_source = (
            "export async function makeThing(name: string): Promise<string> {\n"
            "  return name.toUpperCase();\n"
            "}\n\n"
            "export async function useThing(): Promise<string> {\n"
            "  const value = await makeThing('a');\n"
            "  return value.toLowerCase();\n"
            "}\n"
        )
    else:
        head_source = (
            "export async function makeThing(name: string): Promise<string> {\n"
            "  return name.toUpperCase();\n"
            "}\n\n"
            "export function useThing(): string {\n"
            "  const value = makeThing('a');\n"
            "  return value.toLowerCase();\n"
            "}\n"
        )
    _write(repo / "src" / "worker.ts", head_source)
    out_head = root / "ts_kg_head"
    build_kg(repo, out_head)
    return out_base, out_head, base_checkout, repo


def _build_mjs_async_fixture(root: Path) -> tuple[Path, Path, Path, Path]:
    import shutil

    repo = root / "mjs_repo"
    _write(repo / "package.json", '{"name":"mjs-repo","version":"1.0.0","type":"module"}\n')
    _write(
        repo / "src" / "worker.mjs",
        "export function makeThing(name) {\n"
        "  return name.toUpperCase();\n"
        "}\n\n"
        "export function useThing() {\n"
        "  const value = makeThing('a');\n"
        "  return value.toLowerCase();\n"
        "}\n",
    )
    base_checkout = root / "mjs_base_checkout"
    shutil.copytree(repo, base_checkout)
    out_base = root / "mjs_kg_base"
    build_kg(repo, out_base)

    _write(
        repo / "src" / "worker.mjs",
        "export async function makeThing(name) {\n"
        "  return name.toUpperCase();\n"
        "}\n\n"
        "export function useThing() {\n"
        "  const value = makeThing('a');\n"
        "  return value.toLowerCase();\n"
        "}\n",
    )
    out_head = root / "mjs_kg_head"
    build_kg(repo, out_head)
    return out_base, out_head, base_checkout, repo


def _build_typescript_promise_scheduler_fixture(root: Path) -> tuple[Path, Path, Path, Path]:
    import shutil

    repo = root / "ts_repo"
    _write(repo / "package.json", '{"name":"ts-repo","version":"1.0.0"}\n')
    _write(
        repo / "src" / "worker.ts",
        "export function makeThing(name: string): string {\n"
        "  return name.toUpperCase();\n"
        "}\n\n"
        "export function useThing(): Promise<any> {\n"
        "  return Promise.resolve('queued');\n"
        "}\n",
    )
    base_checkout = root / "ts_base_checkout"
    shutil.copytree(repo, base_checkout)
    out_base = root / "ts_kg_base"
    build_kg(repo, out_base)

    _write(
        repo / "src" / "worker.ts",
        "export async function makeThing(name: string): Promise<string> {\n"
        "  return name.toUpperCase();\n"
        "}\n\n"
        "export function useThing(): Promise<any> {\n"
        "  return Promise.all(makeThing('a'));\n"
        "}\n",
    )
    out_head = root / "ts_kg_head"
    build_kg(repo, out_head)
    return out_base, out_head, base_checkout, repo


def _build_typescript_argument_fixture(root: Path) -> tuple[Path, Path, Path, Path]:
    import shutil

    repo = root / "ts_repo"
    _write(repo / "package.json", '{"name":"ts-repo","version":"1.0.0"}\n')
    _write(
        repo / "src" / "worker.ts",
        "export function makeThing(name: string): string {\n"
        "  return name.toUpperCase();\n"
        "}\n\n"
        "export function useThing(sink: (value: any) => string): string {\n"
        "  return sink(makeThing('a'));\n"
        "}\n",
    )
    base_checkout = root / "ts_base_checkout"
    shutil.copytree(repo, base_checkout)
    out_base = root / "ts_kg_base"
    build_kg(repo, out_base)

    _write(
        repo / "src" / "worker.ts",
        "export async function makeThing(name: string): Promise<string> {\n"
        "  return name.toUpperCase();\n"
        "}\n\n"
        "export function useThing(sink: (value: any) => string): string {\n"
        "  return sink(makeThing('a'));\n"
        "}\n",
    )
    out_head = root / "ts_kg_head"
    build_kg(repo, out_head)
    return out_base, out_head, base_checkout, repo


def _build_typescript_promise_return_fixture(root: Path) -> tuple[Path, Path, Path, Path]:
    import shutil

    repo = root / "ts_repo"
    _write(repo / "package.json", '{"name":"ts-repo","version":"1.0.0"}\n')
    _write(
        repo / "src" / "worker.ts",
        "export function makeThing(name: string): string {\n"
        "  return name.toUpperCase();\n"
        "}\n\n"
        "export function useThing(): string {\n"
        "  const value = makeThing('a');\n"
        "  return value.toLowerCase();\n"
        "}\n",
    )
    base_checkout = root / "ts_base_checkout"
    shutil.copytree(repo, base_checkout)
    out_base = root / "ts_kg_base"
    build_kg(repo, out_base)

    _write(
        repo / "src" / "worker.ts",
        "export function makeThing(name: string): Promise<string> {\n"
        "  return Promise.resolve(name.toUpperCase());\n"
        "}\n\n"
        "export function useThing(): string {\n"
        "  const value = makeThing('a');\n"
        "  return value.toLowerCase();\n"
        "}\n",
    )
    out_head = root / "ts_kg_head"
    build_kg(repo, out_head)
    return out_base, out_head, base_checkout, repo


def _build_typescript_qualified_promise_fixture(root: Path) -> tuple[Path, Path, Path, Path]:
    import shutil

    repo = root / "ts_repo"
    _write(repo / "package.json", '{"name":"ts-repo","version":"1.0.0"}\n')
    _write(
        repo / "src" / "worker.ts",
        "export namespace Foo {\n"
        "  export type Promise<T> = T;\n"
        "}\n\n"
        "export function makeThing(name: string): string {\n"
        "  return name.toUpperCase();\n"
        "}\n\n"
        "export function useThing(): string {\n"
        "  const value = makeThing('a');\n"
        "  return value.toLowerCase();\n"
        "}\n",
    )
    base_checkout = root / "ts_base_checkout"
    shutil.copytree(repo, base_checkout)
    out_base = root / "ts_kg_base"
    build_kg(repo, out_base)

    _write(
        repo / "src" / "worker.ts",
        "export namespace Foo {\n"
        "  export type Promise<T> = T;\n"
        "}\n\n"
        "export function makeThing(name: string): Foo.Promise<string> {\n"
        "  return name.toUpperCase();\n"
        "}\n\n"
        "export function useThing(): string {\n"
        "  const value = makeThing('a');\n"
        "  return value.toLowerCase();\n"
        "}\n",
    )
    out_head = root / "ts_kg_head"
    build_kg(repo, out_head)
    return out_base, out_head, base_checkout, repo


def _build_typescript_class_method_fixture(root: Path) -> tuple[Path, Path, Path, Path]:
    import shutil

    repo = root / "ts_repo"
    _write(repo / "package.json", '{"name":"ts-repo","version":"1.0.0"}\n')
    _write(
        repo / "src" / "worker.ts",
        "export class Worker {\n"
        "  makeThing(name: string): string {\n"
        "    return name.toUpperCase();\n"
        "  }\n"
        "}\n\n"
        "export function useThing(worker: Worker): string {\n"
        "  const value = worker.makeThing('a');\n"
        "  return value.toLowerCase();\n"
        "}\n",
    )
    base_checkout = root / "ts_base_checkout"
    shutil.copytree(repo, base_checkout)
    out_base = root / "ts_kg_base"
    build_kg(repo, out_base)

    _write(
        repo / "src" / "worker.ts",
        "export class Worker {\n"
        "  async makeThing(name: string): Promise<string> {\n"
        "    return name.toUpperCase();\n"
        "  }\n"
        "}\n\n"
        "export function useThing(worker: Worker): string {\n"
        "  const value = worker.makeThing('a') as any;\n"
        "  return value.toLowerCase();\n"
        "}\n",
    )
    out_head = root / "ts_kg_head"
    build_kg(repo, out_head)
    return out_base, out_head, base_checkout, repo


class TestUnawaitedAsyncResultCapture(unittest.TestCase):
    def test_review_context_emits_unawaited_async_result_capture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out_base, out_head, base_checkout, head_checkout = _build_python_async_fixture(root, awaited=False)
            kg = KgSnapshot(out_head)

            with patch(
                "source.kg.product.mcp_tools._splice_semantic_diff_hypotheses",
                side_effect=_no_semantic_splice,
            ):
                result = call_tool(kg, "review_context", {
                    "repo": "repo",
                    "changed_files": ["pkg/worker.py"],
                    "base_snapshot": str(out_base),
                    "base_checkout": str(base_checkout),
                    "head_checkout": str(head_checkout),
                    "execute_followups": False,
                })

        rows = [
            row for row in result.get("review_hypotheses") or []
            if row.get("risk_type") == "unawaited_async_result_capture"
        ]
        self.assertEqual((result.get("review_quality_status") or {}).get("async_result_capture_status"), "active")
        self.assertTrue(rows, f"expected async capture row; got {result.get('review_hypotheses')!r}")
        row = rows[0]
        self.assertEqual(row.get("derivation"), "deterministic_static")
        self.assertIn("make_widget became async", row.get("postable_claim", ""))
        self.assertIn("use_widget", row.get("postable_claim", ""))
        self.assertEqual(row.get("use_context"), "assignment")
        self.assertEqual(row.get("confidence"), "medium")
        self.assertEqual((row.get("cause") or {}).get("path"), "pkg/worker.py")

    def test_async_splice_uses_preloaded_base_snapshot(self) -> None:
        from source.kg.product.mcp_tools import _splice_unawaited_async_result_hypotheses

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out_base, out_head, base_checkout, head_checkout = _build_python_async_fixture(root, awaited=False)
            head_kg = KgSnapshot(out_head)
            base_kg = KgSnapshot(out_base)
            changed_symbols = []
            for row in head_kg.entities:
                if row.get("kind") != "CodeSymbol":
                    continue
                identity = row.get("identity") or {}
                properties = row.get("properties") or {}
                changed_symbols.append({
                    "qualname": identity.get("qualname"),
                    "path": properties.get("path"),
                })

            merged, status = _splice_unawaited_async_result_hypotheses(
                base_snapshot_dir=str(root / "missing-base-snapshot"),
                head_kg=head_kg,
                base_checkout=str(base_checkout),
                head_checkout=str(head_checkout),
                changed_symbols=changed_symbols,
                review_hypotheses=[],
                _base_snapshot=base_kg,
            )

        self.assertEqual(status, "active")
        self.assertTrue(
            any(row.get("risk_type") == "unawaited_async_result_capture" for row in merged),
            f"preloaded base snapshot should avoid reloading base_snapshot_dir; got {merged!r}",
        )

    def test_async_splice_omitted_count_ignores_unspliceable_rows(self) -> None:
        from source.kg.product.mcp_tools import _splice_unawaited_async_result_hypotheses

        raw_rows = [
            {"hypothesis_id": "hypothesis:unawaited_async_result_capture:1", "risk_type": "unawaited_async_result_capture"},
            {"risk_type": "unawaited_async_result_capture"},
            {"hypothesis_id": "hypothesis:unawaited_async_result_capture:2", "risk_type": "unawaited_async_result_capture"},
            {"risk_type": "unawaited_async_result_capture"},
            {"hypothesis_id": "hypothesis:unawaited_async_result_capture:3", "risk_type": "unawaited_async_result_capture"},
        ]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out_base, out_head, base_checkout, head_checkout = _build_python_async_fixture(root, awaited=False)
            head_kg = KgSnapshot(out_head)
            base_kg = KgSnapshot(out_base)
            with patch(
                "source.kg.query.async_result_capture.unawaited_async_result_capture",
                return_value=(raw_rows, {"rows_generated": len(raw_rows)}),
            ):
                merged, status = _splice_unawaited_async_result_hypotheses(
                    base_snapshot_dir=str(out_base),
                    head_kg=head_kg,
                    base_checkout=str(base_checkout),
                    head_checkout=str(head_checkout),
                    changed_symbols=[],
                    review_hypotheses=[],
                    _base_snapshot=base_kg,
                )

        self.assertEqual(status, "active")
        self.assertEqual(len(merged), 3)
        self.assertNotIn("omitted_unawaited_async_result_count", merged[-1])

    def test_async_splice_preserves_parse_error_status_without_spliceable_rows(self) -> None:
        from source.kg.product.mcp_tools import _splice_unawaited_async_result_hypotheses

        raw_rows = [{"risk_type": "unawaited_async_result_capture"}]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out_base, out_head, base_checkout, head_checkout = _build_python_async_fixture(root, awaited=False)
            head_kg = KgSnapshot(out_head)
            base_kg = KgSnapshot(out_base)
            with patch(
                "source.kg.query.async_result_capture.unawaited_async_result_capture",
                return_value=(raw_rows, {"typescript_parse_error_count": 1, "rows_generated": len(raw_rows)}),
            ):
                merged, status = _splice_unawaited_async_result_hypotheses(
                    base_snapshot_dir=str(out_base),
                    head_kg=head_kg,
                    base_checkout=str(base_checkout),
                    head_checkout=str(head_checkout),
                    changed_symbols=[],
                    review_hypotheses=[],
                    _base_snapshot=base_kg,
                )

        self.assertEqual(merged, [])
        self.assertEqual(status, "partial:typescript_parse_error")

    def test_function_info_prefers_qualname_before_line_fallback(self) -> None:
        from source.kg.query.async_result_capture import _function_info

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(
                root / "pkg" / "worker.py",
                "def other():\n"
                "    return 'wrong'\n\n"
                "def target():\n"
                "    return 'right'\n",
            )
            symbol = {
                "identity": {"qualname": "target"},
                "properties": {"path": "pkg/worker.py", "line": 1},
            }

            info = _function_info(root, symbol)

        self.assertIsNotNone(info)
        self.assertEqual(info.qualname, "target")
        self.assertEqual(getattr(info.node, "name", None), "target")

    def test_function_info_line_fallback_returns_matched_qualname(self) -> None:
        from source.kg.query.async_result_capture import _function_info

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(
                root / "pkg" / "worker.py",
                "class Widget:\n"
                "    def load(self):\n"
                "        return 'value'\n",
            )
            symbol = {
                "identity": {"qualname": "load"},
                "properties": {"path": "pkg/worker.py", "line": 2},
            }

            info = _function_info(root, symbol)

        self.assertIsNotNone(info)
        self.assertEqual(info.qualname, "Widget.load")
        self.assertEqual(getattr(info.node, "name", None), "load")

    def test_missing_line_same_leaf_matches_fail_closed(self) -> None:
        import ast
        from source.kg.query.async_result_capture import _first_unawaited_result_use

        tree = ast.parse(
            "def use_widget():\n"
            "    a = make_widget('a')\n"
            "    b = make_widget('b')\n"
            "    return a, b\n"
        )
        function = tree.body[0]

        call_node, use_kind = _first_unawaited_result_use(function, "make_widget", expected_line=None)

        self.assertIsNone(call_node)
        self.assertIsNone(use_kind)

    def test_bare_collision_prone_scheduler_name_is_not_suppressed(self) -> None:
        import ast
        from source.kg.query.async_result_capture import _is_python_coroutine_scheduler

        bare_run = ast.parse("run(make_widget())").body[0].value
        asyncio_run = ast.parse("asyncio.run(make_widget())").body[0].value

        self.assertFalse(_is_python_coroutine_scheduler(bare_run))
        self.assertTrue(_is_python_coroutine_scheduler(asyncio_run))

    def test_python_row_handles_none_properties(self) -> None:
        import ast
        from source.kg.query.async_result_capture import _FunctionInfo, _row

        async_node = ast.parse("async def make_widget(name):\n    return name\n").body[0]
        caller_node = ast.parse("def use_widget():\n    value = make_widget('a')\n").body[0]
        call_node = ast.parse("make_widget('a')").body[0].value

        row = _row(
            {
                "urn": "urn:async",
                "identity": {"qualname": "make_widget", "repo": "repo"},
                "properties": {"path": "pkg/worker.py", "line": 1},
            },
            _FunctionInfo(node=async_node, path="pkg/worker.py", qualname="make_widget"),
            {"identity": {"qualname": "use_widget", "repo": "repo"}, "properties": None},
            _FunctionInfo(node=caller_node, path="pkg/worker.py", qualname="use_widget"),
            call_node,
            use_kind="assignment",
        )

        self.assertEqual(row["cause"]["line_start"], 1)
        self.assertEqual(row["caller_qualname"], "use_widget")

    def test_typescript_parse_error_surfaces_in_async_status(self) -> None:
        from source.kg.product.mcp_tools import _splice_unawaited_async_result_hypotheses

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out_base, out_head, base_checkout, head_checkout = _build_typescript_async_fixture(root, awaited=False)
            head_kg = KgSnapshot(out_head)
            base_kg = KgSnapshot(out_base)
            changed_symbols = []
            for row in head_kg.entities:
                if row.get("kind") != "CodeSymbol":
                    continue
                identity = row.get("identity") or {}
                properties = row.get("properties") or {}
                changed_symbols.append({
                    "qualname": identity.get("qualname"),
                    "path": properties.get("path"),
                })

            with patch("source.kg.core.repo_source.discover_repo", side_effect=RuntimeError("parser bridge broke")):
                merged, status = _splice_unawaited_async_result_hypotheses(
                    base_snapshot_dir=str(out_base),
                    head_kg=head_kg,
                    base_checkout=str(base_checkout),
                    head_checkout=str(head_checkout),
                    changed_symbols=changed_symbols,
                    review_hypotheses=[],
                    _base_snapshot=base_kg,
                )

        self.assertEqual(merged, [])
        self.assertEqual(status, "failed:typescript_parse_error")

    def test_review_context_skips_caller_that_awaits_new_async_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out_base, out_head, base_checkout, head_checkout = _build_python_async_fixture(root, awaited=True)
            kg = KgSnapshot(out_head)

            with patch(
                "source.kg.product.mcp_tools._splice_semantic_diff_hypotheses",
                side_effect=_no_semantic_splice,
            ):
                result = call_tool(kg, "review_context", {
                    "repo": "repo",
                    "changed_files": ["pkg/worker.py"],
                    "base_snapshot": str(out_base),
                    "base_checkout": str(base_checkout),
                    "head_checkout": str(head_checkout),
                    "execute_followups": False,
                })

        rows = [
            row for row in result.get("review_hypotheses") or []
            if row.get("risk_type") == "unawaited_async_result_capture"
        ]
        self.assertEqual(rows, [], f"awaited caller must not emit async capture rows: {rows!r}")

    def test_review_context_skips_python_create_task_scheduler(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out_base, out_head, base_checkout, head_checkout = _build_python_async_scheduler_fixture(root)
            kg = KgSnapshot(out_head)

            with patch(
                "source.kg.product.mcp_tools._splice_semantic_diff_hypotheses",
                side_effect=_no_semantic_splice,
            ):
                result = call_tool(kg, "review_context", {
                    "repo": "repo",
                    "changed_files": ["pkg/worker.py"],
                    "base_snapshot": str(out_base),
                    "base_checkout": str(base_checkout),
                    "head_checkout": str(head_checkout),
                    "execute_followups": False,
                })

        rows = [
            row for row in result.get("review_hypotheses") or []
            if row.get("risk_type") == "unawaited_async_result_capture"
        ]
        self.assertEqual(rows, [], f"scheduled coroutine must not emit async capture rows: {rows!r}")

    def test_review_context_skips_python_asyncio_run_scheduler(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out_base, out_head, base_checkout, head_checkout = _build_python_asyncio_run_fixture(root)
            kg = KgSnapshot(out_head)

            with patch(
                "source.kg.product.mcp_tools._splice_semantic_diff_hypotheses",
                side_effect=_no_semantic_splice,
            ):
                result = call_tool(kg, "review_context", {
                    "repo": "repo",
                    "changed_files": ["pkg/worker.py"],
                    "base_snapshot": str(out_base),
                    "base_checkout": str(base_checkout),
                    "head_checkout": str(head_checkout),
                    "execute_followups": False,
                })

        rows = [
            row for row in result.get("review_hypotheses") or []
            if row.get("risk_type") == "unawaited_async_result_capture"
        ]
        self.assertEqual(rows, [], f"asyncio.run must not emit async capture rows: {rows!r}")

    def test_review_context_skips_python_wait_for_scheduler(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out_base, out_head, base_checkout, head_checkout = _build_python_wait_for_fixture(root)
            kg = KgSnapshot(out_head)

            with patch(
                "source.kg.product.mcp_tools._splice_semantic_diff_hypotheses",
                side_effect=_no_semantic_splice,
            ):
                result = call_tool(kg, "review_context", {
                    "repo": "repo",
                    "changed_files": ["pkg/worker.py"],
                    "base_snapshot": str(out_base),
                    "base_checkout": str(base_checkout),
                    "head_checkout": str(head_checkout),
                    "execute_followups": False,
                })

        rows = [
            row for row in result.get("review_hypotheses") or []
            if row.get("risk_type") == "unawaited_async_result_capture"
        ]
        self.assertEqual(rows, [], f"asyncio.wait_for must not emit async capture rows: {rows!r}")

    def test_review_context_does_not_suppress_custom_gather_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out_base, out_head, base_checkout, head_checkout = _build_python_custom_gather_fixture(root)
            kg = KgSnapshot(out_head)

            with patch(
                "source.kg.product.mcp_tools._splice_semantic_diff_hypotheses",
                side_effect=_no_semantic_splice,
            ):
                result = call_tool(kg, "review_context", {
                    "repo": "repo",
                    "changed_files": ["pkg/worker.py"],
                    "base_snapshot": str(out_base),
                    "base_checkout": str(base_checkout),
                    "head_checkout": str(head_checkout),
                    "execute_followups": False,
                })

        rows = [
            row for row in result.get("review_hypotheses") or []
            if row.get("risk_type") == "unawaited_async_result_capture"
        ]
        self.assertTrue(rows, f"custom gather wrapper should not be treated as asyncio.gather: {rows!r}")
        self.assertEqual(rows[0].get("use_context"), "argument")

    def test_review_context_emits_python_argument_capture_as_medium_confidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out_base, out_head, base_checkout, head_checkout = _build_python_argument_fixture(root)
            kg = KgSnapshot(out_head)

            with patch(
                "source.kg.product.mcp_tools._splice_semantic_diff_hypotheses",
                side_effect=_no_semantic_splice,
            ):
                result = call_tool(kg, "review_context", {
                    "repo": "repo",
                    "changed_files": ["pkg/worker.py"],
                    "base_snapshot": str(out_base),
                    "base_checkout": str(base_checkout),
                    "head_checkout": str(head_checkout),
                    "execute_followups": False,
                })

        rows = [
            row for row in result.get("review_hypotheses") or []
            if row.get("risk_type") == "unawaited_async_result_capture"
        ]
        self.assertTrue(rows, f"expected Python argument capture row; got {result.get('review_hypotheses')!r}")
        self.assertEqual(rows[0].get("use_context"), "argument")
        self.assertEqual(rows[0].get("confidence"), "medium")

    def test_review_context_emits_python_return_capture_as_medium_confidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out_base, out_head, base_checkout, head_checkout = _build_python_return_fixture(root)
            kg = KgSnapshot(out_head)

            with patch(
                "source.kg.product.mcp_tools._splice_semantic_diff_hypotheses",
                side_effect=_no_semantic_splice,
            ):
                result = call_tool(kg, "review_context", {
                    "repo": "repo",
                    "changed_files": ["pkg/worker.py"],
                    "base_snapshot": str(out_base),
                    "base_checkout": str(base_checkout),
                    "head_checkout": str(head_checkout),
                    "execute_followups": False,
                })

        rows = [
            row for row in result.get("review_hypotheses") or []
            if row.get("risk_type") == "unawaited_async_result_capture"
        ]
        self.assertTrue(rows, f"expected Python return capture row; got {result.get('review_hypotheses')!r}")
        self.assertEqual(rows[0].get("use_context"), "return")
        self.assertEqual(rows[0].get("confidence"), "medium")

    def test_review_context_skips_same_leaf_call_when_async_call_is_awaited(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out_base, out_head, base_checkout, head_checkout = _build_python_same_leaf_fixture(root)
            kg = KgSnapshot(out_head)

            with patch(
                "source.kg.product.mcp_tools._splice_semantic_diff_hypotheses",
                side_effect=_no_semantic_splice,
            ):
                result = call_tool(kg, "review_context", {
                    "repo": "repo",
                    "changed_files": ["pkg/worker.py"],
                    "base_snapshot": str(out_base),
                    "base_checkout": str(base_checkout),
                    "head_checkout": str(head_checkout),
                    "execute_followups": False,
                })

        rows = [
            row for row in result.get("review_hypotheses") or []
            if row.get("risk_type") == "unawaited_async_result_capture"
        ]
        self.assertEqual(rows, [], f"same-leaf non-target call must not emit async capture rows: {rows!r}")

    def test_review_context_emits_typescript_unawaited_promise_capture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out_base, out_head, base_checkout, head_checkout = _build_typescript_async_fixture(root, awaited=False)
            kg = KgSnapshot(out_head)

            with patch(
                "source.kg.product.mcp_tools._splice_semantic_diff_hypotheses",
                side_effect=_no_semantic_splice,
            ):
                result = call_tool(kg, "review_context", {
                    "repo": "ts_repo",
                    "changed_files": ["src/worker.ts"],
                    "base_snapshot": str(out_base),
                    "base_checkout": str(base_checkout),
                    "head_checkout": str(head_checkout),
                    "execute_followups": False,
                })

        rows = [
            row for row in result.get("review_hypotheses") or []
            if row.get("risk_type") == "unawaited_async_result_capture"
        ]
        self.assertTrue(rows, f"expected TS async capture row; got {result.get('review_hypotheses')!r}")
        self.assertIn("makeThing became async", rows[0].get("postable_claim", ""))
        self.assertIn("Promise", rows[0].get("postable_claim", ""))
        self.assertEqual(rows[0].get("use_context"), "assignment")
        self.assertEqual(rows[0].get("confidence"), "medium")

    def test_typescript_base_snapshot_without_async_props_uses_parse_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out_base, out_head, base_checkout, head_checkout = _build_typescript_async_fixture(root, awaited=False)
            entities_path = out_base / "entities.jsonl"
            rows = []
            for line in entities_path.read_text(encoding="utf-8").splitlines():
                row = json.loads(line)
                props = row.get("properties")
                if isinstance(props, dict):
                    props.pop("is_async", None)
                    props.pop("returns_promise_type", None)
                rows.append(row)
            entities_path.write_text(
                "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
                encoding="utf-8",
            )
            kg = KgSnapshot(out_head)

            with patch(
                "source.kg.product.mcp_tools._splice_semantic_diff_hypotheses",
                side_effect=_no_semantic_splice,
            ):
                result = call_tool(kg, "review_context", {
                    "repo": "ts_repo",
                    "changed_files": ["src/worker.ts"],
                    "base_snapshot": str(out_base),
                    "base_checkout": str(base_checkout),
                    "head_checkout": str(head_checkout),
                    "execute_followups": False,
                })

        rows = [
            row for row in result.get("review_hypotheses") or []
            if row.get("risk_type") == "unawaited_async_result_capture"
        ]
        self.assertTrue(rows, f"expected parse-fallback TS async capture row; got {result.get('review_hypotheses')!r}")

    def test_review_context_emits_mjs_unawaited_promise_capture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out_base, out_head, base_checkout, head_checkout = _build_mjs_async_fixture(root)
            kg = KgSnapshot(out_head)

            with patch(
                "source.kg.product.mcp_tools._splice_semantic_diff_hypotheses",
                side_effect=_no_semantic_splice,
            ):
                result = call_tool(kg, "review_context", {
                    "repo": "mjs_repo",
                    "changed_files": ["src/worker.mjs"],
                    "base_snapshot": str(out_base),
                    "base_checkout": str(base_checkout),
                    "head_checkout": str(head_checkout),
                    "execute_followups": False,
                })

        rows = [
            row for row in result.get("review_hypotheses") or []
            if row.get("risk_type") == "unawaited_async_result_capture"
        ]
        self.assertTrue(rows, f"expected .mjs async capture row; got {result.get('review_hypotheses')!r}")
        self.assertEqual((rows[0].get("cause") or {}).get("path"), "src/worker.mjs")

    def test_review_context_skips_typescript_awaited_promise_capture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out_base, out_head, base_checkout, head_checkout = _build_typescript_async_fixture(root, awaited=True)
            kg = KgSnapshot(out_head)

            with patch(
                "source.kg.product.mcp_tools._splice_semantic_diff_hypotheses",
                side_effect=_no_semantic_splice,
            ):
                result = call_tool(kg, "review_context", {
                    "repo": "ts_repo",
                    "changed_files": ["src/worker.ts"],
                    "base_snapshot": str(out_base),
                    "base_checkout": str(base_checkout),
                    "head_checkout": str(head_checkout),
                    "execute_followups": False,
                })

        rows = [
            row for row in result.get("review_hypotheses") or []
            if row.get("risk_type") == "unawaited_async_result_capture"
        ]
        self.assertEqual(rows, [], f"awaited TS caller must not emit async capture rows: {rows!r}")

    def test_review_context_skips_typescript_promise_scheduler(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out_base, out_head, base_checkout, head_checkout = _build_typescript_promise_scheduler_fixture(root)
            kg = KgSnapshot(out_head)

            with patch(
                "source.kg.product.mcp_tools._splice_semantic_diff_hypotheses",
                side_effect=_no_semantic_splice,
            ):
                result = call_tool(kg, "review_context", {
                    "repo": "ts_repo",
                    "changed_files": ["src/worker.ts"],
                    "base_snapshot": str(out_base),
                    "base_checkout": str(base_checkout),
                    "head_checkout": str(head_checkout),
                    "execute_followups": False,
                })

        rows = [
            row for row in result.get("review_hypotheses") or []
            if row.get("risk_type") == "unawaited_async_result_capture"
        ]
        self.assertEqual(rows, [], f"Promise scheduler must not emit async capture rows: {rows!r}")

    def test_review_context_emits_typescript_argument_capture_as_medium_confidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out_base, out_head, base_checkout, head_checkout = _build_typescript_argument_fixture(root)
            kg = KgSnapshot(out_head)

            with patch(
                "source.kg.product.mcp_tools._splice_semantic_diff_hypotheses",
                side_effect=_no_semantic_splice,
            ):
                result = call_tool(kg, "review_context", {
                    "repo": "ts_repo",
                    "changed_files": ["src/worker.ts"],
                    "base_snapshot": str(out_base),
                    "base_checkout": str(base_checkout),
                    "head_checkout": str(head_checkout),
                    "execute_followups": False,
                })

        rows = [
            row for row in result.get("review_hypotheses") or []
            if row.get("risk_type") == "unawaited_async_result_capture"
        ]
        self.assertTrue(rows, f"expected TS argument capture row; got {result.get('review_hypotheses')!r}")
        self.assertEqual(rows[0].get("use_context"), "argument")
        self.assertEqual(rows[0].get("confidence"), "medium")

    def test_review_context_emits_typescript_promise_return_without_async_keyword(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out_base, out_head, base_checkout, head_checkout = _build_typescript_promise_return_fixture(root)
            kg = KgSnapshot(out_head)

            with patch(
                "source.kg.product.mcp_tools._splice_semantic_diff_hypotheses",
                side_effect=_no_semantic_splice,
            ):
                result = call_tool(kg, "review_context", {
                    "repo": "ts_repo",
                    "changed_files": ["src/worker.ts"],
                    "base_snapshot": str(out_base),
                    "base_checkout": str(base_checkout),
                    "head_checkout": str(head_checkout),
                    "execute_followups": False,
                })

        rows = [
            row for row in result.get("review_hypotheses") or []
            if row.get("risk_type") == "unawaited_async_result_capture"
        ]
        self.assertTrue(rows, f"Promise-returning function must emit async capture row: {result!r}")
        self.assertEqual(rows[0].get("use_context"), "assignment")
        self.assertEqual(rows[0].get("confidence"), "medium")

    def test_review_context_skips_typescript_qualified_promise_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out_base, out_head, base_checkout, head_checkout = _build_typescript_qualified_promise_fixture(root)
            kg = KgSnapshot(out_head)

            with patch(
                "source.kg.product.mcp_tools._splice_semantic_diff_hypotheses",
                side_effect=_no_semantic_splice,
            ):
                result = call_tool(kg, "review_context", {
                    "repo": "ts_repo",
                    "changed_files": ["src/worker.ts"],
                    "base_snapshot": str(out_base),
                    "base_checkout": str(base_checkout),
                    "head_checkout": str(head_checkout),
                    "execute_followups": False,
                })

        rows = [
            row for row in result.get("review_hypotheses") or []
            if row.get("risk_type") == "unawaited_async_result_capture"
        ]
        self.assertEqual(rows, [], f"qualified Foo.Promise alias must fail closed: {rows!r}")

    def test_review_context_skips_typescript_class_method_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out_base, out_head, base_checkout, head_checkout = _build_typescript_class_method_fixture(root)
            kg = KgSnapshot(out_head)

            with patch(
                "source.kg.product.mcp_tools._splice_semantic_diff_hypotheses",
                side_effect=_no_semantic_splice,
            ):
                result = call_tool(kg, "review_context", {
                    "repo": "ts_repo",
                    "changed_files": ["src/worker.ts"],
                    "base_snapshot": str(out_base),
                    "base_checkout": str(base_checkout),
                    "head_checkout": str(head_checkout),
                    "execute_followups": False,
                })

        rows = [
            row for row in result.get("review_hypotheses") or []
            if row.get("risk_type") == "unawaited_async_result_capture"
        ]
        self.assertEqual(rows, [], f"class-method async flips are a documented fail-closed boundary: {rows!r}")
