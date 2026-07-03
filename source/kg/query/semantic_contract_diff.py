from __future__ import annotations

"""Semantic contract-diff via LLM hypothesis generation (S2).

Covers bug classes deterministic families can only chase one-by-one:
  - reparented onto abstract base
  - returns before checking
  - docstring describes old contract
  - ownership moved (semantic variant)

ADR-0004 compliance: all output rows carry derivation="inferred_llm" and are
candidate-class hypotheses only — never persisted as canonical facts.

Cost bounds (owner constraints):
  - Only symbols whose base/head body text actually differs (cheap text pre-filter)
  - Max 12 symbols per review_context call (cluster-ranked: one per changed-file cluster first)
  - Body text truncated to ~6 000 chars per side
  - <=2 hypotheses per symbol from the model
"""

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from source.kg.core.models import JsonObject

if TYPE_CHECKING:
    from source.kg.query.snapshot import KgSnapshot
    from source.kg.integrations.semantic_llm import SemanticDiffLlmClient


_BODY_CHAR_LIMIT = 6000
_MAX_SYMBOLS = 12
_MAX_HYPS_PER_SYMBOL = 2

_PROMPT_TEMPLATE = (
    "Compare the before/after implementation of {qualname}.\n\n"
    "BEFORE:\n{before}\n\nAFTER:\n{after}\n\n"
    "State up to 2 behavioral contract changes as falsifiable claims. "
    'JSON array: [{{"claim": "...", "cause_line": <head line no. int>, '
    '"consequence": "one sentence", "negative_check": "...", "category": "..."}}]'
)

_REQUIRED_KEYS = {"claim", "cause_line", "consequence", "negative_check", "category"}

# String clamp limits
_CLAMP_CLAIM = 300
_CLAMP_CONSEQUENCE = 300
_CLAMP_NEGATIVE_CHECK = 200
_CLAMP_CATEGORY = 50

# 4x drop thresholds (items exceeding these are garbage-signalled and dropped)
_DROP_CLAIM = 1200
_DROP_CONSEQUENCE = 1200
_DROP_NEGATIVE_CHECK = 800
_DROP_CATEGORY = 200

# Auth error substrings (checked on lowercased exception message)
_AUTH_SUBSTRINGS = ("auth", "unauthorized", "401", "api_key", "apikey")


def _safe_resolve(root: Path, path: str) -> Path | None:
    """Resolve (root / path) and verify it stays under root.

    Returns the resolved Path on success, None on:
      - absolute path (security: Path('/...') escapes root in pathlib)
      - traversal outside root after resolve()
    Callers must treat None as "skip this symbol" and surface it via a counter,
    not silently drop it — the caller logs skipped_unsafe_path counts.
    """
    if Path(path).is_absolute():
        return None
    resolved_root = root.resolve()
    candidate = (root / path).resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError:
        return None
    return candidate


def _read_body(root: Path, path: str, line_start: int | None, line_end: int | None) -> str:
    """Read symbol body from checkout root. Returns empty string on any error or unsafe path."""
    safe = _safe_resolve(root, path)
    if safe is None:
        return ""
    try:
        full = safe.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if line_start is None:
        return full[:_BODY_CHAR_LIMIT]
    lines = full.splitlines(keepends=True)
    start_idx = max(0, line_start - 1)
    # end_line=None means read from line_start to EOF (whole file when span unknown)
    if line_end is None:
        body = "".join(lines[start_idx:])
    else:
        body = "".join(lines[start_idx:line_end])
    return body[:_BODY_CHAR_LIMIT]


def _hyp_id(claim: str, path: str, line: int) -> str:
    payload = f"contract_semantic_diff|{claim}|{path}|{line}"
    return f"hypothesis:contract_semantic_diff:{hashlib.sha256(payload.encode()).hexdigest()[:16]}"


