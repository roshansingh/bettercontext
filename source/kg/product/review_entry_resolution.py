from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
import json
from json import JSONDecodeError
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any

from source.kg.build import pipeline as kg_pipeline
from source.kg.core.models import JsonObject
from source.kg.query.snapshot import KgSnapshot


_CACHE_ROOT = ".supercontext"
_KG_CACHE_DIR = "kg"
_WORKTREE_CACHE_DIR = "worktrees"
_WORKTREE_KEEP_COUNT = 2
_SNAPSHOT_KEEP_COUNT = 4
_SNAPSHOT_FILES = ("entities.jsonl", "facts.jsonl", "evidence.jsonl", "coverage.jsonl", "manifest.json")
_GIT_COMMAND_TIMEOUT_SECONDS = 60
_CACHE_LOCK_TIMEOUT_SECONDS = 60
_CACHE_LOCK_POLL_SECONDS = 0.1
_SNAPSHOT_STORE_ENV = "SUPERCONTEXT_SNAPSHOT_STORE"
# Shared stores are long-lived across runs/clones. Any incompatible KG JSONL output
# or builder-contract change must bump this directory version to avoid stale hits.
_SNAPSHOT_STORE_SCHEMA_VERSION = "kg-jsonl-v2-wave5-review-context"


class SnapshotPairIdentityMismatch(RuntimeError):
    def __init__(self, *, base_repo_name: str | None, head_repo_name: str | None) -> None:
        super().__init__("base_head_repo_identity_mismatch")
        self.base_repo_name = base_repo_name
        self.head_repo_name = head_repo_name


@dataclass(frozen=True)
class ResolvedReviewContextEntry:
    kg: KgSnapshot
    arguments: JsonObject
    entry_resolution: JsonObject
    terminal_payload: JsonObject | None = None


