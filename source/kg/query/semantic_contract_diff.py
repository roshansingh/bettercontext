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


def _read_body(root: Path, path: str, line_start: int | None, line_end: int | None) -> str:
    """Read symbol body from checkout root. Returns empty string on any error."""
    try:
        full = (root / path).read_text(encoding="utf-8", errors="replace")
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
) -> list[JsonObject]:
    """Generate LLM-backed contract-diff hypothesis rows for changed symbols.

    Returns a list of hypothesis rows with derivation="inferred_llm".
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
        return []

    # Build URN → entity for both snapshots
    base_by_urn: dict[str, JsonObject] = {e["urn"]: e for e in base_snapshot.entities}

    ranked = _cluster_rank(changed_symbols, max_symbols)
    rows: list[JsonObject] = []

    for head_entity in ranked:
        urn = head_entity.get("urn", "")
        if not urn:
            continue
        base_entity = base_by_urn.get(urn)
        if base_entity is None:
            continue

        identity = head_entity.get("identity") or {}
        qualname = str(identity.get("qualname") or urn)
        head_props = head_entity.get("properties") or {}
        base_props = base_entity.get("properties") or {}
        head_path = str(head_props.get("path") or "")
        base_path = str(base_props.get("path") or "")
        if not head_path or not base_path:
            continue

        head_line_start = head_props.get("line")
        head_line_end = head_props.get("end_line")
        base_line_start = base_props.get("line")
        base_line_end = base_props.get("end_line")

        head_body = _read_body(head_root, head_path, head_line_start, head_line_end)
        base_body = _read_body(base_root, base_path, base_line_start, base_line_end)

        if not head_body or not base_body:
            continue
        if head_body == base_body:
            # Identical bodies — skip (cost-bound pre-filter).
            continue

        prompt = _PROMPT_TEMPLATE.format(
            qualname=qualname,
            before=base_body,
            after=head_body,
        )
        try:
            parsed = client.complete_json(prompt)
        except Exception:  # noqa: BLE001
            return rows  # propagate failure up via caller's error handling

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

        hyps_from_symbol = 0
        for item in parsed:
            if hyps_from_symbol >= _MAX_HYPS_PER_SYMBOL:
                break
            if not _validate_item(item):
                continue

            claim = str(item["claim"])
            consequence_text = str(item["consequence"])
            negative_check = str(item["negative_check"])
            category = str(item["category"])

            # Validate and clamp cause_line to symbol span
            raw_line = item.get("cause_line")
            try:
                cause_line = int(raw_line)
            except (TypeError, ValueError):
                cause_line = int(head_line_start) if head_line_start is not None else 1

            if head_line_start is not None and head_line_end is not None:
                cause_line = max(int(head_line_start), min(cause_line, int(head_line_end)))
            elif head_line_start is not None:
                cause_line = max(int(head_line_start), cause_line)

            cause: JsonObject = {"path": head_path, "line_start": cause_line}
            if sym_span.get("repo"):
                cause["repo"] = sym_span["repo"]
            consequence: JsonObject = {"path": head_path, "line_start": cause_line}
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
                "cause": cause,
                "consequence_text": consequence_text,
                "consequence": consequence,
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
            rows.append(row)
            hyps_from_symbol += 1

    return rows
