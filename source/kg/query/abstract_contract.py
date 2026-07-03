from __future__ import annotations

"""Deterministic abstract-base reparenting detector (contract-diff family).

Detects the failure family where a class is reparented onto a new base that
declares ``abc.abstractmethod`` members which the subclass does not implement.
A registry / factory that instantiates the subclass then raises ``TypeError``
at construction — before any method runs.

Mechanism (pure ``ast`` + KG structure; NO regex, NO name/keyword lists):
  1. For each changed CLASS symbol (symbol_kind == "class") that exists in both
     base and head checkouts, parse the containing file with ``ast`` and extract
     the class def's base names (``ast.Name`` / dotted ``ast.Attribute`` /
     ``ast.Subscript`` generics with the subscript stripped).
  2. Proceed only when the base-name list CHANGED between base and head — that is
     the reparenting diff trigger.
  3. Resolve each NEW base name against head-snapshot CodeSymbol class entities
     using the same fail-closed disambiguation as semantic_contract_diff
     (dotted → exact qualified-suffix match; bare → exactly one candidate;
     ambiguous → skip).
  4. Parse each resolved base's own file with ``ast``. The base is abstract when
     its own bases include ``ABC``/``abc.ABC``, or it declares
     ``metaclass=ABCMeta``, or any member is decorated with
     ``abstractmethod``/``abc.abstractmethod`` (including stacked
     ``@property`` + ``@abstractmethod``). Collect the required abstract member
     names (functions + properties).
  5. ``unimplemented`` = abstract members with no same-name def/assignment in the
     changed subclass body. Emit a row ONLY when ``unimplemented`` is non-empty.
     An empty subclass body (only ``pass``/``...``/docstring) is a strengthening
     signal included in the claim.
  6. Best-effort instantiation evidence: scan head-snapshot CALLS facts whose
     callee is the subclass symbol; if found, the first referencing coordinate is
     included in ``consequence``. Absence does not suppress the row — the ABC
     semantics alone justify it.

Limitation: only the DIRECT resolved base is inspected. Transitive abstract
chains (abstract member declared two or more hops up the MRO) are out of scope.

ADR-0004: rows are deterministic_static candidate hypotheses spliced alongside
the other deterministic contract-diff families; never persisted as canonical.
"""

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from source.kg.core.models import JsonObject
from source.kg.query.semantic_contract_diff import _read_body, _safe_resolve

if TYPE_CHECKING:
    from source.kg.query.snapshot import KgSnapshot


_RISK_TYPE = "abstract_contract_unimplemented"


@dataclass(frozen=True)
class _AbstractBase:
    """Resolved abstract base class and its required (abstract) member names."""

    qualname: str
    path: str
    line: int | None
    required_members: tuple[str, ...]


@dataclass
class _DetectStats:
    """Internal counters (not part of any public contract)."""

    non_class_skipped: int = 0
    non_python_skipped: int = 0
    parse_failures: int = 0
    reparent_triggers: int = 0
    rows_emitted: int = 0


def _class_def_in_source(source: str, class_name: str) -> ast.ClassDef | None:
    """Parse *source* and return the top-level (or nested) ClassDef named *class_name*.

    Matches on the last qualname segment so ``pkg.mod.Widget`` finds ``Widget``.
    Returns None when the source does not parse or the class is absent.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    target = class_name.rsplit(".", 1)[-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == target:
            return node
    return None


def _base_name(node: ast.expr) -> str | None:
    """Return the source-level base name for a class-base expression.

    Handles ``ast.Name`` (``Base``), dotted ``ast.Attribute`` (``mod.Base``), and
    ``ast.Subscript`` generics (``Base[T]`` → the base expression with the
    subscript stripped). Keyword args (``metaclass=...``) never reach here — they
    are ``keywords`` on the ClassDef, not ``bases``. Returns None for shapes that
    are not plain name references (e.g. a call expression).
    """
    if isinstance(node, ast.Subscript):
        return _base_name(node.value)
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _base_name(node.value)
        if prefix is None:
            return None
        return f"{prefix}.{node.attr}"
    return None


def _base_names(class_def: ast.ClassDef) -> list[str]:
    """Ordered base names of a ClassDef (positional bases only)."""
    names: list[str] = []
    for base in class_def.bases:
        name = _base_name(base)
        if name is not None:
            names.append(name)
    return names


def _has_abstractmethod_decorator(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True when any decorator is ``abstractmethod`` or ``abc.abstractmethod``.

    Covers stacked decorators (``@property`` + ``@abstractmethod``) since every
    decorator on the node is examined.
    """
    for dec in node.decorator_list:
        name = _base_name(dec)
        if name is None:
            continue
        if name.rsplit(".", 1)[-1] == "abstractmethod":
            return True
    return False


