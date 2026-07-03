from __future__ import annotations

"""Contract-diff review families over (GraphDelta, base, head) snapshot pairs.

Tripwire resolutions (from Phase-1 final review):
  1. bytes_ref conformance: refs emitted as {repo, commit_sha, path, line_start, line_end};
     commit_sha pulled from snapshot manifest; absent when manifest lacks "commit_sha".
  2. Path normalization: uses graph_diff._norm (literal ./-prefix strip, not char-class lstrip).
     Imported as a module-local copy to keep this module self-contained.
  3. uninstrumented_scopes passthrough: packet builder propagates the list unchanged.
  4. Kind-filter: all three query functions explicitly guard on entity kind == "CodeSymbol"
     for the changed-symbol subject roles; IMPORTS-subject kinds are not treated as guards.

Boundary note:
  This module owns query + packet-builder only. Product-side splice (family
  round-robin interleaving, omission counts, and wiring into review_hypotheses)
  is implemented in mcp_tools._splice_contract_diff_hypotheses.
"""

from typing import Any

from source.kg.core.models import JsonObject
from source.kg.query.graph_diff import GraphDelta
from source.kg.query.snapshot import KgSnapshot


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _norm(p: str) -> str:
    """Normalize a path: backslashes → '/', strip literal './' prefixes.

    Mirrors graph_diff._norm exactly (literal ./ strip, NOT char-class lstrip).
    '../x' is NOT stripped — only leading './'.
    """
    p = p.replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    return p


def _commit_sha(snap: KgSnapshot) -> str | None:
    return snap.manifest.get("commit_sha") or None


def _bytes_ref(snap: KgSnapshot, entity: JsonObject) -> JsonObject:
    """Build a bytes_ref-conformant coordinate dict from entity + snapshot.

    Convention (ADR-0005 Mode A): {repo, commit_sha, path, line_start, line_end}.
    Falls back to entity properties when evidence is absent; omits fields that
    cannot be determined rather than substituting silent zeros.
    """
    commit = _commit_sha(snap)
    identity = entity.get("identity") or {}
    properties = entity.get("properties") or {}

    repo: str | None = identity.get("repo")
    path: str | None = properties.get("path") or None

    if path:
        ref: JsonObject = {}
        if repo:
            ref["repo"] = repo
        if commit:
            ref["commit_sha"] = commit
        ref["path"] = path
        line = properties.get("line")
        end_line = properties.get("end_line")
        if line is not None:
            ref["line_start"] = int(line)
            ref["line_end"] = int(end_line if end_line is not None else line)
        return ref

    # Fallback: first evidence bytes_ref for the entity.
    for ev in snap.evidence_by_target.get(entity.get("entity_id", ""), []):
        br = ev.get("bytes_ref") or {}
        if br.get("path"):
            ref = {}
            if repo:
                ref["repo"] = repo
            # Prefer manifest commit over evidence commit.
            c = commit or br.get("commit_sha")
            if c:
                ref["commit_sha"] = c
            ref["path"] = br["path"]
            if br.get("line_start") is not None:
                ref["line_start"] = int(br["line_start"])
                ref["line_end"] = int(br.get("line_end", br["line_start"]))
            return ref

    return {}


def _symbol_ref(entity: JsonObject, snap: KgSnapshot) -> JsonObject:
    """Compact symbol identity + bytes_ref for packet rows."""
    identity = entity.get("identity") or {}
    return {
        "urn": entity["urn"],
        "entity_id": entity["entity_id"],
        "kind": entity["kind"],
        "repo": identity.get("repo"),
        "module": identity.get("module"),
        "qualname": identity.get("qualname"),
        "bytes_ref": _bytes_ref(snap, entity),
    }


def _head_entity_for_base(
    base_entity: JsonObject,
    head_by_urn: dict[str, JsonObject],
) -> JsonObject | None:
    """Look up the head-side entity matching a base-side entity by URN."""
    return head_by_urn.get(base_entity["urn"])


# ---------------------------------------------------------------------------
# Query 1: guard_call_removed
# ---------------------------------------------------------------------------