def resolve_review_context_entry(kg: KgSnapshot, arguments: JsonObject) -> ResolvedReviewContextEntry:
    """Resolve minimal review_context args into the legacy explicit argument shape.

    Old callers can still pass repo/changed_files/base_snapshot/base_checkout/head_checkout.
    New callers may pass repo_path + base_ref; derivation fills any missing legacy fields.
    """

    repo_path_arg = _optional_string(arguments.get("repo_path"))
    base_ref_arg = _optional_string(arguments.get("base_ref"))
    snapshot_store_arg = (
        _optional_string(arguments.get("snapshot_store"))
        or _optional_string(os.getenv(_SNAPSHOT_STORE_ENV))
    )
    has_explicit_review_args = any(
        key in arguments
        for key in ("repo", "changed_files", "changed_ranges", "base_snapshot", "base_checkout", "head_checkout")
    )
    explicit_failures = _explicit_argument_failures(arguments)
    has_required_explicit_args = not explicit_failures
    if not repo_path_arg and not base_ref_arg:
        if not has_explicit_review_args:
            entry_resolution = _entry_resolution(
                mode="explicit",
                status="failed",
                derivation_failures=["missing_explicit_or_derived_review_context_args"],
            )
            return ResolvedReviewContextEntry(
                kg=kg,
                arguments=dict(arguments),
                entry_resolution=entry_resolution,
                terminal_payload=_terminal_resolution_payload(entry_resolution),
            )
        if not has_required_explicit_args:
            entry_resolution = _entry_resolution(
                mode="explicit",
                status="failed",
                derivation_failures=explicit_failures,
            )
            return ResolvedReviewContextEntry(
                kg=kg,
                arguments=dict(arguments),
                entry_resolution=entry_resolution,
                terminal_payload=_terminal_resolution_payload(entry_resolution),
            )
        return ResolvedReviewContextEntry(kg=kg, arguments=dict(arguments), entry_resolution={})

    failures: list[str] = []
    if not repo_path_arg:
        failures.append("missing_repo_path")
    if not base_ref_arg:
        failures.append("missing_base_ref")
    if failures:
        if has_required_explicit_args:
            merged = dict(arguments)
            if repo_path_arg:
                repo_path = Path(str(repo_path_arg)).expanduser()
                if repo_path.is_dir():
                    merged.setdefault("head_checkout", str(repo_path.resolve()))
            return ResolvedReviewContextEntry(kg=kg, arguments=merged, entry_resolution={})
        entry_resolution = _entry_resolution(mode="derived", status="failed", derivation_failures=failures)
        return ResolvedReviewContextEntry(
            kg=kg,
            arguments=dict(arguments),
            entry_resolution=entry_resolution,
            terminal_payload=_terminal_resolution_payload(entry_resolution),
        )
    if base_ref_arg.startswith("-"):
        entry_resolution = _entry_resolution(
            mode="derived",
            status="failed",
            base_ref=base_ref_arg,
            derivation_failures=["invalid_base_ref"],
        )
        return ResolvedReviewContextEntry(
            kg=kg,
            arguments=dict(arguments),
            entry_resolution=entry_resolution,
            terminal_payload=_terminal_resolution_payload(entry_resolution),
        )

    repo_path = Path(str(repo_path_arg)).expanduser().resolve()
    if not repo_path.is_dir():
        entry_resolution = _entry_resolution(
            mode="derived",
            status="failed",
            base_ref=base_ref_arg,
            derivation_failures=["repo_path_not_directory"],
        )
        return ResolvedReviewContextEntry(
            kg=kg,
            arguments=dict(arguments),
            entry_resolution=entry_resolution,
            terminal_payload=_terminal_resolution_payload(entry_resolution),
        )
    if not _is_git_repo(repo_path):
        entry_resolution = _entry_resolution(
            mode="derived",
            status="failed",
            base_ref=base_ref_arg,
            derivation_failures=["repo_path_not_git_repo"],
        )
        return ResolvedReviewContextEntry(
            kg=kg,
            arguments=dict(arguments),
            entry_resolution=entry_resolution,
            terminal_payload=_terminal_resolution_payload(entry_resolution),
        )
    try:
        dirty_worktree = _is_dirty_worktree(repo_path)
    except RuntimeError as exc:
        entry_resolution = _entry_resolution(
            mode="derived",
            status="failed",
            base_ref=base_ref_arg,
            derivation_failures=[_failure_reason(exc)],
        )
        return ResolvedReviewContextEntry(
            kg=kg,
            arguments=dict(arguments),
            entry_resolution=entry_resolution,
            terminal_payload=_terminal_resolution_payload(entry_resolution),
        )
    if dirty_worktree:
        entry_resolution = _entry_resolution(
            mode="derived",
            status="failed",
            base_ref=base_ref_arg,
            derivation_failures=["dirty_worktree"],
        )
        return ResolvedReviewContextEntry(
            kg=kg,
            arguments=dict(arguments),
            entry_resolution=entry_resolution,
            terminal_payload=_terminal_resolution_payload(entry_resolution),
        )

    try:
        head_sha = _git_stdout(repo_path, "rev-parse", "--verify", "HEAD^{commit}")
        base_ref_sha = _git_stdout(repo_path, "rev-parse", "--verify", f"{base_ref_arg}^{{commit}}")
        base_sha = _git_stdout(repo_path, "merge-base", base_ref_sha, head_sha)
    except RuntimeError as exc:
        entry_resolution = _entry_resolution(
            mode="derived",
            status="failed",
            base_ref=base_ref_arg,
            derivation_failures=[_failure_reason(exc)],
        )
        return ResolvedReviewContextEntry(
            kg=kg,
            arguments=dict(arguments),
            entry_resolution=entry_resolution,
            terminal_payload=_terminal_resolution_payload(entry_resolution),
        )

    binary_skipped = 0
    derivation_warnings: list[str] = []
    try:
        derived_repo_name = _derived_snapshot_repo_name(repo_path)
        snapshot_store = Path(snapshot_store_arg).expanduser().resolve() if snapshot_store_arg else None
        snapshot_store_identity = (
            _snapshot_store_repo_identity(
                repo_path,
                manifest_repo_name=_optional_string(kg.manifest.get("repo_name")),
                manifest_tenant_id=_optional_string(kg.manifest.get("tenant_id")),
            )
            if snapshot_store is not None
            else None
        )
        with _cache_lock(repo_path):
            locked_head_sha = _git_stdout(repo_path, "rev-parse", "--verify", "HEAD^{commit}")
            if locked_head_sha != head_sha:
                raise RuntimeError("head_moved")
            if _is_dirty_worktree(repo_path):
                raise RuntimeError("dirty_worktree")
            derived_files, binary_skipped = _derive_changed_files(repo_path, base_sha)
            derived_ranges = _derive_changed_ranges(
                repo_path,
                base_sha,
                derived_files,
                _warnings_out=derivation_warnings,
            )
            if derived_files:
                _ensure_supercontext_excluded(repo_path)
                base_checkout, base_worktree_reused, base_worktree_recreated = _ensure_base_worktree(repo_path, base_sha)
                base_snapshot, base_snapshot_reused, base_snapshot_store_hit = _ensure_snapshot(
                    base_checkout,
                    base_sha,
                    cache_root=repo_path,
                    snapshot_store=snapshot_store,
                    repo_identity=snapshot_store_identity,
                    repo_name=derived_repo_name,
                )
                head_snapshot, head_snapshot_reused, head_snapshot_store_hit = _ensure_snapshot(
                    repo_path,
                    head_sha,
                    cache_root=repo_path,
                    snapshot_store=snapshot_store,
                    repo_identity=snapshot_store_identity,
                    repo_name=derived_repo_name,
                )
                _validate_snapshot_pair_repo_identity(base_snapshot, head_snapshot)
                if snapshot_store is None:
                    _prune_snapshot_cache(repo_path, keep={base_snapshot, head_snapshot})
                resolved_kg = KgSnapshot(head_snapshot)
    except RuntimeError as exc:
        snapshot_repo_names = (
            {
                "base_repo_name": exc.base_repo_name,
                "head_repo_name": exc.head_repo_name,
            }
            if isinstance(exc, SnapshotPairIdentityMismatch)
            else None
        )
        entry_resolution = _entry_resolution(
            mode="derived",
            status="failed",
            base_ref=base_ref_arg,
            base_sha=base_sha,
            head_sha=head_sha,
            derivation_failures=[_failure_reason(exc)],
            snapshot_repo_names=snapshot_repo_names,
        )
        return ResolvedReviewContextEntry(
            kg=kg,
            arguments=dict(arguments),
            entry_resolution=entry_resolution,
            terminal_payload=_terminal_resolution_payload(entry_resolution),
        )

    entry_status = "active"
    if not derived_files:
        entry_status = "binary_only_changes" if binary_skipped else "no_changes_detected"
    snapshot_metadata = (
        {
            "base_snapshot": str(base_snapshot),
            "head_snapshot": str(head_snapshot),
            "base_snapshot_reused": base_snapshot_reused,
            "head_snapshot_reused": head_snapshot_reused,
            "base_snapshot_store_hit": base_snapshot_store_hit,
            "head_snapshot_store_hit": head_snapshot_store_hit,
            "snapshot_store_schema_version": _SNAPSHOT_STORE_SCHEMA_VERSION if snapshot_store else None,
            "base_repo_name": _snapshot_repo_name(base_snapshot),
            "head_repo_name": _snapshot_repo_name(head_snapshot),
            "base_worktree": str(base_checkout),
            "base_worktree_reused": base_worktree_reused,
            "base_worktree_recreated": base_worktree_recreated,
        }
        if derived_files
        else None
    )

    entry_resolution = _entry_resolution(
        mode="derived",
        status=entry_status,
        base_ref=base_ref_arg,
        base_ref_sha=base_ref_sha,
        base_sha=base_sha,
        head_sha=head_sha,
        changed_files_count=len(derived_files),
        changed_ranges_count=len(derived_ranges),
        binary_files_skipped=binary_skipped,
        derivation_warnings=derivation_warnings,
        snapshots=snapshot_metadata,
    )
    if not derived_files:
        terminal_args = dict(arguments)
        terminal_args.setdefault("repo", kg.manifest.get("repo_name") or repo_path.name)
        terminal_args.setdefault("changed_files", [])
        terminal_args.setdefault("changed_ranges", [])
        return ResolvedReviewContextEntry(
            kg=kg,
            arguments=terminal_args,
            entry_resolution=entry_resolution,
            terminal_payload=_no_changes_payload(
                entry_resolution,
                repo=str(terminal_args.get("repo") or repo_path.name),
            ),
        )
    merged = dict(arguments)
    merged.setdefault("repo", resolved_kg.manifest.get("repo_name") or repo_path.name)
    merged.setdefault("changed_files", derived_files)
    merged.setdefault("changed_ranges", derived_ranges)
    merged.setdefault("base_snapshot", str(base_snapshot))
    merged.setdefault("base_checkout", str(base_checkout))
    merged.setdefault("head_checkout", str(repo_path))
    return ResolvedReviewContextEntry(kg=resolved_kg, arguments=merged, entry_resolution=entry_resolution)


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _explicit_argument_failures(arguments: JsonObject) -> list[str]:
    failures: list[str] = []
    if "repo" not in arguments:
        failures.append("missing_repo")
    if "changed_files" not in arguments:
        failures.append("missing_changed_files")
        return failures
    changed_files = arguments.get("changed_files")
    if not isinstance(changed_files, list):
        failures.append("invalid_changed_files")
    elif not changed_files:
        failures.append("empty_changed_files")
    elif any(not isinstance(item, str) or not item.strip() for item in changed_files):
        failures.append("invalid_changed_files")
    return failures


