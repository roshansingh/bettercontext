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
  4. Parse each resolved base's own file with ``ast``. Abc markers are resolved
     BINDING-AWARE against that file's imports (``_abc_bindings``): ``ABC``/
     ``ABCMeta``/``abstractmethod`` count only when they actually bind to the ``abc``
     module (``import abc`` + ``abc.X``, ``import abc as a`` + ``a.X``, or
     ``from abc import X [as y]``). A locally-defined ``class ABC`` / ``def
     abstractmethod`` shadows the marker and is treated as non-abc (fail closed). The
     base is abstract when its own bases include the abc ``ABC``, or it declares
     ``metaclass=`` an abc ``ABCMeta``, or any member is decorated with the abc
     ``abstractmethod`` (including stacked ``@property`` + ``@abstractmethod``).
     Collect the required abstract member names (functions + properties).
  5. ``unimplemented`` = abstract members with no same-name def/assignment in the
     changed subclass body AND not concretely supplied by a PRECEDING base in the MRO.
     Only head bases listed BEFORE the abstract base can satisfy its contract:
     ``getattr`` walks the MRO left-to-right, so for ``class W(Base, Mixin)`` the
     abstract member on ``Base`` resolves to ``Base`` — a later ``Mixin`` does NOT
     satisfy it. Each preceding base (mixin or concrete parent) is resolved + parsed and
     its concrete members are subtracted THROUGH its own MRO — ``class FullMixin(Impl):
     pass`` contributes ``Impl``'s implementations too (depth-capped, cycle-guarded). If
     any base or ancestor in that chain cannot be resolved or parsed AND the resolvable
     part does not already cover every required member, the row is SUPPRESSED — an unseen
     ancestor could satisfy the remainder, so a high-confidence claim must not survive
     that uncertainty. Bases listed AFTER the abstract base are irrelevant to it and never
     suppress the row. Reparenting with no preceding base is never suppressed.
     Emit a row ONLY when ``unimplemented`` is non-empty. An empty subclass body (only
     ``pass``/``...``/docstring) is a strengthening signal included in the claim.
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

# The three ``abc`` names that carry abstract semantics for this detector.
_ABC_MARKERS = ("ABC", "ABCMeta", "abstractmethod")


@dataclass(frozen=True)
class _AbcBindings:
    """Binding-aware ``abc`` marker resolution for one parsed module.

    Leaf-name matching (``rsplit(".", 1)[-1] == "ABC"``) misreads a locally-defined
    ``class ABC`` or ``def abstractmethod`` as real ``abc`` semantics. This resolves a
    class-base / decorator name to its ``abc`` marker leaf ONLY when it actually binds
    to the ``abc`` module:

      * ``import abc`` (+ ``as`` alias)      → the alias is an abc-module name; ``<alias>.X``
        resolves to marker ``X`` when ``X`` is an abc marker.
      * ``from abc import ABC/ABCMeta/abstractmethod`` (+ ``as`` alias) → the bound bare
        name resolves to its abc marker.

    A name that is (re)bound by a module-level ``def``/``class``/assignment shadows any
    import and is treated as NOT an abc marker (fail closed).
    """

    #: Module-level names bound to the ``abc`` module (``import abc [as x]``), minus
    #: any shadowed by a local def/class/assignment.
    module_aliases: frozenset[str]
    #: Bare name → abc marker leaf (``from abc import ABC as A`` → ``{"A": "ABC"}``),
    #: minus any shadowed locally.
    from_imports: dict[str, str]

    def marker(self, name: str | None) -> str | None:
        """Return the abc marker leaf (ABC/ABCMeta/abstractmethod) *name* binds to, or None.

        *name* is a source-level base/decorator name from ``_base_name`` (``ast.Name`` →
        bare, ``ast.Attribute`` → dotted). Fails closed on anything not provably ``abc``.
        """
        if name is None:
            return None
        if "." in name:
            prefix, leaf = name.rsplit(".", 1)
            if prefix in self.module_aliases and leaf in _ABC_MARKERS:
                return leaf
            return None
        return self.from_imports.get(name)


def _local_module_bindings(tree: ast.Module) -> set[str]:
    """Module-level names (re)bound by a ``def``/``class``/assignment.

    Such a binding shadows a same-named ``abc`` import, so the name must not be read as
    an abc marker. Only top-level statements are considered — nested scopes cannot rebind
    a module-level import for a class defined at module scope.
    """
    names: set[str] = set()
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(stmt.name)
        elif isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            names.add(stmt.target.id)
    return names


