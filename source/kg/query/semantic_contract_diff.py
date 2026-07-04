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
_BASE_BODY_CHAR_LIMIT = 2500  # per resolved base class body

_PROMPT_FORMAT_INSTRUCTION = "Respond with ONLY a JSON array, no markdown fences, no prose."

_PROMPT_BODY_TEMPLATE = (
    "Compare the before/after implementation of {qualname}.\n\n"
    "BEFORE:\n{before}\n\nAFTER:\n{after}\n\n"
    "State up to 2 behavioral contract changes as falsifiable claims. "
    "For each, describe the OLD contract and the NEW contract in one sentence each, "
    "and name the violated_invariant: the caller-side or persisted-state expectation "
    "this change can break. If the change looks intended/benign and violates no "
    'caller-side or persisted-state expectation, set violated_invariant to exactly "none". '
    'JSON array: [{{"claim": "...", "cause_line": <head line no. int>, '
    '"consequence": "one sentence", "negative_check": "...", "category": "...", '
    '"old_contract": "one sentence", "new_contract": "one sentence", '
    '"violated_invariant": "one sentence or the literal string none"}}]\n\n'
)

# Full template (body + format instruction). The instruction must stay LAST in the
# composed prompt even when a base-class context section is appended — composition
# in semantic_contract_diff inserts base_context between body and instruction.
_PROMPT_TEMPLATE = _PROMPT_BODY_TEMPLATE + _PROMPT_FORMAT_INSTRUCTION

_REQUIRED_KEYS = {
    "claim",
    "cause_line",
    "consequence",
    "negative_check",
    "category",
    "old_contract",
    "new_contract",
    "violated_invariant",
}

# Exact-string protocol sentinel the prompt prescribes for "no violated invariant".
# This is a value WE define, not an inference over the model's free text.
_VIOLATED_INVARIANT_NONE = "none"

# String clamp limits
_CLAMP_CLAIM = 300
_CLAMP_CONSEQUENCE = 300
_CLAMP_NEGATIVE_CHECK = 200
_CLAMP_CATEGORY = 50
_CLAMP_OLD_CONTRACT = 300
_CLAMP_NEW_CONTRACT = 300
_CLAMP_VIOLATED_INVARIANT = 300

# 4x drop thresholds (items exceeding these are garbage-signalled and dropped)
_DROP_CLAIM = 1200
_DROP_CONSEQUENCE = 1200
_DROP_NEGATIVE_CHECK = 800
_DROP_CATEGORY = 200
_DROP_OLD_CONTRACT = 1200
_DROP_NEW_CONTRACT = 1200
_DROP_VIOLATED_INVARIANT = 1200

# Auth error substrings (checked on lowercased exception message)
_AUTH_SUBSTRINGS = ("auth", "unauthorized", "401", "api_key", "apikey")