def _entry_resolution(
    *,
    mode: str,
    status: str,
    base_ref: str | None = None,
    base_ref_sha: str | None = None,
    base_sha: str | None = None,
    head_sha: str | None = None,
    changed_files_count: int | None = None,
    changed_ranges_count: int | None = None,
    binary_files_skipped: int = 0,
    derivation_failures: list[str] | None = None,
    derivation_warnings: list[str] | None = None,
    snapshots: JsonObject | None = None,
    snapshot_repo_names: JsonObject | None = None,
) -> JsonObject:
    resolution: JsonObject = {
        "mode": mode,
        "status": status,
        "derivation_failures": list(derivation_failures or []),
    }
    if base_ref is not None:
        resolution["base_ref"] = base_ref
    if base_ref_sha is not None:
        resolution["base_ref_sha"] = base_ref_sha
    if base_sha is not None:
        resolution["base_sha"] = base_sha
    if head_sha is not None:
        resolution["head_sha"] = head_sha
    if changed_files_count is not None:
        resolution["changed_files_count"] = changed_files_count
    if changed_ranges_count is not None:
        resolution["changed_ranges_count"] = changed_ranges_count
    if binary_files_skipped:
        resolution["binary_files_skipped"] = binary_files_skipped
    if derivation_warnings:
        resolution["derivation_warnings"] = list(derivation_warnings)
    if snapshots:
        resolution["snapshots"] = snapshots
    if snapshot_repo_names:
        resolution["snapshot_repo_names"] = snapshot_repo_names
    return resolution


