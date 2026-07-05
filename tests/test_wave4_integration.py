from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from source.kg.build.pipeline import build_kg
from source.kg.core.models import canonical_json
from source.kg.product.mcp_tools import call_tool, tool_definitions
from source.kg.product.output_budget import (
    REVIEW_CONTEXT_FOLLOWUP_COMBINED_MAX_CHARS,
    REVIEW_CONTEXT_MAX_CHARS,
    _attach_review_context_followup_packets,
    enforce_review_context_budget,
)
from source.kg.query.snapshot import KgSnapshot


def _run(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        [*args],
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(f"{' '.join(args)} failed: {result.stderr or result.stdout}")
    return result.stdout.strip()


def _commit_all(repo: Path, message: str) -> str:
    _run(repo, "git", "add", ".")
    _run(repo, "git", "commit", "-m", message)
    return _run(repo, "git", "rev-parse", "HEAD")


def _init_repo(root: Path) -> Path:
    repo = root / "generic_repo"
    repo.mkdir()
    _run(repo, "git", "init")
    _run(repo, "git", "config", "user.email", "wave4@example.invalid")
    _run(repo, "git", "config", "user.name", "Wave Four")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "core.py").write_text(
        "def ready():\n"
        "    return True\n\n"
        "def widget():\n"
        "    if ready():\n"
        "        return 'old'\n"
        "    return 'fallback'\n",
        encoding="utf-8",
    )
    _commit_all(repo, "base")
    (repo / "pkg" / "core.py").write_text(
        "def ready():\n"
        "    return True\n\n"
        "def widget():\n"
        "    return 'new'\n",
        encoding="utf-8",
    )
    _commit_all(repo, "head")
    return repo


def _placeholder_kg(root: Path) -> KgSnapshot:
    repo = _init_repo(root)
    out = root / "placeholder-kg"
    build_kg(repo, out)
    return KgSnapshot(out)


def _no_semantic_splice(**kwargs):
    stats = kwargs.get("_stats_out")
    if isinstance(stats, dict):
        stats.update({"rows_generated": 0, "rows_verified": 0, "rows_unverified": 0})
    return kwargs["review_hypotheses"], "active"


def _wave4_hyp(index: int, *, label: str | None = None) -> dict:
    return {
        "hypothesis_id": f"hypothesis:wave4:{index}",
        "label": label or f"wave4-followup-{index}",
        "risk_type": "contract_semantic_diff",
        "confidence": 0.86,
        "specificity": "high",
        "why": "Changed contract around a concrete symbol.",
        "postable_claim": f"Contract changed for Widget.handle candidate {index}.",
        "concrete_invariant": "Callers still expect the old Widget.handle contract.",
        "cause": {
            "repo": "generic_repo",
            "path": "pkg/core.py",
            "line_start": 10 + index,
            "line_end": 11 + index,
            "qualname": "Widget.handle",
        },
        "consequence": {
            "repo": "generic_repo",
            "path": "pkg/core.py",
            "line_start": 30 + index,
            "line_end": 31 + index,
            "qualname": "Widget.caller",
        },
        "source_spans": [
            {
                "repo": "generic_repo",
                "path": "pkg/core.py",
                "line_start": 10 + index,
                "line_end": 11 + index,
                "qualname": "Widget.handle",
            }
        ],
        "negative_checks": ["Verify whether callers were updated."],
        "derivation": "inferred_llm",
    }


def _wave4_followup_budget_packet() -> dict:
    full = [_wave4_hyp(index) for index in range(3)]
    returned = [full[0]]
    return {
        "status": "success",
        "repo": "generic_repo",
        "requested_repo": "generic_repo",
        "summary": {"changed_files": ["pkg/core.py"]},
        "review_quality_status": {
            "coverage_status": "complete",
            "specific_hypothesis_count": 1,
            "generic_hypothesis_count": 0,
            "specificity": "high",
            "recommended_action": "use_supercontext_packet",
            "reason": "Packet contains specific hypotheses.",
            "review_readiness": "packet_ready",
        },
        "review_hypotheses": returned,
        "review_answer_packet": {"top_review_hypotheses": returned},
        "review_lead_status": {"coverage_status": "complete"},
        "review_leads": {
            "changed_files": ["pkg/core.py"],
            "changed_symbols": [],
            "direct_callers": [],
            "direct_callees": [],
            "transitive_callers": [],
            "source_coordinates": [],
        },
        "_full_pre_cap_hypotheses": full,
    }