def guard_call_removed(
    delta: GraphDelta,
    base: KgSnapshot,
    head: KgSnapshot,
    paths: list[str],
) -> list[JsonObject]:
    """Changed CodeSymbol whose outgoing CALLS edge was removed while the symbol survives.

    A 'changed symbol' is one whose path (entity properties["path"]) normalizes to one of
    *paths* after the literal-prefix normalization. The symbol itself must still exist in
    head (same URN). For every removed CALLS edge from such a symbol to any callee,
    emit a row describing the removed call — the reviewer should check whether the removed
    step was a guard, cleanup, or invariant the remaining code depends on.

    The query is generic: it does not classify what kind of 'guard' was removed. The why
    in the packet row conveys the family's meaning.

    Kind filter: only CodeSymbol subjects are reported (tripwire #4).

    Output rows sorted by (subject URN, removed callee URN):
      subject: symbol_ref of the changed surviving symbol (head-side bytes_ref)
      removed_callee: symbol_ref of the callee that was called in base but not in head
                      (base-side bytes_ref)

    Returns [] when paths is empty, or no matching removed calls exist.
    """
    if not paths:
        return []

    normalized_paths: set[str] = {_norm(p) for p in paths}

    # Head URNs for survival check.
    head_by_urn: dict[str, JsonObject] = {e["urn"]: e for e in head.entities}

    # Build: base entity_id → entity for quick lookup.
    base_by_id: dict[str, JsonObject] = base.entities_by_id

    # Index removed CALLS facts by subject_id.
    removed_calls_by_subject: dict[str, list[JsonObject]] = {}
    for fact in delta.removed_facts:
        if fact.get("predicate") != "CALLS":
            continue
        removed_calls_by_subject.setdefault(fact["subject_id"], []).append(fact)

    if not removed_calls_by_subject:
        return []

    rows: list[JsonObject] = []
    for subject_id, removed_facts in sorted(removed_calls_by_subject.items()):
        base_subject = base_by_id.get(subject_id)
        if base_subject is None:
            continue
        # Kind filter: only CodeSymbol subjects (tripwire #4).
        if base_subject.get("kind") != "CodeSymbol":
            continue
        # Survival filter: subject must exist in head.
        head_subject = head_by_urn.get(base_subject["urn"])
        if head_subject is None:
            continue
        # Path filter: match EITHER side's path so a file moved/renamed between
        # base and head (subject survives by URN, base path differs from the
        # head-side changed path) is not silently skipped.
        base_path = (base_subject.get("properties") or {}).get("path") or ""
        head_path = (head_subject.get("properties") or {}).get("path") or ""
        candidate_paths = {_norm(p) for p in (base_path, head_path) if p}
        if not candidate_paths or not (candidate_paths & normalized_paths):
            continue

        for fact in sorted(removed_facts, key=lambda f: str(f.get("object_id", ""))):
            callee_id = fact.get("object_id", "")
            base_callee = base_by_id.get(callee_id)
            if base_callee is None:
                continue
            rows.append({
                "subject": _symbol_ref(head_subject, head),
                "removed_callee": _symbol_ref(base_callee, base),
            })

    rows.sort(key=lambda r: (
        str(r["subject"].get("urn") or ""),
        str(r["removed_callee"].get("urn") or ""),
    ))
    return rows


# ---------------------------------------------------------------------------
# Query 2: responsibility_moved
# ---------------------------------------------------------------------------