def _base_is_abstract_and_members(class_def: ast.ClassDef) -> tuple[bool, tuple[str, ...]]:
    """Return (is_abstract, required_member_names) for a base ClassDef.

    is_abstract when the class's own bases include ``ABC``/``abc.ABC``, OR a
    ``metaclass=ABCMeta`` keyword is present, OR any member carries an
    ``abstractmethod`` decorator. required_member_names are the names of members
    (functions + properties) decorated ``abstractmethod``.
    """
    abstract_members: list[str] = []
    for stmt in class_def.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and _has_abstractmethod_decorator(stmt):
            abstract_members.append(stmt.name)

    base_marks_abc = any(
        (_base_name(b) or "").rsplit(".", 1)[-1] == "ABC" for b in class_def.bases
    )
    metaclass_is_abcmeta = any(
        kw.arg == "metaclass" and (_base_name(kw.value) or "").rsplit(".", 1)[-1] == "ABCMeta"
        for kw in class_def.keywords
    )

    is_abstract = bool(abstract_members) or base_marks_abc or metaclass_is_abcmeta
    return is_abstract, tuple(dict.fromkeys(abstract_members))


def _defined_member_names(class_def: ast.ClassDef) -> set[str]:
    """Names defined directly in a subclass body: functions, methods, and assignments.

    A member counts as "implemented" when the subclass binds the same name via a
    def, async def, or an assignment target (covers ``counter_names = (...)`` style
    property overrides).
    """
    names: set[str] = set()
    for stmt in class_def.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(stmt.name)
        elif isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            names.add(stmt.target.id)
    return names


def _body_is_empty(class_def: ast.ClassDef) -> bool:
    """True when the class body is only ``pass``/``...``/a docstring (no real members)."""
    for stmt in class_def.body:
        if isinstance(stmt, ast.Pass):
            continue
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            # Docstring or bare ``...`` (Ellipsis constant).
            continue
        return False
    return True


def _resolve_base_entity(
    base_name: str,
    head_snapshot: "KgSnapshot",
    subclass_repo: str,
) -> JsonObject | None:
    """Resolve a base name to a single head-snapshot class entity (fail-closed).

    Mirrors semantic_contract_diff._build_base_class_context disambiguation:
      - restrict to CodeSymbol class entities in the same repo as the subclass
      - dotted name → exact qualified-suffix match on qualname; require exactly 1
      - bare name → require exactly 1 candidate in the repo
    Returns None (skip) on 0 or 2+ matches.
    """
    last_seg = base_name.rsplit(".", 1)[-1]
    candidates: list[JsonObject] = []
    for entity in head_snapshot.entities:
        if entity.get("kind") != "CodeSymbol":
            continue
        identity = entity.get("identity") or {}
        if str(identity.get("symbol_kind") or "") != "class":
            continue
        if subclass_repo and str(identity.get("repo") or "") != subclass_repo:
            continue
        qname = str(identity.get("qualname") or "")
        if not qname:
            continue
        if qname.rsplit(".", 1)[-1] == last_seg:
            candidates.append(entity)

    if not candidates:
        return None

    if "." in base_name:
        matches = [
            e
            for e in candidates
            if str((e.get("identity") or {}).get("qualname") or "").endswith(base_name)
        ]
        if len(matches) != 1:
            return None
        return matches[0]

    if len(candidates) != 1:
        return None
    return candidates[0]


def _abstract_base_from_entity(
    base_entity: JsonObject,
    head_root: Path,
) -> _AbstractBase | None:
    """Parse a resolved base entity's file and return its abstract profile, or None."""
    identity = base_entity.get("identity") or {}
    props = base_entity.get("properties") or {}
    bpath = str(props.get("path") or "")
    if not bpath:
        return None
    qualname = str(identity.get("qualname") or "")
    source = _read_full_source(head_root, bpath)
    if source is None:
        return None
    class_def = _class_def_in_source(source, qualname)
    if class_def is None:
        return None
    is_abstract, required = _base_is_abstract_and_members(class_def)
    if not is_abstract or not required:
        return None
    line = props.get("line")
    return _AbstractBase(
        qualname=qualname,
        path=bpath,
        line=int(line) if line is not None else None,
        required_members=required,
    )