def _terminal_resolution_payload(entry_resolution: JsonObject) -> JsonObject:
    return {
        "status": "error",
        "entry_resolution": entry_resolution,
        "review_quality_status": {
            "review_readiness": "plain_review_better",
            "reason": "review_context entry argument derivation failed; inspect source directly or retry with explicit changed_files.",
        },
        "review_answer_packet": {
            "packet_mode": "entry_resolution_error",
            "status": "error",
            "entry_resolution": entry_resolution,
        },
        "review_lead_status": {"coverage_status": "low_coverage"},
        "review_hypotheses": [],
    }


def _no_changes_payload(entry_resolution: JsonObject, *, repo: str) -> JsonObject:
    binary_only = entry_resolution.get("status") == "binary_only_changes"
    status = "binary_only_changes" if binary_only else "no_changes_detected"
    reason = (
        "Changed files were detected between base_ref and HEAD, but all detected changes were binary "
        "or unsupported by text-range extraction."
        if binary_only
        else "No changed files were detected between base_ref and HEAD."
    )
    missing = ["changed_text_files"] if binary_only else ["changed_files"]
    followup = (
        "Inspect the binary diff or retry with explicit changed_files if a text review surface exists."
        if binary_only
        else "Confirm the intended base_ref/head pair or inspect the local diff directly."
    )
    return {
        "status": status,
        "repo": repo,
        "requested_repo": repo,
        "entry_resolution": entry_resolution,
        "summary": {"changed_file_count": 0},
        "review_quality_status": {
            "review_readiness": "plain_review_better",
            "reason": reason,
        },
        "review_answer_packet": {
            "packet_mode": status,
            "status": status,
            "entry_resolution": entry_resolution,
        },
        "review_lead_status": {"coverage_status": "low_coverage"},
        "review_leads": {"changed_files": [], "changed_symbols": [], "source_coordinates": []},
        "diff_anchors": [],
        "changed_symbols": [],
        "review_hypotheses": [],
        "answerability": {
            "status": "not_answerable",
            "missing_fact_families": missing,
            "recommended_followups": [followup],
        },
    }