def _validate_item(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    return _REQUIRED_KEYS.issubset(item.keys())


def _is_auth_error(exc: Exception) -> bool:
    """Return True if the exception looks like an auth/API-key error."""
    msg = str(exc).lower()
    return any(sub in msg for sub in _AUTH_SUBSTRINGS)


def _cluster_rank(
    changed_symbols: list[JsonObject],
    max_symbols: int,
) -> list[JsonObject]:
    """One symbol per changed-file cluster first, then fill remaining slots."""
    seen_paths: set[str] = set()
    first_pass: list[JsonObject] = []
    second_pass: list[JsonObject] = []
    for sym in changed_symbols:
        path = str((sym.get("properties") or {}).get("path") or "")
        if path not in seen_paths:
            seen_paths.add(path)
            first_pass.append(sym)
        else:
            second_pass.append(sym)
    combined = first_pass + second_pass
    return combined[:max_symbols]


def semantic_contract_diff(
    base_snapshot: "KgSnapshot",
    head_snapshot: "KgSnapshot",
    base_root: Path,
    head_root: Path,
    changed_symbols: list[JsonObject],
    client: "SemanticDiffLlmClient",
    max_symbols: int = _MAX_SYMBOLS,
) -> tuple[list[JsonObject], str]:
    """Generate LLM-backed contract-diff hypothesis rows for changed symbols.

    Returns (rows, status) where status is one of:
      "active"     — completed normally (zero or more rows)
      "no_api_key" — auth/API-key error detected
      "llm_error"  — all LLM calls failed (non-auth)
      "partial"    — some calls succeeded, some failed

    Each row is a candidate-class hypothesis only — never stored as a canonical fact.

    Args:
        base_snapshot: KG snapshot for the base commit.
        head_snapshot: KG snapshot for the head commit.
        base_root: Filesystem path to the base checkout (for reading body text).
        head_root: Filesystem path to the head checkout (for reading body text).
        changed_symbols: Symbol entities (from head_snapshot) that changed in this PR.
        client: SemanticDiffLlmClient instance to call for completions.
        max_symbols: Cost cap — at most this many symbols sent to the LLM.
    """
    if not changed_symbols:
        return [], "active"

    # Build URN → entity for both snapshots
    base_by_urn: dict[str, JsonObject] = {e["urn"]: e for e in base_snapshot.entities}

    # Problem C fix: prefilter (read bodies, keep only differing) BEFORE cap
    differing: list[tuple[JsonObject, str, str]] = []  # (entity, base_body, head_body)
    read_failures = 0  # P1 deeper fix: count symbols where body reads returned ""
    calls_attempted_prefilter = 0  # symbols that had a valid path pair
    for head_entity in changed_symbols:
        urn = head_entity.get("urn", "")
        if not urn:
            continue
        base_entity = base_by_urn.get(urn)
        if base_entity is None:
            continue

        head_props = head_entity.get("properties") or {}
        base_props = base_entity.get("properties") or {}
        head_path = str(head_props.get("path") or "")
        base_path = str(base_props.get("path") or "")
        if not head_path or not base_path:
            continue

        calls_attempted_prefilter += 1
        head_line_start = head_props.get("line")
        head_line_end = head_props.get("end_line")
        base_line_start = base_props.get("line")
        base_line_end = base_props.get("end_line")

        head_body = _read_body(head_root, head_path, head_line_start, head_line_end)
        base_body = _read_body(base_root, base_path, base_line_start, base_line_end)

        if not head_body or not base_body:
            read_failures += 1
            continue
        if head_body == base_body:
            # Identical bodies — skip (cost-bound pre-filter).
            continue

        differing.append((head_entity, base_body, head_body))

    # P1 deeper fix: distinguish "no differing bodies" from "all reads failed".
    # When checkouts are valid dirs but every symbol's source read returned "" (e.g.
    # missing files, permission errors), return failed:unreadable_sources rather than
    # "active" — "active" would falsely imply semantic diff was performed.
    if not differing:
        if calls_attempted_prefilter > 0 and read_failures == calls_attempted_prefilter:
            return [], "failed:unreadable_sources"
        return [], "active"

    # Apply cap AFTER prefilter — only differing symbols count against the slot budget
    ranked_differing = _cluster_rank([d[0] for d in differing], max_symbols)
    differing_by_urn: dict[str, tuple[str, str]] = {
        d[0].get("urn", ""): (d[1], d[2]) for d in differing
    }

    rows: list[JsonObject] = []
    calls_attempted = 0
    calls_failed = 0
    auth_error_seen = False

    for head_entity in ranked_differing:
        urn = head_entity.get("urn", "")
        bodies = differing_by_urn.get(urn)
        if bodies is None:
            continue
        base_body, head_body = bodies

        identity = head_entity.get("identity") or {}
        qualname = str(identity.get("qualname") or urn)
        head_props = head_entity.get("properties") or {}
        head_path = str(head_props.get("path") or "")
        head_line_start = head_props.get("line")
        head_line_end = head_props.get("end_line")

        prompt = _PROMPT_TEMPLATE.format(
            qualname=qualname,
            before=base_body,
            after=head_body,
        )
        calls_attempted += 1
        try:
            result = client.complete_json(prompt)
        except ImportError:
            # litellm not installed — re-raise so _splice catches it as "unavailable"
            raise
        except Exception as exc:  # noqa: BLE001
            if _is_auth_error(exc):
                auth_error_seen = True
            calls_failed += 1
            continue

        # Map typed result to status tracking
        if result.kind == "no_api_key":
            auth_error_seen = True
            calls_failed += 1
            continue
        if result.kind == "llm_error":
            calls_failed += 1
            continue
        # parse_miss: call completed but no valid JSON → not a failure, just no rows
        if result.kind == "parse_miss":
            continue

        parsed = result.value
        if not isinstance(parsed, list):
            continue

        sym_span: JsonObject = {}
        if head_path:
            sym_span = {"path": head_path}
            if head_line_start is not None:
                sym_span["line_start"] = int(head_line_start)
                sym_span["line_end"] = int(head_line_end if head_line_end is not None else head_line_start)
            repo = identity.get("repo")
            if repo:
                sym_span["repo"] = repo

        # P2 fix: validate FIRST, then dedupe by claim text (first valid wins).
        # Deduping before validation lets a malformed item with the same claim string
        # claim the seen_claims slot, silently dropping a later valid item.
        seen_claims: set[str] = set()
        hyps_from_symbol = 0
        for item in parsed:
            if hyps_from_symbol >= _MAX_HYPS_PER_SYMBOL:
                break
            if not _validate_item(item):
                continue
            claim_key = str(item.get("claim", ""))
            if claim_key in seen_claims:
                continue
            seen_claims.add(claim_key)

            # Problem B fix: drop garbage-signal items (4x threshold)
            raw_claim = str(item["claim"])
            raw_consequence = str(item["consequence"])
            raw_negative_check = str(item["negative_check"])
            raw_category = str(item["category"])

            if (
                len(raw_claim) > _DROP_CLAIM
                or len(raw_consequence) > _DROP_CONSEQUENCE
                or len(raw_negative_check) > _DROP_NEGATIVE_CHECK
                or len(raw_category) > _DROP_CATEGORY
            ):
                continue

            # Clamp strings to their limits
            claim = raw_claim[:_CLAMP_CLAIM]
            consequence_text = raw_consequence[:_CLAMP_CONSEQUENCE]
            negative_check = raw_negative_check[:_CLAMP_NEGATIVE_CHECK]
            category = raw_category[:_CLAMP_CATEGORY]

            # Validate and clamp cause_line to symbol span
            raw_line = item.get("cause_line")
            try:
                cause_line = int(raw_line)
            except (TypeError, ValueError):
                cause_line = int(head_line_start) if head_line_start is not None else 1

            # Problem C fix: compute body-derived upper bound when end_line absent.
            # head_body is available in scope (read during prefilter, stored in differing_by_urn).
            if head_line_end is not None:
                derived_end: int | None = int(head_line_end)
            elif head_line_start is not None and head_body:
                body_line_count = head_body.count("\n") + 1
                derived_end = int(head_line_start) + body_line_count - 1
            else:
                derived_end = None

            if head_line_start is not None and derived_end is not None:
                cause_line = max(int(head_line_start), min(cause_line, derived_end))
            elif head_line_start is not None:
                cause_line = max(int(head_line_start), cause_line)

            # Drop cause/consequence coordinate dicts if cause_line is still out of file range.
            # (This is a safety net: body-derived clamping above should prevent this normally.)
            cause_in_range = True
            if head_body:
                file_line_count = head_body.count("\n") + 1
                if head_line_start is not None and cause_line > (int(head_line_start) + file_line_count - 1):
                    cause_in_range = False

            # Build coordinate dicts only when cause_line is within file range.
            cause: JsonObject | None = None
            consequence: JsonObject | None = None
            if cause_in_range:
                cause = {"path": head_path, "line_start": cause_line}
                if sym_span.get("repo"):
                    cause["repo"] = sym_span["repo"]
                consequence = {"path": head_path, "line_start": cause_line}
                if sym_span.get("repo"):
                    consequence["repo"] = sym_span["repo"]

            hyp_id = _hyp_id(claim, head_path, cause_line)
            source_spans = [sym_span] if sym_span else []

            from source.kg.product.review_attribution import hypothesis_label
            label = hypothesis_label("contract_semantic_diff", hyp_id)

            row: JsonObject = {
                "hypothesis_id": hyp_id,
                "label": label,
                "risk_type": "contract_semantic_diff",
                "specificity": "high",
                "confidence": "medium",
                "derivation": "inferred_llm",
                "postable_claim": claim,
                "concrete_invariant": (
                    f"Inferred candidate (requires source verification): {claim}"
                ),
                "why": (
                    f"LLM-inferred behavioral contract change in {qualname} "
                    f"(category: {category}). This is a candidate hypothesis based on "
                    "body-text diff analysis — verify against head source before acting."
                ),
                "consequence_text": consequence_text,
                "negative_checks": [negative_check],
                "source_checks": [
                    f"Verify: {claim}",
                    f"Check head source at {head_path}:{cause_line} — {consequence_text}",
                ],
                "negative_check": negative_check,
                "category": category,
                "source_spans": source_spans,
                "evidence_refs": source_spans,
                "supporting_lead_ids": [],
                "before_refs": [],
                "after_refs": [sym_span] if sym_span else [],
            }
            # Conditionally include coordinate dicts only when in-range.
            if cause is not None:
                row["cause"] = cause
            if consequence is not None:
                row["consequence"] = consequence
            rows.append(row)
            hyps_from_symbol += 1

    # Compute status — partial (rows non-empty + any failure) takes precedence over
    # the failure kind so successful rows are never discarded.  "no_api_key" is
    # reserved for zero usable rows; detail "partial:auth" is surfaced when the
    # partial was caused by an auth-class failure.
    if rows and calls_failed > 0:
        status = "partial:auth" if auth_error_seen else "partial"
    elif auth_error_seen:
        status = "no_api_key"
    elif calls_attempted > 0 and calls_failed == calls_attempted:
        status = "llm_error"
    else:
        status = "active"

    return rows, status