def _abc_bindings(tree: ast.Module) -> _AbcBindings:
    """Collect ``abc`` marker bindings for one module (call once per parsed file)."""
    shadowed = _local_module_bindings(tree)
    module_aliases: set[str] = set()
    from_imports: dict[str, str] = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                if alias.name == "abc":
                    module_aliases.add(alias.asname or alias.name)
        elif isinstance(stmt, ast.ImportFrom) and stmt.module == "abc" and stmt.level == 0:
            for alias in stmt.names:
                if alias.name in _ABC_MARKERS:
                    from_imports[alias.asname or alias.name] = alias.name
    module_aliases -= shadowed
    for bound in list(from_imports):
        if bound in shadowed:
            del from_imports[bound]
    return _AbcBindings(
        module_aliases=frozenset(module_aliases),
        from_imports=from_imports,
    )


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
    mro_suppressed: int = 0
    rows_emitted: int = 0


def _class_defs_by_leaf(tree: ast.Module, leaf: str) -> list[ast.ClassDef]:
    """All ClassDef nodes in *tree* whose own name equals *leaf* (any nesting depth)."""
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == leaf
    ]


def _class_def_matches_nesting(tree: ast.Module, segments: list[str]) -> ast.ClassDef | None:
    """Walk *tree* following *segments* (outer→inner class names), returning the leaf ClassDef.

    Only ClassDef bodies are descended, so ``segments == ["Outer", "Widget"]`` matches
    ``class Outer: class Widget: ...`` and not a top-level ``Widget``. Returns None when
    the full path is not present.
    """
    body: list[ast.stmt] = list(tree.body)
    node: ast.ClassDef | None = None
    for seg in segments:
        node = next(
            (s for s in body if isinstance(s, ast.ClassDef) and s.name == seg),
            None,
        )
        if node is None:
            return None
        body = list(node.body)
    return node