def _is_git_repo(repo_path: Path) -> bool:
    try:
        _git_stdout(repo_path, "rev-parse", "--is-inside-work-tree")
    except RuntimeError:
        return False
    return True


def _ensure_supercontext_excluded(repo_path: Path) -> None:
    exclude_path_text = _git_stdout(repo_path, "rev-parse", "--git-path", "info/exclude")
    exclude_path = Path(exclude_path_text)
    if not exclude_path.is_absolute():
        exclude_path = repo_path / exclude_path
    try:
        existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
        lines = existing.splitlines()
        if any(line.strip() in {_CACHE_ROOT, f"{_CACHE_ROOT}/"} for line in lines):
            return
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        suffix = "" if not existing or existing.endswith("\n") else "\n"
        exclude_path.write_text(f"{existing}{suffix}{_CACHE_ROOT}/\n", encoding="utf-8")
    except OSError as exc:
        raise RuntimeError("exclude_write_failed") from exc


def _is_dirty_worktree(repo_path: Path) -> bool:
    status = _git_stdout(repo_path, "status", "--porcelain")
    for line in status.splitlines():
        path_text = line[3:] if len(line) > 3 else ""
        paths = [part.strip().strip('"') for part in path_text.split(" -> ")]
        if paths and all(path == _CACHE_ROOT or path.startswith(f"{_CACHE_ROOT}/") for path in paths):
            continue
        if line.strip():
            return True
    return False


def _derive_changed_files(repo_path: Path, base_ref: str) -> tuple[list[str], int]:
    raw = _git_bytes(repo_path, "diff", "--name-status", "-z", "--find-renames", f"{base_ref}..HEAD")
    fields = [field.decode("utf-8", errors="replace") for field in raw.split(b"\0") if field]
    paths: list[str] = []
    index = 0
    while index < len(fields):
        status = fields[index]
        code = status[:1]
        if code in {"R", "C"}:
            if index + 2 >= len(fields):
                raise RuntimeError("malformed_git_name_status")
            path = fields[index + 2]
            index += 3
        else:
            if index + 1 >= len(fields):
                raise RuntimeError("malformed_git_name_status")
            path = fields[index + 1]
            index += 2
        if path and path not in paths:
            paths.append(path)
    binary_paths = _derive_binary_paths(repo_path, base_ref, paths)
    return [path for path in paths if path not in binary_paths], len(binary_paths)


def _derive_binary_paths(repo_path: Path, base_ref: str, paths: list[str]) -> set[str]:
    if not paths:
        return set()
    try:
        raw = _git_bytes(
            repo_path,
            "diff",
            "--numstat",
            "-z",
            "--find-renames",
            f"{base_ref}..HEAD",
            "--",
            *paths,
        )
    except RuntimeError:
        return set()
    binary_paths: set[str] = set()
    requested_paths = set(paths)
    records = [item for item in raw.split(b"\0") if item]
    index = 0
    while index < len(records):
        fields = records[index].decode("utf-8", errors="replace").split("\t", 2)
        index += 1
        if len(fields) < 3 or fields[0] != "-" or fields[1] != "-":
            continue
        path = fields[2]
        if not path and index + 1 < len(records):
            # With --numstat -z, binary renames are emitted as:
            # "-\t-\t\0old-path\0new-path\0". The reviewed path is the postimage.
            postimage_index = index + 1
            path = records[postimage_index].decode("utf-8", errors="replace")
            index += 2
        if path in requested_paths:
            binary_paths.add(path)
    return binary_paths