class Wave4ReviewEntryResolutionTests(unittest.TestCase):
    def test_review_context_schema_and_description_explain_minimal_entry(self) -> None:
        definitions = {row["name"]: row for row in tool_definitions()}
        review = definitions["review_context"]
        properties = review["inputSchema"]["properties"]
        self.assertIn("repo_path", properties)
        self.assertIn("base_ref", properties)
        self.assertEqual(review["inputSchema"].get("required"), [])
        self.assertIn("Call this FIRST", review["description"])
        self.assertIn("repo_path plus base_ref", review["description"])
        self.assertIn("repo_path/.supercontext", review["description"])
        self.assertIn(".git/info/exclude", review["description"])
        self.assertIn("combined response may use up to 20K chars", review["description"])

    def test_review_context_derives_args_builds_and_reuses_cached_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            placeholder = root / "placeholder"
            build_kg(repo, placeholder)
            kg = KgSnapshot(placeholder)

            from source.kg.product import review_entry_resolution

            real_build = review_entry_resolution.kg_pipeline.build_kg
            build_calls: list[tuple[str, str]] = []

            def counted_build(repo_path, output_dir, *args, **kwargs):
                build_calls.append((str(repo_path), str(output_dir)))
                return real_build(repo_path, output_dir, *args, **kwargs)

            with patch(
                "source.kg.product.review_entry_resolution.kg_pipeline.build_kg",
                side_effect=counted_build,
            ), patch(
                "source.kg.product.mcp_tools._splice_semantic_diff_hypotheses",
                side_effect=_no_semantic_splice,
            ):
                first = call_tool(kg, "review_context", {"repo_path": str(repo), "base_ref": "HEAD~1"})
                second = call_tool(kg, "review_context", {"repo_path": str(repo), "base_ref": "HEAD~1"})

            self.assertEqual(first["entry_resolution"]["mode"], "derived")
            self.assertEqual(first["entry_resolution"]["status"], "active")
            self.assertEqual(first["entry_resolution"]["changed_files_count"], 1)
            self.assertEqual(first["review_leads"]["changed_files"], ["pkg/core.py"])
            self.assertTrue(
                first["review_leads"]["changed_symbols"],
                "derived call should use the built head snapshot",
            )
            self.assertEqual(second["entry_resolution"]["status"], "active")
            self.assertEqual(len(build_calls), 2, "base and head snapshots should be built once, then reused")
            cache_root = repo / ".supercontext" / "kg"
            self.assertTrue(cache_root.is_dir())
            self.assertEqual(len([path for path in cache_root.iterdir() if path.is_dir()]), 2)
            exclude_text = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
            self.assertIn(".supercontext/", exclude_text)

    def test_review_context_non_git_dir_returns_structured_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kg = _placeholder_kg(root)
            not_repo = root / "not_repo"
            not_repo.mkdir()

            result = call_tool(kg, "review_context", {"repo_path": str(not_repo), "base_ref": "HEAD~1"})

            self.assertEqual(result["status"], "error")
            self.assertEqual(result["entry_resolution"]["status"], "failed")
            self.assertIn("repo_path_not_git_repo", result["entry_resolution"]["derivation_failures"])

    def test_review_context_unknown_base_ref_returns_structured_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            placeholder = root / "placeholder"
            build_kg(repo, placeholder)
            kg = KgSnapshot(placeholder)

            result = call_tool(kg, "review_context", {"repo_path": str(repo), "base_ref": "missing-ref"})

            self.assertEqual(result["status"], "error")
            self.assertEqual(result["entry_resolution"]["status"], "failed")
            self.assertTrue(result["entry_resolution"]["derivation_failures"])

    def test_review_context_option_like_base_ref_returns_structured_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            placeholder = root / "placeholder"
            build_kg(repo, placeholder)
            kg = KgSnapshot(placeholder)

            result = call_tool(kg, "review_context", {"repo_path": str(repo), "base_ref": "--help"})

            self.assertEqual(result["status"], "error")
            self.assertEqual(result["entry_resolution"]["status"], "failed")
            self.assertIn("invalid_base_ref", result["entry_resolution"]["derivation_failures"])

    def test_repo_only_legacy_call_returns_structured_missing_changed_files_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kg = _placeholder_kg(root)

            result = call_tool(kg, "review_context", {"repo": "generic_repo"})

            self.assertEqual(result["status"], "error")
            self.assertEqual(result["entry_resolution"]["mode"], "explicit")
            self.assertIn("missing_changed_files", result["entry_resolution"]["derivation_failures"])

    def test_empty_explicit_changed_files_returns_structured_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kg = _placeholder_kg(root)

            result = call_tool(kg, "review_context", {"repo": "generic_repo", "changed_files": []})

            self.assertEqual(result["status"], "error")
            self.assertIn("empty_changed_files", result["entry_resolution"]["derivation_failures"])

    def test_review_context_empty_diff_is_explicit_no_changes_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            placeholder = root / "placeholder"
            build_kg(repo, placeholder)
            kg = KgSnapshot(placeholder)

            with patch(
                "source.kg.product.review_entry_resolution.kg_pipeline.build_kg",
                side_effect=AssertionError("empty diff must not build snapshots"),
            ):
                result = call_tool(kg, "review_context", {"repo_path": str(repo), "base_ref": "HEAD"})

            self.assertEqual(result["status"], "no_changes_detected")
            self.assertEqual(result["entry_resolution"]["status"], "no_changes_detected")
            self.assertEqual(result["entry_resolution"]["changed_files_count"], 0)
            self.assertNotIn("snapshots", result["entry_resolution"])

    def test_review_context_binary_only_diff_is_not_reported_as_no_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "binary_repo"
            repo.mkdir()
            _run(repo, "git", "init")
            _run(repo, "git", "config", "user.email", "wave4@example.invalid")
            _run(repo, "git", "config", "user.name", "Wave Four")
            (repo / "pkg").mkdir()
            (repo / "pkg" / "core.py").write_text("VALUE = 1\n", encoding="utf-8")
            (repo / "asset.bin").write_bytes(b"\x00\x01\x02")
            _commit_all(repo, "base")
            (repo / "asset.bin").write_bytes(b"\x00\xff\x02\x03")
            _commit_all(repo, "binary change")
            placeholder = root / "placeholder"
            build_kg(repo, placeholder)
            kg = KgSnapshot(placeholder)

            result = call_tool(kg, "review_context", {"repo_path": str(repo), "base_ref": "HEAD~1"})

            self.assertEqual(result["status"], "binary_only_changes")
            self.assertEqual(result["entry_resolution"]["status"], "binary_only_changes")
            self.assertEqual(result["entry_resolution"]["changed_files_count"], 0)
            self.assertEqual(result["entry_resolution"]["binary_files_skipped"], 1)
            self.assertIn("all detected changes were binary", result["review_quality_status"]["reason"])

    def test_legacy_explicit_review_context_call_does_not_gain_entry_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            placeholder = root / "placeholder"
            build_kg(repo, placeholder)
            kg = KgSnapshot(placeholder)

            result = call_tool(
                kg,
                "review_context",
                {"repo": "generic_repo", "changed_files": ["pkg/core.py"], "execute_followups": False},
            )

            self.assertNotIn("entry_resolution", result)
            self.assertNotIn("entry_resolution", result.get("review_answer_packet", {}))

    def test_mixed_call_with_complete_legacy_args_does_not_fail_missing_base_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            placeholder = root / "placeholder"
            build_kg(repo, placeholder)
            kg = KgSnapshot(placeholder)

            result = call_tool(
                kg,
                "review_context",
                {
                    "repo": "generic_repo",
                    "changed_files": ["pkg/core.py"],
                    "repo_path": str(repo),
                    "execute_followups": False,
                },
            )

            self.assertNotEqual(result["status"], "error")
            self.assertNotIn("entry_resolution", result)

    def test_dirty_worktree_fails_before_snapshot_cache_then_committed_call_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            placeholder = root / "placeholder"
            build_kg(repo, placeholder)
            kg = KgSnapshot(placeholder)
            (repo / "pkg" / "core.py").write_text(
                "def ready():\n"
                "    return True\n\n"
                "def widget():\n"
                "    return 'dirty'\n",
                encoding="utf-8",
            )

            dirty = call_tool(kg, "review_context", {"repo_path": str(repo), "base_ref": "HEAD~1"})

            self.assertEqual(dirty["status"], "error")
            self.assertIn("dirty_worktree", dirty["entry_resolution"]["derivation_failures"])
            self.assertFalse((repo / ".supercontext" / "kg").exists())

            _commit_all(repo, "commit dirty change")
            clean = call_tool(kg, "review_context", {"repo_path": str(repo), "base_ref": "HEAD~1"})
            reused = call_tool(kg, "review_context", {"repo_path": str(repo), "base_ref": "HEAD~1"})

            self.assertNotEqual(clean["status"], "error")
            self.assertNotEqual(reused["status"], "error")
            self.assertEqual(clean["entry_resolution"]["status"], "active")
            self.assertFalse(clean["entry_resolution"]["snapshots"]["head_snapshot_reused"])
            self.assertTrue(reused["entry_resolution"]["snapshots"]["head_snapshot_reused"])

    def test_derived_base_snapshot_uses_merge_base_not_advanced_base_tip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "diverged_repo"
            repo.mkdir()
            _run(repo, "git", "init")
            _run(repo, "git", "config", "user.email", "wave4@example.invalid")
            _run(repo, "git", "config", "user.name", "Wave Four")
            (repo / "pkg").mkdir()
            (repo / "pkg" / "core.py").write_text(
                "def widget():\n"
                "    return 'base'\n",
                encoding="utf-8",
            )
            merge_base = _commit_all(repo, "base")
            _run(repo, "git", "checkout", "-b", "feature")
            (repo / "pkg" / "core.py").write_text(
                "def widget():\n"
                "    return 'feature'\n",
                encoding="utf-8",
            )
            _commit_all(repo, "feature")
            _run(repo, "git", "checkout", "-b", "base_branch", merge_base)
            (repo / "pkg" / "base_only.py").write_text("VALUE = 'base-tip'\n", encoding="utf-8")
            base_tip = _commit_all(repo, "base tip")
            _run(repo, "git", "checkout", "feature")
            placeholder = root / "placeholder"
            build_kg(repo, placeholder)
            kg = KgSnapshot(placeholder)

            with patch(
                "source.kg.product.mcp_tools._splice_semantic_diff_hypotheses",
                side_effect=_no_semantic_splice,
            ):
                result = call_tool(kg, "review_context", {"repo_path": str(repo), "base_ref": "base_branch"})

            self.assertEqual(result["entry_resolution"]["base_sha"], merge_base)
            self.assertEqual(result["entry_resolution"]["base_ref_sha"], base_tip)
            self.assertEqual(result["review_leads"]["changed_files"], ["pkg/core.py"])
            manifest_path = Path(result["entry_resolution"]["snapshots"]["base_snapshot"]) / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["commit_sha"], merge_base)

    def test_git_timeout_is_structured_derivation_failure(self) -> None:
        import subprocess as subprocess_module
        from source.kg.product.review_entry_resolution import _git_stdout

        with tempfile.TemporaryDirectory() as tmp:
            with patch(
                "source.kg.product.review_entry_resolution.subprocess.run",
                side_effect=subprocess_module.TimeoutExpired(cmd=["git"], timeout=60),
            ):
                with self.assertRaisesRegex(RuntimeError, "git_timeout"):
                    _git_stdout(Path(tmp), "status")

    def test_dirty_worktree_git_failure_returns_structured_entry_resolution_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            placeholder = root / "placeholder"
            build_kg(repo, placeholder)
            kg = KgSnapshot(placeholder)

            with patch(
                "source.kg.product.review_entry_resolution._is_dirty_worktree",
                side_effect=RuntimeError("git_timeout"),
            ):
                result = call_tool(kg, "review_context", {"repo_path": str(repo), "base_ref": "HEAD~1"})

            self.assertEqual(result["status"], "error")
            self.assertEqual(result["entry_resolution"]["status"], "failed")
            self.assertIn("git_timeout", result["entry_resolution"]["derivation_failures"])

    def test_cache_lock_failure_returns_structured_entry_resolution_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            placeholder = root / "placeholder"
            build_kg(repo, placeholder)
            kg = KgSnapshot(placeholder)

            with patch(
                "source.kg.product.review_entry_resolution._cache_lock",
                side_effect=RuntimeError("cache_lock_timeout"),
            ):
                result = call_tool(kg, "review_context", {"repo_path": str(repo), "base_ref": "HEAD~1"})

            self.assertEqual(result["status"], "error")
            self.assertEqual(result["entry_resolution"]["status"], "failed")
            self.assertIn("cache_lock_timeout", result["entry_resolution"]["derivation_failures"])

    def test_changed_range_parser_does_not_treat_added_plus_plus_line_as_file_header(self) -> None:
        from source.kg.product.review_entry_resolution import _derive_changed_files, _derive_changed_ranges

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "range_repo"
            repo.mkdir()
            _run(repo, "git", "init")
            _run(repo, "git", "config", "user.email", "wave4@example.invalid")
            _run(repo, "git", "config", "user.name", "Wave Four")
            _run(repo, "git", "config", "diff.mnemonicPrefix", "true")
            (repo / "pkg").mkdir()
            (repo / "pkg" / "core.py").write_text(
                "def first():\n"
                "    return 'old'\n\n"
                "def second():\n"
                "    return 'old'\n",
                encoding="utf-8",
            )
            _commit_all(repo, "base")
            (repo / "pkg" / "core.py").write_text(
                "def first():\n"
                "    marker = True\n"
                "++ content not a header\n"
                "    return 'new'\n\n"
                "def second():\n"
                "    value = 'new'\n"
                "    return 'new'\n",
                encoding="utf-8",
            )
            _commit_all(repo, "head")

            files, _binary = _derive_changed_files(repo, "HEAD~1")
            ranges = _derive_changed_ranges(repo, "HEAD~1", files)

            self.assertTrue(ranges)
            self.assertEqual({row["path"] for row in ranges}, {"pkg/core.py"})

    def test_top_of_file_pure_deletion_does_not_emit_zero_start_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "delete_top_repo"
            repo.mkdir()
            _run(repo, "git", "init")
            _run(repo, "git", "config", "user.email", "wave4@example.invalid")
            _run(repo, "git", "config", "user.name", "Wave Four")
            (repo / "pkg").mkdir()
            (repo / "pkg" / "core.py").write_text(
                "import os\n"
                "import sys\n"
                "\n"
                "def widget():\n"
                "    return 'ok'\n",
                encoding="utf-8",
            )
            _commit_all(repo, "base")
            (repo / "pkg" / "core.py").write_text(
                "\n"
                "def widget():\n"
                "    return 'ok'\n",
                encoding="utf-8",
            )
            _commit_all(repo, "delete leading imports")
            placeholder = root / "placeholder"
            build_kg(repo, placeholder)
            kg = KgSnapshot(placeholder)

            with patch(
                "source.kg.product.mcp_tools._splice_semantic_diff_hypotheses",
                side_effect=_no_semantic_splice,
            ):
                result = call_tool(kg, "review_context", {"repo_path": str(repo), "base_ref": "HEAD~1"})

            self.assertNotEqual(result["status"], "error")
            self.assertEqual(result["entry_resolution"]["changed_files_count"], 1)
            self.assertEqual(result["entry_resolution"]["changed_ranges_count"], 0)

    def test_changed_range_parser_uses_one_git_diff_for_all_files(self) -> None:
        from source.kg.product.review_entry_resolution import _derive_changed_ranges

        diff_text = "\n".join(
            [
                "diff --git a/pkg/a.py b/pkg/a.py",
                "--- a/pkg/a.py",
                "+++ b/pkg/a.py",
                "@@ -1 +1,2 @@",
                "+a = 2",
                "+a = 3",
                "diff --git a/pkg/b.py b/pkg/b.py",
                "--- a/pkg/b.py",
                "+++ b/pkg/b.py",
                "@@ -4,0 +5 @@",
                "+b = 1",
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            with patch("source.kg.product.review_entry_resolution._git_stdout", return_value=diff_text) as git_stdout:
                ranges = _derive_changed_ranges(repo, "HEAD~1", ["pkg/a.py", "pkg/b.py"])

        self.assertEqual(git_stdout.call_count, 1)
        self.assertEqual(
            ranges,
            [
                {"path": "pkg/a.py", "start_line": 1, "end_line": 2},
                {"path": "pkg/b.py", "start_line": 5, "end_line": 5},
            ],
        )

    def test_changed_range_derivation_failure_is_reported_as_warning(self) -> None:
        from source.kg.product.review_entry_resolution import _derive_changed_ranges

        warnings: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            with patch(
                "source.kg.product.review_entry_resolution._git_stdout",
                side_effect=RuntimeError("git diff failed"),
            ):
                ranges = _derive_changed_ranges(
                    repo,
                    "HEAD~1",
                    ["pkg/core.py"],
                    _warnings_out=warnings,
                )

        self.assertEqual(ranges, [])
        self.assertEqual(warnings, ["changed_range_derivation_failed"])

    def test_changed_range_parser_handles_space_path_with_git_quotepath_enabled(self) -> None:
        from source.kg.product.review_entry_resolution import _derive_changed_files, _derive_changed_ranges

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "space_path_repo"
            repo.mkdir()
            _run(repo, "git", "init")
            _run(repo, "git", "config", "user.email", "wave4@example.invalid")
            _run(repo, "git", "config", "user.name", "Wave Four")
            _run(repo, "git", "config", "core.quotepath", "true")
            (repo / "pkg").mkdir()
            path = repo / "pkg" / "has space.py"
            path.write_text("def widget():\n    return 'old'\n", encoding="utf-8")
            _commit_all(repo, "base")
            path.write_text("def widget():\n    value = 'new'\n    return value\n", encoding="utf-8")
            _commit_all(repo, "head")

            files, binary = _derive_changed_files(repo, "HEAD~1")
            ranges = _derive_changed_ranges(repo, "HEAD~1", files)

        self.assertEqual(binary, 0)
        self.assertEqual(files, ["pkg/has space.py"])
        self.assertTrue(ranges)
        self.assertEqual({row["path"] for row in ranges}, {"pkg/has space.py"})

    def test_malformed_hunk_range_is_skipped_not_raised(self) -> None:
        from source.kg.product.review_entry_resolution import _parse_hunk_added_range

        self.assertIsNone(_parse_hunk_added_range("@@ -1 +not-a-line @@"))
        self.assertIsNone(_parse_hunk_added_range("@@ -1 +2,not-a-count @@"))
        self.assertIsNone(_parse_hunk_added_range("@@ -1,2 +0,0 @@"))

    def test_deleted_file_name_status_dedupes_duplicate_rows(self) -> None:
        from source.kg.product import review_entry_resolution

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            with patch(
                "source.kg.product.review_entry_resolution._git_bytes",
                return_value=b"D\0pkg/deleted.py\0D\0pkg/deleted.py\0",
            ), patch(
                "source.kg.product.review_entry_resolution._derive_binary_paths",
                return_value=set(),
            ):
                files, binary = review_entry_resolution._derive_changed_files(repo, "HEAD~1")

        self.assertEqual(files, ["pkg/deleted.py"])
        self.assertEqual(binary, 0)

    def test_binary_rename_numstat_marks_postimage_path_binary(self) -> None:
        from source.kg.product.review_entry_resolution import _derive_binary_paths

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            with patch(
                "source.kg.product.review_entry_resolution._git_bytes",
                return_value=b"-\t-\t\0asset-old.bin\0asset-new.bin\0",
            ):
                binary_paths = _derive_binary_paths(repo, "HEAD~1", ["asset-new.bin"])

        self.assertEqual(binary_paths, {"asset-new.bin"})

    def test_base_worktree_sha_mismatch_self_heals(self) -> None:
        from source.kg.product.review_entry_resolution import _ensure_base_worktree

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            base_sha = _run(repo, "git", "rev-parse", "HEAD~1")
            stale = repo / ".supercontext" / "worktrees" / base_sha[:12]
            stale.mkdir(parents=True)
            (stale / "stale.txt").write_text("not a worktree", encoding="utf-8")

            worktree, reused, recreated = _ensure_base_worktree(repo, base_sha)

            self.assertFalse(reused)
            self.assertTrue(recreated)
            self.assertEqual(_run(worktree, "git", "rev-parse", "HEAD"), base_sha)

    def test_base_worktree_prunes_stale_registration_after_cache_dir_deleted(self) -> None:
        import shutil
        from source.kg.product.review_entry_resolution import _ensure_base_worktree

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            base_sha = _run(repo, "git", "rev-parse", "HEAD~1")
            first, _reused, _recreated = _ensure_base_worktree(repo, base_sha)
            self.assertTrue(first.is_dir())
            shutil.rmtree(repo / ".supercontext")

            second, reused, recreated = _ensure_base_worktree(repo, base_sha)

            self.assertFalse(reused)
            self.assertFalse(recreated)
            self.assertEqual(_run(second, "git", "rev-parse", "HEAD"), base_sha)

    def test_snapshot_cache_prunes_old_dirs_but_keeps_active_pair(self) -> None:
        from source.kg.product.review_entry_resolution import _prune_snapshot_cache

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_root = root / ".supercontext" / "kg"
            cache_root.mkdir(parents=True)
            dirs = []
            for index in range(6):
                path = cache_root / f"sha-{index}"
                path.mkdir()
                (path / "manifest.json").write_text(json.dumps({"commit_sha": f"sha-{index}"}), encoding="utf-8")
                dirs.append(path)
            keep = {dirs[0], dirs[5]}

            _prune_snapshot_cache(root, keep=keep)

            remaining = {path.name for path in cache_root.iterdir() if path.is_dir()}
            self.assertLessEqual(len(remaining), 4)
            self.assertIn("sha-0", remaining)
            self.assertIn("sha-5", remaining)

    def test_cache_lock_reclaims_dead_pid_lock(self) -> None:
        from source.kg.product.review_entry_resolution import _cache_lock

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            lock_dir = repo / ".supercontext"
            lock_dir.mkdir()
            lock_path = lock_dir / "review_context.lock"
            lock_path.write_text("99999999\n", encoding="utf-8")

            with _cache_lock(repo):
                self.assertTrue(lock_path.exists())

            self.assertFalse(lock_path.exists())


class Wave4ReviewFollowupExecutionTests(unittest.TestCase):
    def test_budget_executes_internal_followup_packets_for_truncated_specific_rows(self) -> None:
        packet = _wave4_followup_budget_packet()
        packet["review_leads"]["source_coordinates"] = [
            {
                "repo": "generic_repo",
                "path": "pkg/core.py",
                "line_start": index,
                "line_end": index,
                "text": "x" * 500,
            }
            for index in range(100)
        ]
        self.assertGreater(len(canonical_json(packet)), REVIEW_CONTEXT_MAX_CHARS)
        result = enforce_review_context_budget(
            packet,
            execute_followups=True,
        )

        status = result["review_quality_status"]
        self.assertEqual(status["review_readiness"], "packet_ready")
        self.assertEqual(status["followups_executed"], 1)
        self.assertNotIn("inspection_areas", status)
        self.assertNotIn("_full_pre_cap_hypotheses", result)
        self.assertLessEqual(len(canonical_json(result)), REVIEW_CONTEXT_FOLLOWUP_COMBINED_MAX_CHARS)
        packets = result.get("followup_packets")
        self.assertIsInstance(packets, list)
        self.assertEqual(len(packets), 1)
        followup_rows = packets[0]["rows"]
        self.assertEqual(
            {row["hypothesis_id"] for row in followup_rows},
            {"hypothesis:wave4:1", "hypothesis:wave4:2"},
        )
        self.assertEqual(packets[0]["followup_depth"], 1)

    def test_empty_followup_packets_are_not_attached_or_counted(self) -> None:
        result = {"review_quality_status": {"review_readiness": "needs_followup"}}
        demoted_rows = [dict(_wave4_hyp(index), specificity="low") for index in range(2)]

        _attach_review_context_followup_packets(
            result,
            [],
            full_pre_cap_hypotheses=demoted_rows,
            parent_max_chars=15_000,
            execute_followups=True,
        )

        status = result["review_quality_status"]
        self.assertEqual(status["review_readiness"], "needs_followup")
        self.assertEqual(status["followups_executed"], 0)
        self.assertEqual(status["followup_execution"], "no_truncated_specific_rows")
        self.assertNotIn("followup_packets", result)

    def test_execute_followups_false_preserves_needs_followup_contract(self) -> None:
        result = enforce_review_context_budget(
            _wave4_followup_budget_packet(),
            execute_followups=False,
        )

        status = result["review_quality_status"]
        self.assertEqual(status["review_readiness"], "needs_followup")
        self.assertEqual(status["followups_executed"], 0)
        self.assertEqual(status["followup_execution"], "disabled_by_request")
        self.assertIn("inspection_areas", status)
        self.assertNotIn("followup_packets", result)

    def test_combined_cap_trimming_preserves_residual_followup_count(self) -> None:
        full = [_wave4_hyp(index) for index in range(5)]
        result = {
            "review_quality_status": {"review_readiness": "needs_followup"},
            "review_hypotheses": [full[0]],
            "padding": "x" * 1000,
        }

        with patch("source.kg.product.output_budget.REVIEW_CONTEXT_FOLLOWUP_COMBINED_MAX_CHARS", 4000):
            _attach_review_context_followup_packets(
                result,
                [full[0]],
                full_pre_cap_hypotheses=full,
                parent_max_chars=100,
                execute_followups=True,
            )

        status = result["review_quality_status"]
        packets = result.get("followup_packets")
        self.assertEqual(status["review_readiness"], "needs_followup")
        self.assertEqual(status["followups_executed"], 1)
        self.assertEqual(status["followup_execution"], "executed")
        self.assertEqual(status["remaining_followup_candidate_count"], 3)
        self.assertIsInstance(packets, list)
        self.assertEqual(len(packets), 1)
        self.assertEqual(len(packets[0]["rows"]), 1)

    def test_lead_only_budget_packet_preserves_entry_resolution(self) -> None:
        from source.kg.product.output_budget import _review_lead_only_budget_packet

        entry_resolution = {"mode": "derived", "status": "active", "changed_files_count": 1}
        result = {
            "tool": "review_context",
            "status": "success",
            "repo": "generic_repo",
            "requested_repo": "generic_repo",
            "entry_resolution": entry_resolution,
            "review_answer_packet": {"status": "success"},
            "review_lead_status": {"coverage_status": "useful"},
            "review_quality_status": {"review_readiness": "packet_ready"},
            "review_leads": {"changed_files": ["pkg/core.py"]},
            "review_hypotheses": [],
            "output_budget": {"engine_version": "test"},
        }

        compact = _review_lead_only_budget_packet(
            result,
            measured_chars=30_000,
            max_chars=1_000,
            truncated_sections=set(),
            original_hypotheses=[],
        )

        self.assertEqual(compact["entry_resolution"], entry_resolution)
        self.assertEqual(compact["review_answer_packet"]["entry_resolution"], entry_resolution)