def _read_full_source(root: Path, path: str) -> str | None:
    """Read the entire file (safe-resolved). None on unsafe path or read error."""
    safe = _safe_resolve(root, path)
    if safe is None:
        return None
    try:
        return safe.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _instantiation_coordinate(
    subclass_entity: JsonObject,
    head_snapshot: "KgSnapshot",
) -> JsonObject | None:
    """Best-effort: first CALLS coordinate whose callee is the subclass symbol.

    Instantiating ``Subclass(...)`` is recorded as a CALLS edge to the class
    symbol in this KG. Returns a {repo, path, line_start} coordinate from the
    first such edge's evidence, or None when no instantiation edge exists.
    """
    subclass_id = subclass_entity.get("entity_id")
    if not subclass_id:
        return None
    for fact in head_snapshot.facts:
        if fact.get("predicate") != "CALLS" or fact.get("object_id") != subclass_id:
            continue
        caller = head_snapshot.entities_by_id.get(fact.get("subject_id", ""))
        for ev in head_snapshot.evidence_by_target.get(fact.get("fact_id", ""), []):
            br = ev.get("bytes_ref") or {}
            path = br.get("path")
            if not path:
                continue
            coord: JsonObject = {"path": path}
            if br.get("line_start") is not None:
                coord["line_start"] = int(br["line_start"])
            if caller is not None:
                repo = (caller.get("identity") or {}).get("repo")
                if repo:
                    coord["repo"] = repo
            return coord
    return None


def _cause_coordinate(subclass_entity: JsonObject) -> JsonObject:
    """Subclass class-def coordinate for the ``cause`` field."""
    identity = subclass_entity.get("identity") or {}
    props = subclass_entity.get("properties") or {}
    coord: JsonObject = {}
    repo = identity.get("repo")
    path = props.get("path")
    if path:
        coord["path"] = str(path)
    line = props.get("line")
    if line is not None:
        coord["line_start"] = int(line)
    if repo:
        coord["repo"] = str(repo)
    return coord


def abstract_contract_diff(
    base_snapshot: "KgSnapshot",
    head_snapshot: "KgSnapshot",
    base_root: Path,
    head_root: Path,
    changed_symbols: list[JsonObject],
) -> tuple[list[JsonObject], _DetectStats]:
    """Detect abstract-base reparenting with unimplemented abstract members.

    Args:
        base_snapshot: KG snapshot for the base commit (unused for resolution but
            kept for signature symmetry with semantic_contract_diff and future use).
        head_snapshot: KG snapshot for the head commit (base-class resolution).
        base_root: base checkout dir (read the pre-change class def).
        head_root: head checkout dir (read the reparented class def + base bodies).
        changed_symbols: head-snapshot CodeSymbol entities that changed in the PR.

    Returns (rows, stats). Each row is a deterministic_static hypothesis row shaped
    to mirror the other contract-diff families (splice-ready).
    """
    del base_snapshot  # signature symmetry; head_snapshot carries the resolution index
    rows: list[JsonObject] = []
    stats = _DetectStats()

    for entity in changed_symbols:
        if entity.get("kind") != "CodeSymbol":
            continue
        identity = entity.get("identity") or {}
        if str(identity.get("symbol_kind") or "") != "class":
            stats.non_class_skipped += 1
            continue

        props = entity.get("properties") or {}
        head_path = str(props.get("path") or "")
        if not head_path or not head_path.endswith(".py"):
            stats.non_python_skipped += 1
            continue

        qualname = str(identity.get("qualname") or "")
        if not qualname:
            continue

        head_source = _read_full_source(head_root, head_path)
        base_source = _read_full_source(base_root, head_path)
        if head_source is None or base_source is None:
            continue

        head_class = _class_def_in_source(head_source, qualname)
        base_class = _class_def_in_source(base_source, qualname)
        if head_class is None or base_class is None:
            # Class absent on one side (new/deleted) → not a reparent of an
            # existing class; parse failure also lands here.
            stats.parse_failures += 1
            continue

        head_bases = _base_names(head_class)
        base_bases = _base_names(base_class)
        if head_bases == base_bases:
            # Base list unchanged → not a reparenting diff.
            continue
        stats.reparent_triggers += 1

        new_bases = [b for b in head_bases if b not in set(base_bases)]
        if not new_bases:
            continue

        subclass_repo = str(identity.get("repo") or "")
        defined = _defined_member_names(head_class)
        empty_body = _body_is_empty(head_class)

        for base_name in new_bases:
            base_entity = _resolve_base_entity(base_name, head_snapshot, subclass_repo)
            if base_entity is None:
                continue
            abstract_base = _abstract_base_from_entity(base_entity, head_root)
            if abstract_base is None:
                continue

            unimplemented = tuple(m for m in abstract_base.required_members if m not in defined)
            if not unimplemented:
                continue

            row = _build_row(
                subclass_entity=entity,
                subclass_qualname=qualname,
                abstract_base=abstract_base,
                unimplemented=unimplemented,
                empty_body=empty_body,
                head_snapshot=head_snapshot,
            )
            rows.append(row)
            stats.rows_emitted += 1

    rows.sort(key=lambda r: str(r.get("hypothesis_id") or ""))
    return rows, stats