def _derive_changed_ranges(
    repo_path: Path,
    base_ref: str,
    changed_files: list[str],
    *,
    _warnings_out: list[str] | None = None,
) -> list[JsonObject]:
    ranges: list[JsonObject] = []
    if not changed_files:
        return ranges
    try:
        diff_text = _git_stdout(
            repo_path,
            "-c",
            "core.quotepath=false",
            "diff",
            "--unified=0",
            "--no-ext-diff",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            f"{base_ref}..HEAD",
            "--",
            *changed_files,
        )
    except RuntimeError:
        if _warnings_out is not None:
            _warnings_out.append("changed_range_derivation_failed")
        return ranges
    changed_file_set = set(changed_files)
    current_path: str | None = None
    awaiting_new_header = False
    in_hunk = False
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            awaiting_new_header = False
            in_hunk = False
            continue
        if not in_hunk and line.startswith("--- "):
            awaiting_new_header = True
            continue
        if not in_hunk and awaiting_new_header and line.startswith("+++ "):
            marker = line[4:].split("\t", 1)[0]
            current_path = None if marker == "/dev/null" else _strip_diff_prefix(marker)
            awaiting_new_header = False
            continue
        if not line.startswith("@@ ") or current_path is None or current_path not in changed_file_set:
            if not in_hunk:
                awaiting_new_header = False
            continue
        in_hunk = True
        parsed = _parse_hunk_added_range(line)
        if parsed is None:
            continue
        start, end = parsed
        ranges.append({"path": current_path, "start_line": start, "end_line": end})
    return ranges


@contextmanager
def _cache_lock(repo_path: Path):
    lock_dir = repo_path / _CACHE_ROOT
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / "review_context.lock"
    deadline = time.monotonic() + _CACHE_LOCK_TIMEOUT_SECONDS
    fd: int | None = None
    while fd is None:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if _reclaim_stale_cache_lock(lock_path):
                continue
            if time.monotonic() >= deadline:
                raise RuntimeError("cache_lock_timeout")
            time.sleep(_CACHE_LOCK_POLL_SECONDS)
    try:
        os.write(fd, f"{os.getpid()}\n".encode("utf-8"))
        yield
    finally:
        os.close(fd)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _reclaim_stale_cache_lock(lock_path: Path) -> bool:
    try:
        raw_pid = lock_path.read_text(encoding="utf-8").strip().splitlines()[0]
        pid = int(raw_pid)
    except (OSError, IndexError, ValueError):
        pid = None
    if pid is not None:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            try:
                lock_path.unlink()
                return True
            except FileNotFoundError:
                return True
        except PermissionError:
            return False
        else:
            return False
    try:
        age = time.time() - lock_path.stat().st_mtime
    except OSError:
        return True
    if age > _CACHE_LOCK_TIMEOUT_SECONDS:
        try:
            lock_path.unlink()
            return True
        except FileNotFoundError:
            return True
    return False


def _strip_diff_prefix(value: str) -> str:
    if value.startswith("b/"):
        return value[2:]
    return value


def _parse_hunk_added_range(line: str) -> tuple[int, int] | None:
    parts = line.split(" ")
    plus = next((part for part in parts if part.startswith("+") and len(part) > 1), None)
    if plus is None:
        return None
    payload = plus[1:]
    try:
        if "," in payload:
            start_s, count_s = payload.split(",", 1)
            count = int(count_s)
        else:
            start_s = payload
            count = 1
        start = int(start_s)
    except ValueError:
        return None
    if count <= 0:
        return None
    end = start + count - 1
    return start, end


def _ensure_base_worktree(repo_path: Path, base_sha: str) -> tuple[Path, bool, bool]:
    root = repo_path / _CACHE_ROOT / _WORKTREE_CACHE_DIR
    root.mkdir(parents=True, exist_ok=True)
    target = root / base_sha
    recreated = False
    if target.exists():
        try:
            existing = _git_stdout(target, "rev-parse", "--verify", "HEAD^{commit}")
        except RuntimeError:
            existing = ""
        if existing == base_sha:
            _prune_worktree_cache(repo_path, keep={target})
            return target, True, False
        _remove_cached_worktree(repo_path, target)
        recreated = True
    _git_stdout(repo_path, "worktree", "prune")
    _git_stdout(repo_path, "worktree", "add", "--detach", str(target), base_sha)
    _prune_worktree_cache(repo_path, keep={target})
    return target, False, recreated


