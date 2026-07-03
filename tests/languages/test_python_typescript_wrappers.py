from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import tempfile
import unittest

from source.kg.core.repo_source import RepoSnapshot, discover_repo
from source.kg.extraction.framework.adapter import ExtractionContext
from source.kg.languages.dotnet.language import LANGUAGE_SUPPORT as DOTNET_SUPPORT
from source.kg.languages.dotnet.consumer_manifest import DotnetConsumerManifestExtractor
from source.kg.languages.dotnet.package_resolver import DotnetPackageResolver
from source.kg.languages.python.consumer_manifest import PythonConsumerManifestExtractor
from source.kg.languages.python.language import LANGUAGE_SUPPORT as PYTHON_SUPPORT
from source.kg.languages.typescript.consumer_manifest import TypeScriptConsumerManifestExtractor
from source.kg.languages.typescript.language import LANGUAGE_SUPPORT as TYPESCRIPT_SUPPORT


class PythonTypeScriptWrapperTest(unittest.TestCase):
    def test_python_wrapper_exposes_existing_adapter_names_and_roots(self) -> None:
        ctx = ExtractionContext()
        ctx.import_roots_by_language.setdefault("python", set()).update({"flask"})

        self.assertEqual(
            [adapter.capability.name for adapter in PYTHON_SUPPORT.adapters()],
            ["python-ast", "python-boto3-transport", "python-swallowed-exception"],
        )
        self.assertEqual(PYTHON_SUPPORT.source_roots(_repo_snapshot(), ctx), {"python": {"flask"}})
        self.assertEqual(PYTHON_SUPPORT.parse_repo(_repo_snapshot(), ctx), {})
        self.assertEqual(
            [type(detector).__name__ for detector in PYTHON_SUPPORT.opportunity_detectors()],
            ["HttpClientOpportunityDetector"],
        )
        self.assertEqual(type(PYTHON_SUPPORT.package_resolver()).__name__, "PythonPackageResolver")
        self.assertIsInstance(PYTHON_SUPPORT.consumer_manifest_extractor(), PythonConsumerManifestExtractor)
        python_rules = PYTHON_SUPPORT.dimension_rules()
        self.assertEqual(python_rules["version"], 1)
        self.assertIn("backend", {rule["dimension"] for rule in python_rules["rules"]})
        python_rules["rules"].clear()
        self.assertTrue(PYTHON_SUPPORT.dimension_rules()["rules"])
        self.assertEqual(PYTHON_SUPPORT.useful_edges(), {})

    def test_typescript_wrapper_exposes_existing_adapter_names_and_javascript_roots(self) -> None:
        ctx = ExtractionContext()
        ctx.import_roots_by_language.setdefault("javascript", set()).update({"express"})

        self.assertEqual(
            [adapter.capability.name for adapter in TYPESCRIPT_SUPPORT.adapters()],
            ["typescript-express-routes", "typescript-message-transport", "typescript-compiler-api", "typescript-async-lifecycle", "typescript-call-result-identity-comparison"],
        )
        self.assertEqual(TYPESCRIPT_SUPPORT.source_roots(_repo_snapshot(), ctx), {"javascript": {"express"}})
        self.assertEqual(TYPESCRIPT_SUPPORT.parse_repo(_repo_snapshot(), ctx), {})
        self.assertEqual(
            [type(detector).__name__ for detector in TYPESCRIPT_SUPPORT.opportunity_detectors()],
            ["TypeScriptHttpClientOpportunityDetector"],
        )
        self.assertEqual(type(TYPESCRIPT_SUPPORT.package_resolver()).__name__, "TypeScriptPackageResolver")
        self.assertIsInstance(TYPESCRIPT_SUPPORT.consumer_manifest_extractor(), TypeScriptConsumerManifestExtractor)
        typescript_rules = TYPESCRIPT_SUPPORT.dimension_rules()
        self.assertEqual(typescript_rules["version"], 1)
        self.assertIn("frontend", {rule["dimension"] for rule in typescript_rules["rules"]})
        typescript_rules["rules"].clear()
        self.assertTrue(TYPESCRIPT_SUPPORT.dimension_rules()["rules"])
        self.assertEqual(TYPESCRIPT_SUPPORT.useful_edges(), {})

    def test_typescript_matcher_preserves_declaration_file_exclusion(self) -> None:
        self.assertFalse(TYPESCRIPT_SUPPORT.matches_file(Path("types/foo.d.ts")))
        self.assertTrue(TYPESCRIPT_SUPPORT.matches_file(Path("src/index.ts")))
        self.assertTrue(TYPESCRIPT_SUPPORT.matches_file(Path("src/index.jsx")))

    def test_dotnet_wrapper_exposes_adapter_names_and_rules(self) -> None:
        ctx = ExtractionContext()
        ctx.import_roots_by_language.setdefault("dotnet", set()).update({"Microsoft.AspNetCore.Mvc"})

        self.assertEqual(
            [adapter.capability.name for adapter in DOTNET_SUPPORT.adapters()],
            ["dotnet-csharp-bridge", "dotnet-endpoints"],
        )
        self.assertEqual(DOTNET_SUPPORT.source_roots(_repo_snapshot(), ctx), {"dotnet": {"Microsoft.AspNetCore.Mvc"}})
        self.assertEqual(DOTNET_SUPPORT.parse_repo(_repo_snapshot(), ctx), {})
        self.assertEqual(DOTNET_SUPPORT.opportunity_detectors(), ())
        self.assertIsInstance(DOTNET_SUPPORT.package_resolver(), DotnetPackageResolver)
        self.assertIsInstance(DOTNET_SUPPORT.consumer_manifest_extractor(), DotnetConsumerManifestExtractor)
        dotnet_rules = DOTNET_SUPPORT.dimension_rules()
        self.assertEqual(dotnet_rules["version"], 1)
        self.assertIn("backend", {rule["dimension"] for rule in dotnet_rules["rules"]})
        dotnet_rules["rules"].clear()
        self.assertTrue(DOTNET_SUPPORT.dimension_rules()["rules"])
        self.assertEqual(DOTNET_SUPPORT.useful_edges(), {})

    def test_dotnet_matcher_excludes_build_output_dirs(self) -> None:
        self.assertTrue(DOTNET_SUPPORT.matches_file(Path("src/Program.cs")))
        self.assertFalse(DOTNET_SUPPORT.matches_file(Path("obj/Debug/net8.0/Generated.cs")))
        self.assertFalse(DOTNET_SUPPORT.matches_file(Path("bin/Debug/net8.0/Generated.cs")))

    def test_repo_discovery_populates_generic_language_buckets(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            python_file = root / "app.py"
            ts_file = root / "src" / "index.ts"
            declaration_file = root / "src" / "types.d.ts"
            ts_file.parent.mkdir()
            python_file.write_text("print('ok')\n", encoding="utf-8")
            ts_file.write_text("export const ok = true;\n", encoding="utf-8")
            declaration_file.write_text("declare const value: string;\n", encoding="utf-8")
            (root / "README.md").write_text("# docs\n", encoding="utf-8")

            repo = discover_repo(root)

        self.assertEqual(repo.files_by_language["python"], (python_file.resolve(),))
        self.assertEqual(repo.files_by_language["typescript"], (ts_file.resolve(),))

    def test_repo_discovery_prefilters_non_candidate_files_before_matching(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source_file = root / "main.example"
            readme = root / "README.md"
            source_file.write_text("example\n", encoding="utf-8")
            readme.write_text("# docs\n", encoding="utf-8")
            matcher = _CountingMatcher()

            repo = discover_repo(root, language_files=(matcher,))

        self.assertEqual(repo.files_by_language["example"], (source_file.resolve(),))
        self.assertEqual(matcher.calls, [source_file.resolve()])


def _repo_snapshot() -> RepoSnapshot:
    return RepoSnapshot(
        root=Path("/tmp/supercontext-language-test"),
        name="repo",
        owner="test",
        commit_sha="sha",
        files_by_language={"python": (), "typescript": (), "dotnet": ()},
    )


@dataclass(frozen=True)
class _CountingMatcher:
    name: str = "example"
    aliases: tuple[str, ...] = ()
    file_extensions: frozenset[str] = frozenset({".example"})
    manifest_files: frozenset[str] = frozenset()
    calls: list[Path] = field(default_factory=list, compare=False)

    def matches_file(self, path: Path) -> bool:
        self.calls.append(path)
        return path.suffix == ".example"


if __name__ == "__main__":
    unittest.main()