def _build_row(
    *,
    subclass_entity: JsonObject,
    subclass_qualname: str,
    abstract_base: _AbstractBase,
    unimplemented: tuple[str, ...],
    empty_body: bool,
    head_snapshot: "KgSnapshot",
) -> JsonObject:
    """Assemble a deterministic_static hypothesis row (splice-ready)."""
    from hashlib import sha256

    cause = _cause_coordinate(subclass_entity)
    members_str = ", ".join(unimplemented)

    payload = f"{_RISK_TYPE}|{subclass_qualname}|{abstract_base.qualname}|{members_str}"
    hypothesis_id = f"hypothesis:{_RISK_TYPE}:{sha256(payload.encode()).hexdigest()[:16]}"

    postable_claim = (
        f"{subclass_qualname} was reparented onto abstract base {abstract_base.qualname} "
        f"but does not implement its abstract member(s): {members_str}. "
        "Instantiating the subclass raises TypeError at construction, before any method runs."
    )
    if empty_body:
        postable_claim += " The subclass body is empty (pass/…), so no members are provided."

    instantiation = _instantiation_coordinate(subclass_entity, head_snapshot)
    consequence: JsonObject = {
        "text": (
            f"Any call site that instantiates {subclass_qualname.rsplit('.', 1)[-1]}(...) "
            "raises TypeError: Can't instantiate abstract class with abstract methods "
            f"{members_str}."
        ),
    }
    if instantiation is not None:
        consequence["instantiation_site"] = instantiation

    concrete_invariant = (
        f"{subclass_qualname} must implement every abstract member of {abstract_base.qualname} "
        f"({', '.join(abstract_base.required_members)}) to be instantiable."
    )

    source_checks = [
        f"Inspect {abstract_base.qualname} at {abstract_base.path}"
        + (f":{abstract_base.line}" if abstract_base.line is not None else "")
        + f" — confirm it declares abstractmethod members: {', '.join(abstract_base.required_members)}.",
        f"Inspect the {subclass_qualname} body — confirm it defines none of: {members_str}.",
        f"Inspect instantiation sites of {subclass_qualname.rsplit('.', 1)[-1]}"
        + (
            f" (e.g. {instantiation.get('path')}:{instantiation.get('line_start')})"
            if instantiation is not None
            else " (registries/factories) — the TypeError fires there at construction"
        )
        + ".",
    ]
    negative_checks = [
        f"If {subclass_qualname} (or a mixin in its MRO) implements all of {members_str}, "
        "the class is instantiable and this risk does not apply.",
        f"If {subclass_qualname.rsplit('.', 1)[-1]} is never instantiated anywhere "
        "(only used as a further base or type annotation), no TypeError is raised — do not post.",
    ]

    row: JsonObject = {
        "hypothesis_id": hypothesis_id,
        "risk_type": _RISK_TYPE,
        "specificity": "high",
        "confidence": "high",
        "derivation": "deterministic_static",
        "postable_claim": postable_claim,
        "concrete_invariant": concrete_invariant,
        "why": postable_claim,
        "cause": cause,
        "consequence": consequence,
        "source_checks": source_checks,
        "negative_checks": negative_checks,
        "unimplemented_members": list(unimplemented),
        "abstract_base": abstract_base.qualname,
        "subclass": subclass_qualname,
    }
    return row