def _safe_resolve(root: Path, path: str) -> Path | None:
    """Resolve (root / path) and verify it stays under root.

    Returns the resolved Path on success, None on:
      - absolute path (security: Path('/...') escapes root in pathlib)
      - traversal outside root after resolve()
    Callers treat None as "skip this symbol". Skipped unsafe paths are not
    counted separately; they fall into the generic read-failure accounting.
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


def _extract_base_names_from_class_line(class_line: str) -> list[str]:
    """Extract base class names from a 'class X(A, B):' line using str ops.

    Returns the last-segment names (e.g. ['A', 'B']) or [] if not a class line
    or no bases are listed.  Handles simple comma-separated bases only.
    """
    return [last for _full, last in _extract_base_refs_from_class_line(class_line)]


def _extract_base_refs_from_class_line(class_line: str) -> list[tuple[str, str]]:
    """Extract base class references as (full_name, last_segment) pairs.

    Returns pairs like [('module.Base', 'Base'), ('Other', 'Other')] or [] if
    not a class line or no bases are listed.  Handles simple comma-separated
    bases only; strips generic subscripts (e.g. Base[T] → Base).
    """
    line = class_line.strip()
    if not line.startswith("class "):
        return []
    paren_open = line.find("(")
    paren_close = line.rfind(")")
    if paren_open == -1 or paren_close <= paren_open:
        return []
    bases_str = line[paren_open + 1 : paren_close].strip()
    if not bases_str:
        return []
    # Split on top-level commas only (bracket-depth aware, str ops) so generic
    # subscripts like Generic[T, U] don't split into garbage parts.
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in bases_str:
        if ch in "[(":
            depth += 1
            current.append(ch)
        elif ch in "])":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    result: list[tuple[str, str]] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        # Skip keyword arguments (e.g. metaclass=ABCMeta) — not base classes.
        if "=" in part:
            continue
        # Strip a generic subscript (StatefulDetectorHandler[T] → StatefulDetectorHandler)
        # so the name matches entity qualnames.
        bracket = part.find("[")
        if bracket != -1:
            part = part[:bracket].strip()
        if not part:
            continue
        last = part.rsplit(".", 1)[-1]
        if last:
            result.append((part, last))
    return result


def _build_base_class_context(
    head_body: str,
    base_body: str,
    head_snapshot: "KgSnapshot",
    head_root: Path,
    head_entity: "JsonObject",
) -> str:
    """Return a prompt section with up to 2 resolved base class bodies.

    Cheap check: first line of head_body and base_body both start with "class "
    and differ → reparenting candidate.  Resolves bases from the head class line
    against head_snapshot CodeSymbol entities (kind=class, qualname last-segment).
    Reads each resolved base's body via _safe_resolve-guarded _read_body.
    One hop, no recursion.  Returns "" when no enrichment is applicable.
    """
    head_lines = head_body.splitlines()
    base_lines = base_body.splitlines()
    if not head_lines or not base_lines:
        return ""

    head_first = head_lines[0].strip()
    base_first = base_lines[0].strip()

    if not head_first.startswith("class ") or not base_first.startswith("class "):
        return ""
    if head_first == base_first:
        return ""

    base_refs = _extract_base_refs_from_class_line(head_first)
    if not base_refs:
        return ""

    # Build a lookup: last qualname segment → list of CodeSymbol entities
    # Restrict to same repo as the changed symbol.
    identity = head_entity.get("identity") or {}
    head_repo = str(identity.get("repo") or "")
    candidate_entities: dict[str, list[JsonObject]] = {}
    for entity in head_snapshot.entities:
        if entity.get("kind") != "CodeSymbol":
            continue
        eid = entity.get("identity") or {}
        if head_repo and str(eid.get("repo") or "") != head_repo:
            continue
        qname = str(eid.get("qualname") or "")
        if not qname:
            continue
        last_seg = qname.rsplit(".", 1)[-1]
        candidate_entities.setdefault(last_seg, []).append(entity)

    sections: list[str] = []
    resolved_count = 0
    for full_name, last_seg in base_refs:
        if resolved_count >= 2:
            break
        all_matches = candidate_entities.get(last_seg, [])
        if not all_matches:
            continue

        is_dotted = "." in full_name
        if is_dotted:
            # Dotted base (e.g. module.Base): require exact qualified suffix match on qualname.
            # A qualname like "pkg.module.Base" ends with "module.Base" — use str suffix check.
            suffix = full_name
            matches = [
                e for e in all_matches
                if str((e.get("identity") or {}).get("qualname") or "").endswith(suffix)
            ]
            if len(matches) != 1:
                # 0 or 2+ qualified matches → skip enrichment for this base (conservative).
                continue
            base_entity = matches[0]
        else:
            # Bare name: enrich ONLY when exactly one candidate exists in the head snapshot.
            # 2+ candidates → common name collision → skip to avoid wrong class body.
            if len(all_matches) != 1:
                continue
            base_entity = all_matches[0]
        bprops = base_entity.get("properties") or {}
        bpath = str(bprops.get("path") or "")
        if not bpath:
            continue
        bline_start = bprops.get("line")
        bline_end = bprops.get("end_line")
        body = _read_body(head_root, bpath, bline_start, bline_end)
        if not body:
            continue
        body = body[:_BASE_BODY_CHAR_LIMIT]
        bidentity = base_entity.get("identity") or {}
        bqualname = str(bidentity.get("qualname") or full_name)
        sections.append(f"Referenced base class (for context) — {bqualname}:\n{body}")
        resolved_count += 1

    if not sections:
        return ""
    return "\n\n" + "\n\n".join(sections)


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


def _empty_diff_stats(client: "SemanticDiffLlmClient") -> dict:
    """Return a zero-call stats dict (no LLM calls made)."""
    return {
        "model": getattr(client, "model", None),
        "calls_attempted": 0,
        "calls_succeeded": 0,
        "calls_failed": 0,
        "parse_misses": 0,
        "rows_generated": 0,
        "prompt_tokens": None,
        "completion_tokens": None,
        "cost_usd": None,
    }


def semantic_contract_diff(
    base_snapshot: "KgSnapshot",
    head_snapshot: "KgSnapshot",
    base_root: Path,
    head_root: Path,
    changed_symbols: list[JsonObject],
    client: "SemanticDiffLlmClient",
    max_symbols: int = _MAX_SYMBOLS,
    _stats_out: "dict | None" = None,
) -> tuple[list[JsonObject], str]:
    """Generate LLM-backed contract-diff hypothesis rows for changed symbols.

    Returns (rows, status) where status is one of:
      "active"                        — completed normally (zero or more rows)
      "no_api_key"                    — auth/API-key error, zero usable rows
      "llm_error"                     — all LLM calls failed (non-auth)
      "partial"                       — some calls succeeded, some failed
      "partial:auth"                  — partial rows returned but some calls hit auth errors
      "failed:unreadable_sources"     — all source-body reads failed before any LLM call
      "failed:all_responses_unparseable" — calls completed but no response parsed to a usable list
      "failed:no_valid_claims"        — response(s) parsed to a list but every item failed schema validation

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
        if _stats_out is not None:
            _stats_out.update(_empty_diff_stats(client))
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
            if _stats_out is not None:
                _stats_out.update(_empty_diff_stats(client))
            return [], "failed:unreadable_sources"
        if _stats_out is not None:
            _stats_out.update(_empty_diff_stats(client))
        return [], "active"

    # Apply cap AFTER prefilter — only differing symbols count against the slot budget
    ranked_differing = _cluster_rank([d[0] for d in differing], max_symbols)
    differing_by_urn: dict[str, tuple[str, str]] = {
        d[0].get("urn", ""): (d[1], d[2]) for d in differing
    }

    rows: list[JsonObject] = []
    calls_attempted = 0
    calls_failed = 0
    parse_miss_count = 0
    parsed_ok_count = 0  # responses that parsed to a usable list (even if zero valid items)
    valid_item_count = 0  # items that passed _validate_item across all parsed responses
    auth_error_seen = False
    total_prompt_tokens: int | None = None
    total_completion_tokens: int | None = None
    total_cost_usd: float | None = None

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

        base_context = _build_base_class_context(
            head_body=head_body,
            base_body=base_body,
            head_snapshot=head_snapshot,
            head_root=head_root,
            head_entity=head_entity,
        )
        # Compose: body, then optional base-class context, then the format
        # instruction LAST (Fix 3 requires the instruction to end the prompt).
        prompt = _PROMPT_BODY_TEMPLATE.format(
            qualname=qualname,
            before=base_body,
            after=head_body,
        )
        if base_context:
            prompt += base_context.lstrip("\n") + "\n\n"
        prompt += _PROMPT_FORMAT_INSTRUCTION
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

        # Accumulate per-call usage (None stays None until at least one call reports data).
        pt = getattr(result, "prompt_tokens", None)
        ct = getattr(result, "completion_tokens", None)
        cu = getattr(result, "cost_usd", None)
        if pt is not None:
            total_prompt_tokens = (total_prompt_tokens or 0) + pt
        if ct is not None:
            total_completion_tokens = (total_completion_tokens or 0) + ct
        if cu is not None:
            total_cost_usd = (total_cost_usd or 0.0) + cu

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
            parse_miss_count += 1
            continue

        parsed = result.value
        if not isinstance(parsed, list):
            # Parsed to a non-list (unexpected shape) — unusable as claims; count as
            # a parse miss so the honesty statuses below see it.
            parse_miss_count += 1
            continue
        parsed_ok_count += 1

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
            valid_item_count += 1
            claim_key = str(item.get("claim", ""))
            if claim_key in seen_claims:
                continue
            seen_claims.add(claim_key)

            # Problem B fix: drop garbage-signal items (4x threshold)
            raw_claim = str(item["claim"])
            raw_consequence = str(item["consequence"])
            raw_negative_check = str(item["negative_check"])
            raw_category = str(item["category"])
            raw_old_contract = str(item["old_contract"])
            raw_new_contract = str(item["new_contract"])
            raw_violated_invariant = str(item["violated_invariant"])

            if (
                len(raw_claim) > _DROP_CLAIM
                or len(raw_consequence) > _DROP_CONSEQUENCE
                or len(raw_negative_check) > _DROP_NEGATIVE_CHECK
                or len(raw_category) > _DROP_CATEGORY
                or len(raw_old_contract) > _DROP_OLD_CONTRACT
                or len(raw_new_contract) > _DROP_NEW_CONTRACT
                or len(raw_violated_invariant) > _DROP_VIOLATED_INVARIANT
            ):
                continue

            # Clamp strings to their limits
            claim = raw_claim[:_CLAMP_CLAIM]
            consequence_text = raw_consequence[:_CLAMP_CONSEQUENCE]
            negative_check = raw_negative_check[:_CLAMP_NEGATIVE_CHECK]
            category = raw_category[:_CLAMP_CATEGORY]
            old_contract = raw_old_contract[:_CLAMP_OLD_CONTRACT]
            new_contract = raw_new_contract[:_CLAMP_NEW_CONTRACT]
            violated_invariant = raw_violated_invariant[:_CLAMP_VIOLATED_INVARIANT]

            # Classification: a row whose violated_invariant is the prescribed "none"
            # sentinel (exact-string protocol value) or empty is a BEHAVIOR-DELTA row —
            # it merely describes the new behavior with no caller-side/persisted-state
            # expectation broken. Such rows are context-grade (medium specificity); rows
            # with a concrete violated_invariant keep high. The claim-strength scorer
            # (review_hypotheses) then seats them below high-specificity peers.
            has_violated_invariant = bool(
                violated_invariant.strip()
            ) and violated_invariant.strip().lower() != _VIOLATED_INVARIANT_NONE
            specificity = "high" if has_violated_invariant else "medium"

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

            # Compose postable_claim / concrete_invariant by class:
            #   - concrete violated_invariant → surface WHY it's risky (the broken
            #     caller-side/persisted-state expectation) so the reviewer treats it
            #     as a hypothesis, not raw context.
            #   - "none"/empty (BEHAVIOR-DELTA) → phrase as context ("behavior
            #     changed: ..."), NOT a risk assertion.
            if has_violated_invariant:
                postable_claim = f"{claim} — violated invariant: {violated_invariant}"
                concrete_invariant = (
                    f"Inferred candidate (requires source verification): "
                    f"{violated_invariant}"
                )
            else:
                postable_claim = f"behavior changed: {claim}"
                concrete_invariant = (
                    f"Inferred candidate (requires source verification): {claim}"
                )

            # Thread old/new contract into source_checks so the reviewer verifies the
            # old contract in the base checkout and the new contract in the head.
            source_checks = [
                f"Verify: {claim}",
                f"Check head source at {head_path}:{cause_line} — {consequence_text}",
                f"Verify old contract holds in base: {old_contract}",
                f"Verify new contract holds in head: {new_contract}",
            ]

            row: JsonObject = {
                "hypothesis_id": hyp_id,
                "label": label,
                "risk_type": "contract_semantic_diff",
                "specificity": specificity,
                "confidence": "medium",
                "derivation": "inferred_llm",
                "postable_claim": postable_claim,
                "concrete_invariant": concrete_invariant,
                "why": (
                    f"LLM-inferred behavioral contract change in {qualname} "
                    f"(category: {category}). This is a candidate hypothesis based on "
                    "body-text diff analysis — verify against head source before acting."
                ),
                "consequence_text": consequence_text,
                "negative_checks": [negative_check],
                "source_checks": source_checks,
                "negative_check": negative_check,
                "category": category,
                "old_contract": old_contract,
                "new_contract": new_contract,
                "violated_invariant": violated_invariant,
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
    #
    # Parse-miss honesty: when calls were attempted and no response parsed while at
    # least one parse-missed, surface "failed:all_responses_unparseable" rather than
    # "active" (which would imply the mechanism ran cleanly). This holds even when
    # some calls also failed outright — zero parsed responses plus parse-misses is
    # never a clean run. Mixed (some parsed, some missed) stays "active"/"partial"
    # with the detail counts appended so the caller logs them.
    all_parse_missed = (
        calls_attempted > 0
        and parsed_ok_count == 0
        and parse_miss_count > 0
    )
    # Schema-invalid honesty: a response can parse as a JSON list yet have EVERY item fail
    # _validate_item (e.g. the old five-key shape after the schema grew). That yields zero
    # rows but is NOT a clean run, so it must not report "active" (silent nothing). When
    # calls were attempted, >= 1 response parsed, and no item validated overall, surface
    # "failed:no_valid_claims". Gated on rows being empty so any usable row keeps the
    # active/partial path.
    no_valid_claims = (
        calls_attempted > 0
        and parsed_ok_count > 0
        and valid_item_count == 0
    )

    if rows and calls_failed > 0:
        status = "partial:auth" if auth_error_seen else "partial"
    elif all_parse_missed:
        status = "failed:all_responses_unparseable"
    elif auth_error_seen:
        status = "no_api_key"
    elif calls_attempted > 0 and calls_failed == calls_attempted:
        status = "llm_error"
    elif no_valid_claims:
        status = "failed:no_valid_claims"
    else:
        status = "active"

    # Mixed-case quality note: some responses parsed, some parse-missed — append the
    # counts to the status the caller logs. Only "active"/"partial" carry the note so
    # the splice's exact-match handling of "partial:auth"/"no_api_key"/"llm_error"
    # stays intact.
    if parse_miss_count > 0 and parsed_ok_count > 0 and status in ("active", "partial"):
        status = f"{status} ({parse_miss_count} of {calls_attempted} responses unparseable)"

    if _stats_out is not None:
        _stats_out.update({
            "model": getattr(client, "model", None),
            "calls_attempted": calls_attempted,
            "calls_succeeded": parsed_ok_count,
            "calls_failed": calls_failed,
            "parse_misses": parse_miss_count,
            "rows_generated": len(rows),
            "prompt_tokens": total_prompt_tokens,
            "completion_tokens": total_completion_tokens,
            "cost_usd": total_cost_usd,
        })

    return rows, status