def _class_def_in_source(
    source: str,
    class_name: str,
    *,
    line: int | None = None,
) -> ast.ClassDef | None:
    """Parse *source* and return the ClassDef identified by *class_name* (fail-closed).

    *class_name* is a qualname whose last dotted segment is the class's own name; the
    remaining segments (if any) form its nesting path. Disambiguation, in order:
      1. Single leaf-name match → return it.
      2. Multiple matches + *line* anchor → the candidate whose ``lineno`` matches, else
         the nearest ClassDef starting at or before *line* (innermost containing def).
      3. Multiple matches + no usable line anchor → full nesting-path match against the
         qualname segments (e.g. ``Outer.Widget``).
      4. Still ambiguous (2+ candidates, no anchor resolves) → None (fail closed).
    Returns None when the source does not parse or the class is absent.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    segments = [s for s in class_name.split(".") if s]
    leaf = segments[-1] if segments else class_name
    candidates = _class_defs_by_leaf(tree, leaf)
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    # Ambiguous leaf name: prefer the KG line anchor.
    if line is not None:
        exact = [c for c in candidates if c.lineno == line]
        if len(exact) == 1:
            return exact[0]
        containing = [c for c in candidates if c.lineno <= line]
        if containing:
            nearest = max(containing, key=lambda c: c.lineno)
            if sum(1 for c in containing if c.lineno == nearest.lineno) == 1:
                return nearest

    # No usable line anchor: try the full nesting path from the qualname.
    if len(segments) > 1:
        matched = _class_def_matches_nesting(tree, segments)
        if matched is not None:
            return matched

    # 2+ candidates and nothing disambiguates → fail closed.
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


def _has_abstractmethod_decorator(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    bindings: _AbcBindings,
) -> bool:
    """True when any decorator binds to ``abc.abstractmethod`` in the enclosing module.

    Binding-aware: a locally-defined ``abstractmethod`` decorator does NOT count. Covers
    stacked decorators (``@property`` + ``@abstractmethod``) since every decorator on the
    node is examined.
    """
    for dec in node.decorator_list:
        if bindings.marker(_base_name(dec)) == "abstractmethod":
            return True
    return False


def _base_is_abstract_and_members(
    class_def: ast.ClassDef, bindings: _AbcBindings
) -> tuple[bool, tuple[str, ...]]:
    """Return (is_abstract, required_member_names) for a base ClassDef.

    A base blocks instantiation only when BOTH hold: (a) it declares one or more
    ``abstractmethod``-decorated members, AND (b) it is governed by ``ABCMeta`` —
    i.e. its own bases include ``ABC``/``abc.ABC`` or a ``metaclass=ABCMeta``
    keyword is present. This mirrors Python semantics: ``@abstractmethod`` is inert
    under the default ``type`` metaclass; a plain class with an abstractmethod-
    decorated member is still instantiable. Conversely an ABC with zero abstract
    members is instantiable too, so the abstract-member requirement is also load-
    bearing (the caller further guards on a non-empty member set).

    Known conservative limitation: a base that inherits ``ABCMeta`` transitively
    (its own parent is ``ABC``) but carries no LOCAL ``ABC``/``metaclass`` marker is
    reported non-abstract here — a false negative in the safe direction, since only
    the DIRECT base def is inspected.

    required_member_names are the names of members (functions + properties)
    decorated ``abstractmethod``.
    """
    abstract_members: list[str] = []
    for stmt in class_def.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and _has_abstractmethod_decorator(stmt, bindings):
            abstract_members.append(stmt.name)

    base_marks_abc = any(bindings.marker(_base_name(b)) == "ABC" for b in class_def.bases)
    metaclass_is_abcmeta = any(
        kw.arg == "metaclass" and bindings.marker(_base_name(kw.value)) == "ABCMeta"
        for kw in class_def.keywords
    )

    is_abstract = bool(abstract_members) and (base_marks_abc or metaclass_is_abcmeta)
    return is_abstract, tuple(dict.fromkeys(abstract_members))


def _concrete_member_names(class_def: ast.ClassDef, bindings: _AbcBindings) -> set[str]:
    """Names CONCRETELY defined in a base body (def/assign NOT decorated abstractmethod).

    A concrete def or an assignment satisfies an abstract member of the same name via
    the MRO. abstractmethod-decorated defs do NOT count — they re-declare, not satisfy.
    Binding-aware: only a decorator that binds to ``abc.abstractmethod`` disqualifies a
    def.
    """
    names: set[str] = set()
    for stmt in class_def.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not _has_abstractmethod_decorator(stmt, bindings):
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


def _parse_base_entity(
    base_entity: JsonObject,
    head_root: Path,
) -> tuple[ast.Module, ast.ClassDef, str, str, int | None] | None:
    """Parse a resolved base entity's file once.

    Returns (module_tree, class_def, path, qualname, line) or None when the path is
    missing/unsafe, the file is unreadable, or the class def cannot be located. The tree
    is returned so the caller can build binding-aware ``abc`` marker context for that file.
    """
    identity = base_entity.get("identity") or {}
    props = base_entity.get("properties") or {}
    bpath = str(props.get("path") or "")
    if not bpath:
        return None
    qualname = str(identity.get("qualname") or "")
    source = _read_full_source(head_root, bpath)
    if source is None:
        return None
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    raw_line = props.get("line")
    line = int(raw_line) if raw_line is not None else None
    class_def = _class_def_in_source(source, qualname, line=line)
    if class_def is None:
        return None
    return tree, class_def, bpath, qualname, line


def _abstract_base_from_entity(
    base_entity: JsonObject,
    head_root: Path,
) -> _AbstractBase | None:
    """Parse a resolved base entity's file and return its abstract profile, or None."""
    parsed = _parse_base_entity(base_entity, head_root)
    if parsed is None:
        return None
    tree, class_def, bpath, qualname, line = parsed
    is_abstract, required = _base_is_abstract_and_members(class_def, _abc_bindings(tree))
    if not is_abstract or not required:
        return None
    return _AbstractBase(
        qualname=qualname,
        path=bpath,
        line=line,
        required_members=required,
    )


# Depth cap for the transitive concrete-member walk (fail-closed above it).
_ANCESTOR_DEPTH_CAP = 5


