from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from source.kg.core.repo_source import RepoSnapshot
from source.kg.extraction.framework.adapter import Adapter, ExtractionContext
from source.kg.languages._shared.dimension_rules_loader import load_dimension_rules
from source.kg.languages.known_stacks import load_known_stacks
from source.kg.languages.python.consumer_manifest import PythonConsumerManifestExtractor
from source.kg.languages.types import ConsumerManifestExtractor, PackageResolver
from source.kg.languages.python.extractors.extractor_adapter import PYTHON_AST_ADAPTER
from source.kg.languages.python.extractors.python_boto3_transport import PYTHON_BOTO3_TRANSPORT_ADAPTER
from source.kg.languages.python.extractors.python_swallowed_exception import PYTHON_SWALLOWED_EXCEPTION_ADAPTER
from source.kg.languages.python.files import LANGUAGE_FILES, PythonLanguageFiles
from source.kg.languages.python.opportunities import HttpClientOpportunityDetector
from source.kg.languages.python.package_resolver import PythonPackageResolver


@dataclass(frozen=True)
class PythonLanguageSupport:
    files: PythonLanguageFiles = LANGUAGE_FILES

    @property
    def name(self) -> str:
        return self.files.name

    @property
    def aliases(self) -> tuple[str, ...]:
        return self.files.aliases

    @property
    def file_extensions(self) -> frozenset[str]:
        return self.files.file_extensions

    @property
    def manifest_files(self) -> frozenset[str]:
        return self.files.manifest_files

    def matches_file(self, path: Path) -> bool:
        return self.files.matches_file(path)

    def source_roots(self, repo: RepoSnapshot, ctx: ExtractionContext) -> dict[str, set[str]]:
        return {"python": ctx.import_roots_by_language.setdefault("python", set())}

    def parse_repo(self, repo: RepoSnapshot, ctx: ExtractionContext) -> Mapping[str, Any]:
        return {}

    def opportunity_detectors(self) -> tuple[Any, ...]:
        return (HttpClientOpportunityDetector(),)

    def package_resolver(self) -> PackageResolver | None:
        return PythonPackageResolver()

    def consumer_manifest_extractor(self) -> ConsumerManifestExtractor | None:
        return PythonConsumerManifestExtractor()

    def dimension_rules(self) -> Mapping[str, Any]:
        return deepcopy(_dimension_rules())

    def useful_edges(self) -> Mapping[str, Any]:
        return {}

    def adapters(self) -> tuple[Adapter, ...]:
        return (PYTHON_AST_ADAPTER, PYTHON_BOTO3_TRANSPORT_ADAPTER, PYTHON_SWALLOWED_EXCEPTION_ADAPTER)

    def known_stacks(self) -> dict[str, dict[str, str]]:
        return {"python": dict(_known_stack_imports())}


LANGUAGE_SUPPORT = PythonLanguageSupport()


@cache
def _known_stack_imports() -> dict[str, str]:
    # Static package metadata: read once per process and return copies above.
    return load_known_stacks(Path(__file__).with_name("known_stacks.yaml"))


@cache
def _dimension_rules() -> Mapping[str, Any]:
    return load_dimension_rules(Path(__file__).with_name("dimension_rules.yaml"))