def responsibility_moved(
    delta: GraphDelta,
    base: KgSnapshot,
    head: KgSnapshot,
) -> list[JsonObject]:
    """Callee sets that moved between surviving CodeSymbol subjects within the same repo.

    Matches the pattern: (X→Z) removed AND (Y→Z) added within the same repo, grouped by Z.
    X and Y must both be CodeSymbols (tripwire #4). Both X and Y must be present in their
    respective snapshots. This captures 'service no longer owns query execution' shapes.

    The match is on the callee's URN (Z's identity, not its entity_id) so the pairing is
    commit-independent.

    Output rows sorted by (shared callee URN, moved-from URN, moved-to URN):
      shared_callee: symbol_ref of Z (present in both base and head by URN if possible,
                     else the base-side entity)
      moved_from: symbol_ref of X (base-side)
      moved_to: symbol_ref of Y (head-side)

    Returns [] when no such pattern exists.
    """
    base_by_id: dict[str, JsonObject] = base.entities_by_id
    head_by_id: dict[str, JsonObject] = head.entities_by_id
    head_by_urn: dict[str, JsonObject] = {e["urn"]: e for e in head.entities}
    base_by_urn: dict[str, JsonObject] = {e["urn"]: e for e in base.entities}

    # Map callee URN → set of base-side subject entity_ids for removed CALLS facts.
    removed_by_callee_urn: dict[str, list[str]] = {}
    for fact in delta.removed_facts:
        if fact.get("predicate") != "CALLS":
            continue
        subj = base_by_id.get(fact["subject_id"])
        obj = base_by_id.get(fact["object_id"])
        if subj is None or obj is None:
            continue
        if subj.get("kind") != "CodeSymbol":
            continue
        callee_urn = obj["urn"]
        removed_by_callee_urn.setdefault(callee_urn, []).append(fact["subject_id"])

    # Map callee URN → set of head-side subject entity_ids for added CALLS facts.
    added_by_callee_urn: dict[str, list[str]] = {}
    for fact in delta.added_facts:
        if fact.get("predicate") != "CALLS":
            continue
        subj = head_by_id.get(fact["subject_id"])
        obj = head_by_id.get(fact["object_id"])
        if subj is None or obj is None:
            continue
        if subj.get("kind") != "CodeSymbol":
            continue
        callee_urn = obj["urn"]
        added_by_callee_urn.setdefault(callee_urn, []).append(fact["subject_id"])

    # Find callee URNs with both removed and added subjects.
    shared_callees = sorted(set(removed_by_callee_urn) & set(added_by_callee_urn))
    if not shared_callees:
        return []

    rows: list[JsonObject] = []
    for callee_urn in shared_callees:
        # Resolve the callee entity (prefer head-side, fall back to base-side).
        callee_entity: JsonObject | None = head_by_urn.get(callee_urn) or base_by_urn.get(callee_urn)
        if callee_entity is None:
            continue

        callee_snap = head if callee_urn in head_by_urn else base

        from_ids = sorted(set(removed_by_callee_urn[callee_urn]))
        to_ids = sorted(set(added_by_callee_urn[callee_urn]))

        for from_id in from_ids:
            from_entity = base_by_id.get(from_id)
            if from_entity is None:
                continue
            from_repo = (from_entity.get("identity") or {}).get("repo")

            for to_id in to_ids:
                to_entity = head_by_id.get(to_id)
                if to_entity is None:
                    continue
                to_repo = (to_entity.get("identity") or {}).get("repo")

                # Same-repo constraint: moved responsibility within a single repo.
                if from_repo != to_repo:
                    continue

                # Exclude self-move: X == Y by URN (e.g., same symbol added a new callee
                # while also removing the old one — not a move between distinct symbols).
                if from_entity["urn"] == to_entity["urn"]:
                    continue

                from_survives = from_entity["urn"] in head_by_urn
                rows.append({
                    "shared_callee": _symbol_ref(callee_entity, callee_snap),
                    "moved_from": _symbol_ref(from_entity, base),
                    "moved_to": _symbol_ref(to_entity, head),
                    "from_symbol_survives_in_head": from_survives,
                })

    rows.sort(key=lambda r: (
        str(r["shared_callee"].get("urn") or ""),
        str(r["moved_from"].get("urn") or ""),
        str(r["moved_to"].get("urn") or ""),
    ))
    return rows


# ---------------------------------------------------------------------------
# Packet builder
# ---------------------------------------------------------------------------