def _transitive_concrete_members(
    base_entity: JsonObject,
    head_snapshot: "KgSnapshot",
    subclass_repo: str,
    head_root: Path,
    *,
    depth: int,
    visited: set[str],
) -> tuple[set[str], bool]:
    """Concrete members supplied by a preceding base THROUGH its own MRO, depth-capped.

    Unions the base's local concrete members with those of its recursively-resolved
    ancestors, so ``class FullMixin(Impl): pass`` contributes ``Impl``'s implementations
    (Python's MRO satisfies the contract before construction). Binding-aware
    abstractmethod classification uses each ancestor file's own import context.

    Returns (supplied, certain). ``supplied`` is the concrete members provable from the
    resolvable part of the chain. ``certain`` is False when the base or any ancestor
    cannot be resolved/parsed, on a cycle, or when the depth cap is exceeded — an unseen
    ancestor could supply more members, so the caller must fail closed unless the certain
    part already covers every required member. Depth-capped so the walk stays bounded.
    """
    if depth > _ANCESTOR_DEPTH_CAP:
        return set(), False
    entity_id = str(base_entity.get("entity_id") or "")
    if entity_id and entity_id in visited:
        return set(), False
    if entity_id:
        visited.add(entity_id)

    parsed = _parse_base_entity(base_entity, head_root)
    if parsed is None:
        return set(), False
    tree, class_def, _bpath, _qualname, _line = parsed
    bindings = _abc_bindings(tree)

    supplied = _concrete_member_names(class_def, bindings)
    certain = True
    for ancestor_name in _base_names(class_def):
        ancestor_entity = _resolve_base_entity(ancestor_name, head_snapshot, subclass_repo)
        if ancestor_entity is None:
            certain = False
            continue
        ancestor_members, ancestor_certain = _transitive_concrete_members(
            ancestor_entity,
            head_snapshot,
            subclass_repo,
            head_root,
            depth=depth + 1,
            visited=visited,
        )
        supplied |= ancestor_members
        certain = certain and ancestor_certain
    return supplied, certain


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

        try:
            head_tree = ast.parse(head_source)
        except SyntaxError:
            stats.parse_failures += 1
            continue
        # Binding-aware abc markers for the subclass's own file: a locally-defined
        # ``abstractmethod`` decorator must not read as real abc semantics when
        # classifying the subclass's members concrete vs abstract-redeclared.
        head_bindings = _abc_bindings(head_tree)

        raw_line = props.get("line")
        head_line = int(raw_line) if raw_line is not None else None
        # head_line is the head-snapshot coordinate; only anchor the head lookup with
        # it. The base checkout may place the same qualname on a different line, so the
        # base lookup relies on leaf uniqueness / nesting-path disambiguation.
        head_class = _class_def_in_source(head_source, qualname, line=head_line)
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
        # Concrete-only: a subclass that REDECLARES a required member with
        # @abstractmethod stays abstract and still raises at construction, so it
        # must not count as satisfying the member. Assignments still count as
        # concrete overrides. Mirrors the MRO subtraction path, which already uses
        # concrete-only collection via _transitive_concrete_members below.
        defined = _concrete_member_names(head_class, head_bindings)
        empty_body = _body_is_empty(head_class)

        for base_name in new_bases:
            base_entity = _resolve_base_entity(base_name, head_snapshot, subclass_repo)
            if base_entity is None:
                continue
            abstract_base = _abstract_base_from_entity(base_entity, head_root)
            if abstract_base is None:
                continue

            # MRO fail-closed, order-sensitive: getattr walks the MRO left-to-right, so
            # only bases listed BEFORE this abstract base can supply its members. Each
            # preceding base contributes its concrete members THROUGH its own MRO (so
            # ``class FullMixin(Impl): pass`` counts Impl's implementations), depth-capped
            # with a cycle-guarding visited set. If any base or ancestor in that chain
            # cannot be resolved/parsed, the walk is uncertain — an unseen implementation
            # could exist, so suppress unless the resolvable part already satisfies every
            # required member. Bases listed AFTER the abstract base are irrelevant to it.
            base_index = head_bases.index(base_name)
            preceding_bases = head_bases[:base_index]
            mro_supplied: set[str] = set()
            preceding_certain = True
            for other_name in preceding_bases:
                other_entity = _resolve_base_entity(other_name, head_snapshot, subclass_repo)
                if other_entity is None:
                    preceding_certain = False
                    continue
                concrete, certain = _transitive_concrete_members(
                    other_entity,
                    head_snapshot,
                    subclass_repo,
                    head_root,
                    depth=0,
                    visited=set(),
                )
                mro_supplied |= concrete
                preceding_certain = preceding_certain and certain

            unimplemented = tuple(
                m
                for m in abstract_base.required_members
                if m not in defined and m not in mro_supplied
            )
            if not unimplemented:
                # Every required member satisfied by the subclass or the resolvable MRO
                # part → no risk, regardless of uncertainty elsewhere.
                continue
            if not preceding_certain:
                # A preceding base (or one of its ancestors) is unresolvable/unparseable
                # AND the resolvable part does not cover all required members — an unseen
                # ancestor could supply the remainder, so a high-confidence row must not
                # survive. Suppress.
                stats.mro_suppressed += 1
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
