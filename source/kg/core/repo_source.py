from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping
from pathlib import Path
import subprocess
from types import MappingProxyType

from source.kg.languages.file_matchers import REGISTERED_LANGUAGE_FILES
from source.kg.languages.types import LanguageFileMatcher
from source.kg.languages.unsupported import unsupported_files_by_language


IGNORED_DIRS = {
    ".git",
    ".idea",
    ".ipynb_checkpoints",
    ".mypy_cache",
    ".next",
    ".pytest_cache",
    ".ruff_cache",
    ".supercontext",
    ".turbo",
    ".vercel",
    ".venv",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
}

@dataclass(frozen=True, init=False, eq=False)
class RepoSnapshot:
    root: Path
    name: str
    owner: str
    commit_sha: str
    files_by_language: Mapping[str, tuple[Path, ...]]
    unsupported_files_by_language: Mapping[str, tuple[Path, ...]]

    def __init__(
        self,
        root: Path,
        name: str,
        owner: str,
        commit_sha: str,
        files_by_language: Mapping[str, tuple[Path, ...]],
        unsupported_files_by_language: Mapping[str, tuple[Path, ...]] | None = None,
    ) -> None:
        object.__setattr__(self, "root", root)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "owner", owner)
        object.__setattr__(self, "commit_sha", commit_sha)
        object.__setattr__(
            self,
            "files_by_language",
            MappingProxyType({language: tuple(paths) for language, paths in files_by_language.items()}),
        )
        object.__setattr__(
            self,
            "unsupported_files_by_language",
            MappingProxyType(
                {language: tuple(paths) for language, paths in (unsupported_files_by_language or {}).items()}
            ),
        )

    def __hash__(self) -> int:
        return hash((self.root, self.name, self.owner, self.commit_sha))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, RepoSnapshot):
            return NotImplemented
        return (
            self.root,
            self.name,
            self.owner,
            self.commit_sha,
        ) == (
            other.root,
            other.name,
            other.owner,
            other.commit_sha,
        )


def discover_repo(
    repo_path: str | Path,
    language_files: tuple[LanguageFileMatcher, ...] = REGISTERED_LANGUAGE_FILES,
    owner: str | None = None,
    name: str | None = None,
) -> RepoSnapshot:
    root = Path(repo_path).expanduser().resolve()
    if not root.exists():
        raise FileNotFoundError(f"Repo path does not exist: {root}")
    repo_name = _repo_name(root, name)

    files_by_language = _files_by_language(root, language_files)
    source_files = tuple(path for paths in files_by_language.values() for path in paths)
    return RepoSnapshot(
        root=root,
        name=repo_name,
        owner=owner or root.parent.name,
        commit_sha=_git_commit_sha(root),
        files_by_language=files_by_language,
        unsupported_files_by_language=unsupported_files_by_language(
            root,
            source_files=source_files,
            language_files=language_files,
            ignored_dirs=frozenset(IGNORED_DIRS),
        ),
    )


def _repo_name(root: Path, name: str | None) -> str:
    value = name.strip() if isinstance(name, str) else root.name
    if not value:
        raise ValueError("repo_name_empty")
    return value


def _iter_source_files(root: Path) -> list[Path]:
    return sorted({path for paths in _files_by_language(root).values() for path in paths})


def _files_by_language(
    root: Path,
    language_files: tuple[LanguageFileMatcher, ...] = REGISTERED_LANGUAGE_FILES,
) -> dict[str, tuple[Path, ...]]:
    buckets: dict[str, list[Path]] = {language.name: [] for language in language_files}
    candidate_extensions = {extension for language in language_files for extension in language.file_extensions}
    candidate_manifest_files = {filename for language in language_files for filename in language.manifest_files}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in IGNORED_DIRS for part in path.relative_to(root).parts):
            continue
        if path.suffix not in candidate_extensions and path.name not in candidate_manifest_files:
            continue
        for language in language_files:
            if language.matches_file(path):
                buckets.setdefault(language.name, []).append(path)
                break
    return {language: tuple(sorted(paths)) for language, paths in buckets.items()}


def _git_commit_sha(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "working-tree"
    return result.stdout.strip() or "working-tree"