def contract_diff_packet(
    base_snapshot_dir: str,
    head_snapshot_dir: str,
    changed_paths: list[str] | None = None,
) -> JsonObject:
    """Build a hypothesis-shaped contract-diff packet for the given snapshot pair.

    Loads both snapshots, computes the GraphDelta, runs all three families, and
    returns a JSON-serialisable dict with hypothesis rows shaped for the product
    layer. Does NOT modify source/kg/product/ — product-side splice is Phase B.

    Hypothesis row shape:
      hypothesis_id: stable string key
      risk_type: one of guard_call_removed_drift | responsibility_moved_drift |
                 test_reference_removed_drift
      concrete_invariant: human-readable statement of what changed
      why: evidence from the delta (not prose)
      source_checks: list of verification questions
      before_refs: list of bytes_ref dicts from base snapshot
      after_refs: list of bytes_ref dicts from head snapshot

    uninstrumented_scopes passthrough: included verbatim (tripwire #3 — it's a
    list of scope dicts, not a count).
    """
    from source.kg.query.graph_diff import (
        diff_snapshots,
        removed_test_references,
    )
    from source.kg.query.snapshot import KgSnapshot
    from hashlib import sha256

    def _hyp_id(risk_type: str, *key_parts: Any) -> str:
        payload = risk_type + "|" + "|".join(str(p) for p in key_parts)
        return f"hypothesis:{risk_type}:{sha256(payload.encode()).hexdigest()[:16]}"

    base = KgSnapshot(base_snapshot_dir)
    head = KgSnapshot(head_snapshot_dir)
    delta = diff_snapshots(base, head)
    head_by_urn: dict[str, JsonObject] = {e["urn"]: e for e in head.entities}

    paths = list(changed_paths or [])

    _GUARD_CAP = 3
    _LARGE_REFACTOR_THRESHOLD = 5

    # Family 1: guard_call_removed (noise-bounded in packet builder; query stays complete)
    guard_rows = guard_call_removed(delta, base, head, paths)

    # Count base-graph referrers per callee URN for ordering.
    base_referrer_count: dict[str, int] = {}
    for fact in base.facts:
        if fact.get("predicate") == "CALLS":
            obj_id = fact.get("object_id", "")
            obj_ent = base.entities_by_id.get(obj_id)
            if obj_ent:
                urn = obj_ent.get("urn", "")
                base_referrer_count[urn] = base_referrer_count.get(urn, 0) + 1

    # Group rows by subject URN.
    from collections import defaultdict as _defaultdict
    rows_by_subject: dict[str, list[JsonObject]] = _defaultdict(list)
    for row in guard_rows:
        rows_by_subject[row["subject"].get("urn", "")].append(row)

    guard_hypotheses: list[JsonObject] = []
    for subj_urn in sorted(rows_by_subject):
        subj_rows = rows_by_subject[subj_urn]
        total = len(subj_rows)
        # Order: most-referenced callee first, then callee URN asc.
        subj_rows.sort(key=lambda r: (
            -base_referrer_count.get(r["removed_callee"].get("urn", ""), 0),
            r["removed_callee"].get("urn", ""),
        ))
        kept = subj_rows[:_GUARD_CAP]
        omitted = total - len(kept)
        large_refactor = total > _LARGE_REFACTOR_THRESHOLD

        for idx, row in enumerate(kept):
            subject = row["subject"]
            callee = row["removed_callee"]
            hyp_id = _hyp_id(
                "guard_call_removed_drift",
                subject.get("urn", ""),
                callee.get("urn", ""),
            )
            why = (
                "A CALLS edge from this symbol to the listed callee was present in base "
                "and absent in head while the subject symbol survives. The removed call may "
                "have been a guard, cleanup, or invariant the remaining code depends on."
            )
            if large_refactor:
                why += f" (part of a larger removal: {total} calls removed from this symbol)"
            hyp: JsonObject = {
                "hypothesis_id": hyp_id,
                "risk_type": "guard_call_removed_drift",
                "concrete_invariant": (
                    f"{subject.get('qualname') or subject.get('urn')} "
                    f"no longer calls {callee.get('qualname') or callee.get('urn')}"
                ),
                "why": why,
                "source_checks": [
                    "Confirm whether the removed call was a precondition check or side-effect.",
                    "Verify the remaining code path still enforces the invariant by another means.",
                ],
                "before_refs": [callee["bytes_ref"]] if callee.get("bytes_ref") else [],
                "after_refs": [subject["bytes_ref"]] if subject.get("bytes_ref") else [],
                "subject": subject,
                "removed_callee": callee,
            }
            if omitted > 0 and idx == len(kept) - 1:
                hyp["omitted_removed_call_count"] = omitted
            guard_hypotheses.append(hyp)

    # Family 2: responsibility_moved
    moved_rows = responsibility_moved(delta, base, head)
    moved_hypotheses: list[JsonObject] = []
    for row in moved_rows:
        from_sym = row["moved_from"]
        to_sym = row["moved_to"]
        callee = row["shared_callee"]
        survives = row["from_symbol_survives_in_head"]
        hyp_id = _hyp_id(
            "responsibility_moved_drift",
            from_sym.get("urn", ""),
            to_sym.get("urn", ""),
            callee.get("urn", ""),
        )
        why = (
            "A CALLS edge (X→Z) was removed and (Y→Z) was added for the same callee Z "
            "within the same repo. The callee's invocation responsibility moved from "
            "one symbol to another — review whether the context, arguments, or invariants "
            "enforced around the call are preserved."
        )
        source_checks = [
            "Confirm whether the caller context (auth, scope, preconditions) is equivalent.",
            "Verify the arguments and error-handling at the new call site match the old.",
        ]
        if not survives:
            why += (
                " The from-symbol no longer exists in head — this may be a rename rather "
                "than an ownership transfer; verify before treating as a dropped responsibility."
            )
            source_checks.append(
                "Check whether the from-symbol was renamed: look for a new symbol with "
                "equivalent behaviour before treating this as a dropped responsibility."
            )
        moved_hypotheses.append({
            "hypothesis_id": hyp_id,
            "risk_type": "responsibility_moved_drift",
            "concrete_invariant": (
                f"Call to {callee.get('qualname') or callee.get('urn')} "
                f"moved from {from_sym.get('qualname') or from_sym.get('urn')} "
                f"to {to_sym.get('qualname') or to_sym.get('urn')}"
            ),
            "why": why,
            "source_checks": source_checks,
            "before_refs": [from_sym["bytes_ref"]] if from_sym.get("bytes_ref") else [],
            "after_refs": [to_sym["bytes_ref"]] if to_sym.get("bytes_ref") else [],
            "moved_from": from_sym,
            "moved_to": to_sym,
            "shared_callee": callee,
            "from_symbol_survives_in_head": survives,
        })

    # Family 3: test_reference_removed (reuses Phase-1 query unchanged)
    test_rows = removed_test_references(delta, base, head)
    test_hypotheses: list[JsonObject] = []
    for row in test_rows:
        subj = row["subject"]
        obj = row["object"]
        hyp_id = _hyp_id(
            "test_reference_removed_drift",
            subj.get("urn", ""),
            obj.get("urn", ""),
        )
        # Build bytes_refs from the row's subject path + base entity evidence.
        base_subj_entity = base.entities_by_id.get(subj.get("entity_id", ""))
        before_ref = _bytes_ref(base, base_subj_entity) if base_subj_entity else {}
        # Object survives in head; reuse already-built head_by_urn.
        head_obj = head_by_urn.get(obj.get("urn", ""))
        after_ref = _bytes_ref(head, head_obj) if head_obj else {}

        test_hypotheses.append({
            "hypothesis_id": hyp_id,
            "risk_type": "test_reference_removed_drift",
            "concrete_invariant": (
                f"Test {subj.get('qualname') or subj.get('path')} "
                f"no longer references {obj.get('qualname') or obj.get('urn')} "
                f"via {row.get('predicate')}"
            ),
            "why": (
                f"A {row.get('predicate')} fact from test-classified path "
                f"{subj.get('path')!r} to a surviving symbol was removed. "
                "The test may no longer cover a runtime invariant."
            ),
            "source_checks": [
                "Confirm whether the removed test call covered an edge case or contract.",
                "Verify the surviving symbol still has adequate test coverage after the change.",
            ],
            "before_refs": [before_ref] if before_ref else [],
            "after_refs": [after_ref] if after_ref else [],
            "test_subject": subj,
            "surviving_object": obj,
        })

    all_hypotheses = guard_hypotheses + moved_hypotheses + test_hypotheses

    return {
        "contract_diff_packet": {
            "base_snapshot": base_snapshot_dir,
            "head_snapshot": head_snapshot_dir,
            "changed_paths": paths,
            "delta_summary": delta.summary(),
            "uninstrumented_scopes": delta.uninstrumented_scopes,
            "hypothesis_count": len(all_hypotheses),
            "hypotheses": all_hypotheses,
            "families": {
                "guard_call_removed_drift": len(guard_hypotheses),
                "responsibility_moved_drift": len(moved_hypotheses),
                "test_reference_removed_drift": len(test_hypotheses),
            },
        }
    }