def _remove_cached_worktree(repo_path: Path, target: Path) -> None:
    try:
        subprocess.run(
            ["git", "-C", str(repo_path), "worktree", "remove", "--force", str(target)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=_GIT_COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("git_timeout") from exc
    except OSError as exc:
        raise _git_os_runtime_error(exc) from exc
    if target.exists():
        _safe_remove_cache_dir(target, root=repo_path / _CACHE_ROOT / _WORKTREE_CACHE_DIR)


def _prune_worktree_cache(repo_path: Path, *, keep: set[Path]) -> None:
    root = repo_path / _CACHE_ROOT / _WORKTREE_CACHE_DIR
    if not root.is_dir():
        return
    candidates = [path for path in root.iterdir() if path.is_dir() and path not in keep]
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    for stale in candidates[max(0, _WORKTREE_KEEP_COUNT - len(keep)) :]:
        try:
            subprocess.run(
                ["git", "-C", str(repo_path), "worktree", "remove", "--force", str(stale)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=_GIT_COMMAND_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("git_timeout") from exc
        except OSError as exc:
            raise _git_os_runtime_error(exc) from exc
        if stale.exists():
            _safe_remove_cache_dir(stale, root=root)


def _ensure_snapshot(
    repo_path: Path,
    commit_sha: str,
    *,
    cache_root: Path,
    snapshot_store: Path | None = None,
    repo_identity: str | None = None,
    repo_name: str | None = None,
) -> tuple[Path, bool, bool]:
    if snapshot_store is not None and repo_identity:
        repo_store_root = snapshot_store / repo_identity / _SNAPSHOT_STORE_SCHEMA_VERSION
        target = repo_store_root / commit_sha
        if _snapshot_matches(target, commit_sha, expected_repo_name=repo_name):
            return target, True, True
        with _snapshot_store_lock(repo_store_root, commit_sha):
            if _snapshot_matches(target, commit_sha, expected_repo_name=repo_name):
                return target, True, True
            _build_snapshot_atomically(repo_path, target, commit_sha, root=repo_store_root, repo_name=repo_name)
            return target, False, False

    target = cache_root / _CACHE_ROOT / _KG_CACHE_DIR / commit_sha
    if _snapshot_matches(target, commit_sha, expected_repo_name=repo_name):
        return target, True, False
    kg_pipeline.build_kg(repo_path, target, repo_name=repo_name)
    if not _snapshot_matches(target, commit_sha, expected_repo_name=repo_name):
        raise RuntimeError("snapshot_manifest_sha_mismatch")
    return target, False, False


def _derived_snapshot_repo_name(repo_path: Path) -> str:
    repo_name = repo_path.name.strip()
    if not repo_name:
        raise RuntimeError("repo_name_empty")
    return repo_name


def _snapshot_store_repo_identity(
    repo_path: Path,
    *,
    manifest_repo_name: str | None,
    manifest_tenant_id: str | None,
) -> str:
    tenant = manifest_tenant_id or "default"
    try:
        origin_url = _git_stdout(repo_path, "config", "--get", "remote.origin.url")
    except RuntimeError:
        origin_url = ""
    if origin_url:
        source = f"origin:{tenant}:{repo_path.name}:{origin_url}"
        return "origin-" + hashlib.sha256(source.encode("utf-8")).hexdigest()[:24]
    if manifest_repo_name:
        source = f"manifest:{tenant}:{manifest_repo_name}"
        return "manifest-" + hashlib.sha256(source.encode("utf-8")).hexdigest()[:24]
    local_root = _git_stdout(repo_path, "rev-parse", "--show-toplevel")
    source = f"local:{tenant}:{local_root}"
    return "local-" + hashlib.sha256(source.encode("utf-8")).hexdigest()[:24]


@contextmanager
def _snapshot_store_lock(repo_store_root: Path, commit_sha: str):
    repo_store_root.mkdir(parents=True, exist_ok=True)
    lock_path = repo_store_root / f"{commit_sha}.lock"
    deadline = time.monotonic() + _CACHE_LOCK_TIMEOUT_SECONDS
    fd: int | None = None
    while fd is None:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if _reclaim_stale_cache_lock(lock_path):
                continue
            if time.monotonic() >= deadline:
                raise RuntimeError("snapshot_store_lock_timeout")
            time.sleep(_CACHE_LOCK_POLL_SECONDS)
    try:
        os.write(fd, f"{os.getpid()}\n".encode("utf-8"))
        yield
    finally:
        os.close(fd)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _build_snapshot_atomically(
    repo_path: Path,
    target: Path,
    commit_sha: str,
    *,
    root: Path,
    repo_name: str | None = None,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / f".{commit_sha}.tmp-{os.getpid()}-{time.monotonic_ns()}"
    if tmp.exists():
        _safe_remove_cache_dir(tmp, root=root)
    try:
        kg_pipeline.build_kg(repo_path, tmp, repo_name=repo_name)
        if not _snapshot_matches(tmp, commit_sha, expected_repo_name=repo_name):
            raise RuntimeError("snapshot_manifest_sha_mismatch")
        if target.exists():
            _safe_remove_cache_dir(target, root=root)
        tmp.rename(target)
    except Exception:
        if tmp.exists():
            _safe_remove_cache_dir(tmp, root=root)
        raise


def _prune_snapshot_cache(cache_root: Path, *, keep: set[Path]) -> None:
    root = cache_root / _CACHE_ROOT / _KG_CACHE_DIR
    if not root.is_dir():
        return
    normalized_keep = {path.resolve() for path in keep}
    candidates = [
        path for path in root.iterdir()
        if path.is_dir() and path.resolve() not in normalized_keep
    ]
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    stale_count = max(0, len(candidates) + len(normalized_keep) - _SNAPSHOT_KEEP_COUNT)
    stale_candidates = candidates[-stale_count:] if stale_count else []
    for stale in stale_candidates:
        _safe_remove_cache_dir(stale, root=root)


def _safe_remove_cache_dir(path: Path, *, root: Path) -> None:
    resolved_root = root.resolve()
    try:
        resolved_parent = path.parent.resolve()
    except OSError as exc:
        raise RuntimeError("cache_path_resolution_failed") from exc
    if not resolved_parent.is_relative_to(resolved_root):
        raise RuntimeError("cache_path_escape")
    if path.is_symlink():
        path.unlink()
        return
    try:
        resolved_path = path.resolve()
    except OSError as exc:
        raise RuntimeError("cache_path_resolution_failed") from exc
    if not resolved_path.is_relative_to(resolved_root):
        raise RuntimeError("cache_path_escape")
    shutil.rmtree(path)


def _snapshot_matches(path: Path, commit_sha: str, *, expected_repo_name: str | None = None) -> bool:
    if not path.is_dir():
        return False
    if any(not (path / filename).exists() for filename in _SNAPSHOT_FILES):
        return False
    manifest = _read_snapshot_manifest(path)
    if not isinstance(manifest, dict):
        return False
    repo_name = _optional_string(manifest.get("repo_name"))
    if repo_name == commit_sha:
        return False
    if expected_repo_name is not None and repo_name != expected_repo_name:
        return False
    return manifest.get("commit_sha") == commit_sha


def _read_snapshot_manifest(path: Path) -> JsonObject | None:
    try:
        manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, JSONDecodeError):
        return None
    return manifest if isinstance(manifest, dict) else None


def _snapshot_repo_name(path: Path) -> str | None:
    manifest = _read_snapshot_manifest(path)
    if not isinstance(manifest, dict):
        return None
    return _optional_string(manifest.get("repo_name"))


def _validate_snapshot_pair_repo_identity(base_snapshot: Path, head_snapshot: Path) -> None:
    base_repo_name = _snapshot_repo_name(base_snapshot)
    head_repo_name = _snapshot_repo_name(head_snapshot)
    if base_repo_name != head_repo_name:
        raise SnapshotPairIdentityMismatch(
            base_repo_name=base_repo_name,
            head_repo_name=head_repo_name,
        )


def _git_stdout(repo_path: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=_GIT_COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("git_timeout") from exc
    except OSError as exc:
        raise _git_os_runtime_error(exc) from exc
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "git_command_failed")
    return result.stdout.strip()


def _git_bytes(repo_path: Path, *args: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=_GIT_COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("git_timeout") from exc
    except OSError as exc:
        raise _git_os_runtime_error(exc) from exc
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(message or "git_command_failed")
    return result.stdout


def _git_os_runtime_error(exc: OSError) -> RuntimeError:
    return RuntimeError(f"git_os_error:{exc.__class__.__name__}")


def _failure_reason(exc: RuntimeError) -> str:
    message = str(exc).strip()
    if not message:
        return "git_or_cache_resolution_failed"
    return message.splitlines()[0][:160]
