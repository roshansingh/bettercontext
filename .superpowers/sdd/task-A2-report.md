# Task A2: Graph-Diff Phase 2 — Contract-Diff Review Families

**Status:** COMPLETE

**Branch:** graph-diff-phase2 (off graph-diff-review-facts @ 18e4646)

**Commits:** 1 (pending below)

## Queries implemented

- `guard_call_removed(delta, base, head, paths)` — surviving CodeSymbol in paths loses outgoing CALLS edge; rows: {subject (head-side), removed_callee (base-side)} with bytes_ref on both sides
- `responsibility_moved(delta, base, head)` — (X→Z) removed + (Y→Z) added in same repo, grouped by Z; rows: {shared_callee, moved_from (base), moved_to (head)}
- `removed_test_references` — reused from Phase 1 unchanged

## Packet builder

`contract_diff_packet(base_snapshot_dir, head_snapshot_dir, changed_paths) -> JsonObject` in `source/kg/query/contract_diff.py`. Hypothesis row shape: `{hypothesis_id, risk_type, concrete_invariant, why, source_checks, before_refs, after_refs, ...family fields}`. Three risk_types: `guard_call_removed_drift`, `responsibility_moved_drift`, `test_reference_removed_drift`. Includes `uninstrumented_scopes` passthrough.

CLI: `supercontext-diff-kg review-packet --base-snapshot ... --head-snapshot ... --path ... --path ...`

## Tripwire resolutions

1. **bytes_ref naming**: emits `{repo, commit_sha, path, line_start, line_end}` — no `line` key (verified by test `test_bytes_ref_has_line_start_line_end`); commit_sha from manifest
2. **Path normalizer**: uses same `_norm` logic as graph_diff (literal `./` strip, not char-class lstrip)
3. **uninstrumented_scopes**: passed as list verbatim (verified by `test_uninstrumented_scopes_is_list`)
4. **Kind filter**: guard_call_removed and responsibility_moved both guard `entity.kind == "CodeSymbol"` (verified by `test_non_code_symbol_subject_excluded`)

## Test summary

25 new tests in `tests/test_contract_diff.py`; full suite 1614 passed, 2 skipped; compileall clean.

Positive + negative fixtures per query; inversion evidence (added calls not reported; unrelated add+remove not matched; cross-repo not matched); bytes_ref shape tests; determinism tests; build_kg round-trip packet tests.

## Product boundary

`source/kg/product/` not modified. Product-side splice (wiring into `review_hypotheses.py`) is Phase B, pending branch convergence with PR #175.
