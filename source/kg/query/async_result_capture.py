from __future__ import annotations

import ast
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING

from source.kg.core.models import JsonObject
from source.kg.languages.typescript.files import TYPESCRIPT_EXTENSIONS

if TYPE_CHECKING:
    from source.kg.query.snapshot import KgSnapshot


_RISK_TYPE = "unawaited_async_result_capture"
# Pragmatic API allowlist, not prose keyword detection: the AST has already proven
# the inner call result is being passed as an argument, and these stdlib schedulers
# are the only static signal available without type resolution. Collision-prone
# method names are root-guarded in _is_python_coroutine_scheduler.
_PYTHON_COROUTINE_SCHEDULERS = {
    "as_completed",
    "create_task",
    "ensure_future",
    "gather",
    "run",
    "run_until_complete",
    "run_coroutine_threadsafe",
    "shield",
    "wait",
    "wait_for",
}
_PYTHON_COROUTINE_SCHEDULER_COLLISION_PRONE = {
    "gather",
    "run",
    "shield",
    "wait",
    "wait_for",
}


@dataclass(frozen=True)
class _FunctionInfo:
    node: ast.FunctionDef | ast.AsyncFunctionDef
    path: str
    qualname: str


def unawaited_async_result_capture(
    *,
    base_snapshot: "KgSnapshot",
    head_snapshot: "KgSnapshot",
    base_root: Path,
    head_root: Path,
    changed_symbols: list[JsonObject],
) -> tuple[list[JsonObject], JsonObject]:
    """Detect callers that still capture/use a function result after it became async.

    Python implementation is AST-backed and fail-closed: unreadable/unparseable files,
    unresolved symbols, and callers whose relevant call cannot be found emit no row.
    Argument-position uses are heuristic leads: framework/custom scheduler wrappers may
    await internally, so emitted rows always carry source_checks and negative_checks.
    """

    stats: JsonObject = {
        "cross_snapshot_pairing": {
            "candidate_symbols": 0,
            "paired_symbols": 0,
        },
        "symbols_checked": 0,
        "asyncness_changes": 0,
        "callers_checked": 0,
        "callers_skipped": 0,
        "rows_generated": 0,
    }
    base_by_coord = _symbols_by_path_qualname(base_snapshot)
    head_changed = _changed_head_entities(head_snapshot, changed_symbols)
    stats["cross_snapshot_pairing"]["candidate_symbols"] = len(head_changed)
    ts_head_parse_paths = _typescript_head_parse_paths(head_snapshot, head_changed)
    ts_base_parse_paths = {
        _normalize_path(str((symbol.get("properties") or {}).get("path") or ""))
        for symbol in head_changed
        if _is_typescript_path(str((symbol.get("properties") or {}).get("path") or ""))
    }
    ts_base_parsed: dict[str, JsonObject] | None = None
    ts_head_parsed: dict[str, JsonObject] | None = None
    rows: list[JsonObject] = []
    for head_symbol in head_changed:
        props = head_symbol.get("properties") or {}
        path = str(props.get("path") or "")
        if not (path.endswith(".py") or _is_typescript_path(path)):
            continue
        identity = head_symbol.get("identity") or {}
        qualname = str(identity.get("qualname") or "")
        if not qualname:
            continue
        base_symbol = base_by_coord.get((_normalize_path(path), qualname))
        if base_symbol is None:
            continue
        stats["cross_snapshot_pairing"]["paired_symbols"] += 1
        stats["symbols_checked"] += 1
        if _is_typescript_path(path):
            if ts_head_parsed is None:
                ts_head_parsed = _parse_typescript_root(
                    head_root,
                    paths=ts_head_parse_paths,
                    stats=stats,
                    role="head",
                )
            if ts_head_parsed is None:
                continue
            base_async = _typescript_symbol_asyncness_from_entity(base_symbol)
            if base_async is None:
                # Fresh Wave5 snapshots carry asyncness on CodeSymbol properties. The
                # parse fallback keeps explicit base snapshots built by older code usable.
                if ts_base_parsed is None:
                    ts_base_parsed = _parse_typescript_root(
                        base_root,
                        paths=ts_base_parse_paths,
                        stats=stats,
                        role="base",
                    )
                if ts_base_parsed is None:
                    continue
                base_async = _typescript_symbol_asyncness_from_parsed(ts_base_parsed, path, qualname)
            head_async = _typescript_symbol_asyncness_from_entity(head_symbol)
            if head_async is None:
                head_async = _typescript_symbol_asyncness_from_parsed(ts_head_parsed, path, qualname)
            if base_async is None or head_async is None:
                continue
            rows.extend(
                _rows_for_typescript_symbol(
                    head_parsed=ts_head_parsed,
                    head_snapshot=head_snapshot,
                    async_symbol=head_symbol,
                    path=path,
                    qualname=qualname,
                    base_async=base_async,
                    head_async=head_async,
                    stats=stats,
                )
            )
            continue
        base_info = _function_info(base_root, base_symbol)
        head_info = _function_info(head_root, head_symbol)
        if base_info is None or head_info is None:
            continue
        if not (isinstance(base_info.node, ast.FunctionDef) and isinstance(head_info.node, ast.AsyncFunctionDef)):
            continue
        stats["asyncness_changes"] += 1
        rows.extend(
            _rows_for_python_callers(
                head_snapshot=head_snapshot,
                head_root=head_root,
                async_symbol=head_symbol,
                async_info=head_info,
                stats=stats,
            )
        )
    stats["rows_generated"] = len(rows)
    return rows, stats


def _rows_for_typescript_symbol(
    *,
    head_parsed: dict[str, JsonObject],
    head_snapshot: "KgSnapshot",
    async_symbol: JsonObject,
    path: str,
    qualname: str,
    base_async: bool,
    head_async: bool,
    stats: JsonObject,
) -> list[JsonObject]:
    if base_async or not head_async:
        return []
    stats["asyncness_changes"] += 1
    rows: list[JsonObject] = []
    callee_id = async_symbol.get("entity_id")
    if not callee_id:
        return rows
    callee_leaf = qualname.rsplit(".", 1)[-1]
    for fact in head_snapshot.facts:
        if fact.get("predicate") != "CALLS" or fact.get("object_id") != callee_id:
            continue
        caller = head_snapshot.entities_by_id.get(str(fact.get("subject_id") or ""))
        if not caller or caller.get("kind") != "CodeSymbol":
            continue
        stats["callers_checked"] += 1
        caller_identity = caller.get("identity") or {}
        caller_props = caller.get("properties") or {}
        caller_qualname = str(caller_identity.get("qualname") or "")
        caller_path = str(caller_props.get("path") or "")
        use = _typescript_capture_use(
            head_parsed,
            caller_path,
            caller_qualname,
            callee_leaf,
            expected_line=_call_fact_line(head_snapshot, fact),
        )
        if use is None:
            continue
        rows.append(_row_typescript(async_symbol, path, qualname, caller, caller_path, caller_qualname, use))
    return rows


def _rows_for_python_callers(
    *,
    head_snapshot: "KgSnapshot",
    head_root: Path,
    async_symbol: JsonObject,
    async_info: _FunctionInfo,
    stats: JsonObject,
) -> list[JsonObject]:
    rows: list[JsonObject] = []
    callee_id = async_symbol.get("entity_id")
    if not callee_id:
        return rows
    for fact in head_snapshot.facts:
        if fact.get("predicate") != "CALLS" or fact.get("object_id") != callee_id:
            continue
        caller = head_snapshot.entities_by_id.get(str(fact.get("subject_id") or ""))
        if not caller or caller.get("kind") != "CodeSymbol":
            continue
        stats["callers_checked"] += 1
        caller_info = _function_info(head_root, caller)
        if caller_info is None:
            stats["callers_skipped"] += 1
            continue
        call_node, use_kind = _first_unawaited_result_use(
            caller_info.node,
            async_info.qualname.rsplit(".", 1)[-1],
            expected_line=_call_fact_line(head_snapshot, fact),
        )
        if call_node is None:
            continue
        row = _row(async_symbol, async_info, caller, caller_info, call_node, use_kind=use_kind)
        rows.append(row)
    return rows


def _row(
    async_symbol: JsonObject,
    async_info: _FunctionInfo,
    caller_symbol: JsonObject,
    caller_info: _FunctionInfo,
    call_node: ast.Call,
    *,
    use_kind: str | None,
) -> JsonObject:
    async_props = async_symbol.get("properties") or {}
    caller_props = caller_symbol.get("properties") or {}
    caller_identity = caller_symbol.get("identity") or {}
    async_identity = async_symbol.get("identity") or {}
    caller_line = int(getattr(call_node, "lineno", caller_props.get("line") or 1))
    async_line = int(async_props.get("line") or 1)
    repo = async_identity.get("repo") or caller_identity.get("repo")
    cause: JsonObject = {"path": caller_info.path, "line_start": caller_line, "qualname": caller_info.qualname}
    consequence: JsonObject = {"path": async_info.path, "line_start": async_line, "qualname": async_info.qualname}
    if repo:
        cause["repo"] = repo
        consequence["repo"] = repo
    payload = f"{_RISK_TYPE}|{async_symbol.get('urn')}|{caller_info.path}|{caller_line}|{caller_info.qualname}"
    hypothesis_id = f"hypothesis:{_RISK_TYPE}:{sha256(payload.encode()).hexdigest()[:16]}"
    postable_claim = (
        f"{async_info.qualname} became async in this diff; {caller_info.qualname} "
        f"({caller_info.path}:{caller_line}) stores/uses its result without await, so it now holds "
        "a coroutine where a resolved value was expected."
    )
    return {
        "hypothesis_id": hypothesis_id,
        "risk_type": _RISK_TYPE,
        "specificity": "high",
        "confidence": "medium",
        "derivation": "deterministic_static",
        "use_context": use_kind or "unknown",
        "postable_claim": postable_claim,
        "concrete_invariant": postable_claim,
        "why": "A changed Python function became AsyncFunctionDef while a static CALLS caller still uses the call result without ast.Await.",
        "cause": cause,
        "consequence": consequence,
        "source_spans": [cause, consequence],
        "evidence_refs": [cause, consequence],
        "source_checks": [
            f"Inspect {caller_info.path}:{caller_line}; confirm the call to {async_info.qualname} is not awaited before use.",
            f"Inspect {async_info.path}:{async_line}; confirm {async_info.qualname} is async in head and was sync in base.",
        ],
        "negative_checks": [
            "If the caller awaits the value before use, chains/schedules it intentionally, or intentionally stores a coroutine for later awaiting, do not post.",
        ],
        "subject_urn": str(async_symbol.get("urn") or ""),
        "subject_qualname": async_info.qualname,
        "subject_path": async_info.path,
        "caller_qualname": caller_info.qualname,
    }


def _row_typescript(
    async_symbol: JsonObject,
    async_path: str,
    async_qualname: str,
    caller_symbol: JsonObject,
    caller_path: str,
    caller_qualname: str,
    use: JsonObject,
) -> JsonObject:
    async_props = async_symbol.get("properties") or {}
    async_identity = async_symbol.get("identity") or {}
    caller_identity = caller_symbol.get("identity") or {}
    line = _int(use.get("line")) or _int((caller_symbol.get("properties") or {}).get("line")) or 1
    async_line = _int(async_props.get("line")) or 1
    repo = async_identity.get("repo") or caller_identity.get("repo")
    cause: JsonObject = {"path": caller_path, "line_start": line, "qualname": caller_qualname}
    consequence: JsonObject = {"path": async_path, "line_start": async_line, "qualname": async_qualname}
    if repo:
        cause["repo"] = repo
        consequence["repo"] = repo
    payload = f"{_RISK_TYPE}|{async_symbol.get('urn')}|{caller_path}|{line}|{caller_qualname}"
    hypothesis_id = f"hypothesis:{_RISK_TYPE}:{sha256(payload.encode()).hexdigest()[:16]}"
    postable_claim = (
        f"{async_qualname} became async in this diff; {caller_qualname} "
        f"({caller_path}:{line}) stores/uses its result without await, so it now holds "
        "a Promise where a resolved value was expected."
    )
    use_context = str(use.get("context") or "unknown")
    return {
        "hypothesis_id": hypothesis_id,
        "risk_type": _RISK_TYPE,
        "specificity": "high",
        "confidence": "medium",
        "derivation": "deterministic_static",
        "use_context": use_context,
        "postable_claim": postable_claim,
        "concrete_invariant": postable_claim,
        "why": "A changed TypeScript/JavaScript symbol became async or Promise-returning while a static CALLS caller still uses the result without await/then.",
        "cause": cause,
        "consequence": consequence,
        "source_spans": [cause, consequence],
        "evidence_refs": [cause, consequence],
        "source_checks": [
            f"Inspect {caller_path}:{line}; confirm the call to {async_qualname} is not awaited or then-chained before use.",
            f"Inspect {async_path}:{async_line}; confirm {async_qualname} is async/Promise-returning in head and was sync in base.",
        ],
        "negative_checks": [
            "If the caller awaits the value, then-chains it, or intentionally stores a Promise for later resolution, do not post.",
        ],
        "subject_urn": str(async_symbol.get("urn") or ""),
        "subject_qualname": async_qualname,
        "subject_path": async_path,
        "caller_qualname": caller_qualname,
    }


def _parse_typescript_root(
    root: Path,
    *,
    paths: set[str] | None = None,
    stats: JsonObject | None = None,
    role: str = "unknown",
) -> dict[str, JsonObject] | None:
    try:
        from source.kg.core.repo_source import RepoSnapshot, discover_repo
        from source.kg.languages.typescript.extractors.parser_bridge import parse_typescript_repo

        repo = discover_repo(root)
        if paths is not None:
            wanted = {_normalize_path(path) for path in paths if path}
            narrowed_files = tuple(
                path for path in repo.files_by_language.get("typescript", ())
                if _normalize_path(str(path.relative_to(repo.root))) in wanted
            )
            repo = RepoSnapshot(
                root=repo.root,
                name=repo.name,
                owner=repo.owner,
                commit_sha=repo.commit_sha,
                files_by_language={**dict(repo.files_by_language), "typescript": narrowed_files},
                unsupported_files_by_language=repo.unsupported_files_by_language,
            )
        return parse_typescript_repo(repo, None)
    except Exception as exc:  # noqa: BLE001
        if stats is not None:
            stats["typescript_parse_error_count"] = int(stats.get("typescript_parse_error_count") or 0) + 1
            errors = stats.get("typescript_parse_errors")
            if not isinstance(errors, list):
                errors = []
                stats["typescript_parse_errors"] = errors
            if len(errors) < 3:
                errors.append(f"{role}:{type(exc).__name__}:{str(exc)[:160]}")
        return None


def _typescript_head_parse_paths(head_snapshot: "KgSnapshot", head_changed: list[JsonObject]) -> set[str]:
    paths: set[str] = set()
    for symbol in head_changed:
        props = symbol.get("properties") or {}
        path = str(props.get("path") or "")
        if not _is_typescript_path(path):
            continue
        paths.add(_normalize_path(path))
        paths.update(_typescript_caller_paths(head_snapshot, symbol))
    return paths


def _typescript_caller_paths(head_snapshot: "KgSnapshot", async_symbol: JsonObject) -> set[str]:
    callee_id = async_symbol.get("entity_id")
    if not callee_id:
        return set()
    paths: set[str] = set()
    for fact in head_snapshot.facts:
        if fact.get("predicate") != "CALLS" or fact.get("object_id") != callee_id:
            continue
        caller = head_snapshot.entities_by_id.get(str(fact.get("subject_id") or ""))
        if not caller or caller.get("kind") != "CodeSymbol":
            continue
        path = str((caller.get("properties") or {}).get("path") or "")
        if _is_typescript_path(path):
            paths.add(_normalize_path(path))
    return paths


def _typescript_symbol_meta(
    parsed: dict[str, JsonObject],
    path: str,
    qualname: str,
) -> JsonObject | None:
    parsed_file = parsed.get(_normalize_path(path))
    if not isinstance(parsed_file, dict):
        return None
    matches = [
        row for row in parsed_file.get("symbols") or []
        if isinstance(row, dict) and row.get("name") == qualname
    ]
    if len(matches) != 1:
        return None
    return matches[0]


def _typescript_symbol_asyncness_from_entity(symbol: JsonObject) -> bool | None:
    props = symbol.get("properties") or {}
    if "is_async" not in props and "returns_promise_type" not in props:
        return None
    return bool(props.get("is_async") or props.get("returns_promise_type"))


def _typescript_symbol_asyncness_from_parsed(
    parsed: dict[str, JsonObject],
    path: str,
    qualname: str,
) -> bool | None:
    meta = _typescript_symbol_meta(parsed, path, qualname)
    if meta is None:
        return None
    return bool(meta.get("is_async") or meta.get("returns_promise_type"))


def _typescript_capture_use(
    parsed: dict[str, JsonObject],
    caller_path: str,
    caller_qualname: str,
    callee_leaf: str,
    expected_line: int | None = None,
) -> JsonObject | None:
    parsed_file = parsed.get(_normalize_path(caller_path))
    if not isinstance(parsed_file, dict):
        return None
    for row in parsed_file.get("async_result_capture_uses") or []:
        if not isinstance(row, dict):
            continue
        if row.get("qualname") != caller_qualname:
            continue
        if expected_line is not None and _int(row.get("line")) != expected_line:
            continue
        callee = str(row.get("callee") or "")
        if callee.rsplit(".", 1)[-1] == callee_leaf:
            return row
    return None


def _first_unawaited_result_use(
    function_node: ast.FunctionDef | ast.AsyncFunctionDef,
    callee_leaf: str,
    expected_line: int | None = None,
) -> tuple[ast.Call | None, str | None]:
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(function_node):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    class _Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.matches: list[tuple[ast.Call, str]] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            if node is function_node:
                self.generic_visit(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            if node is function_node:
                self.generic_visit(node)

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            return

        def visit_Call(self, node: ast.Call) -> None:
            if expected_line is not None and self.matches:
                return
            if expected_line is not None and getattr(node, "lineno", None) != expected_line:
                self.generic_visit(node)
                return
            use_kind = _call_result_use_kind_without_await(node, parents)
            if _call_leaf(node.func) == callee_leaf and use_kind is not None:
                self.matches.append((node, use_kind))
                return
            self.generic_visit(node)

    visitor = _Visitor()
    visitor.visit(function_node)
    if len(visitor.matches) != 1:
        return None, None
    return visitor.matches[0]


def _call_result_use_kind_without_await(node: ast.Call, parents: dict[ast.AST, ast.AST]) -> str | None:
    current: ast.AST = node
    if current in parents and isinstance(parents[current], ast.Await):
        return None
    parent = parents.get(node)
    child: ast.AST = node
    while isinstance(parent, ast.BinOp | ast.BoolOp | ast.Compare | ast.IfExp | ast.UnaryOp | ast.Subscript | ast.Attribute):
        child = parent
        if child in parents and isinstance(parents[child], ast.Await):
            return None
        parent = parents.get(child)
    if isinstance(parent, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        return "assignment"
    if isinstance(parent, ast.Return):
        return "return"
    if isinstance(parent, ast.Call) and any(arg is child for arg in parent.args):
        if _is_python_coroutine_scheduler(parent):
            return None
        return "argument"
    if isinstance(parent, ast.keyword) and parent.value is child:
        scheduler = parents.get(parent)
        if isinstance(scheduler, ast.Call) and _is_python_coroutine_scheduler(scheduler):
            return None
        return "argument"
    return None


def _is_python_coroutine_scheduler(node: ast.Call) -> bool:
    leaf = _call_leaf(node.func)
    if leaf not in _PYTHON_COROUTINE_SCHEDULERS:
        return False
    if leaf in _PYTHON_COROUTINE_SCHEDULER_COLLISION_PRONE:
        root = _call_root(node.func)
        return root == "asyncio"
    return True


def _call_leaf(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _call_root(node: ast.AST) -> str | None:
    current = node
    while isinstance(current, ast.Attribute):
        current = current.value
    if isinstance(current, ast.Name):
        return current.id
    return None


def _function_info(root: Path, symbol: JsonObject) -> _FunctionInfo | None:
    identity = symbol.get("identity") or {}
    props = symbol.get("properties") or {}
    path = str(props.get("path") or "")
    qualname = str(identity.get("qualname") or "")
    if not path or not qualname:
        return None
    source_path = root / path
    try:
        source = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(source_path))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None
    line = _int(props.get("line"))
    function_rows = _iter_functions(tree)
    for node, node_qualname in function_rows:
        if node_qualname == qualname:
            return _FunctionInfo(node=node, path=path, qualname=qualname)
    for node, node_qualname in function_rows:
        if line is not None and getattr(node, "lineno", None) == line:
            return _FunctionInfo(node=node, path=path, qualname=node_qualname)
    return None


def _call_fact_line(snapshot: "KgSnapshot", fact: JsonObject) -> int | None:
    fact_id = fact.get("fact_id")
    if not isinstance(fact_id, str):
        return None
    for evidence in snapshot.evidence_by_target.get(fact_id, []):
        if not isinstance(evidence, dict):
            continue
        ref = evidence.get("bytes_ref")
        if not isinstance(ref, dict):
            continue
        line = _int(ref.get("line_start"))
        if line is not None:
            return line
    return None


def _iter_functions(tree: ast.Module) -> list[tuple[ast.FunctionDef | ast.AsyncFunctionDef, str]]:
    rows: list[tuple[ast.FunctionDef | ast.AsyncFunctionDef, str]] = []

    def visit_body(body: list[ast.stmt], prefix: str = "") -> None:
        for stmt in body:
            if isinstance(stmt, ast.ClassDef):
                visit_body(stmt.body, f"{prefix}{stmt.name}.")
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                rows.append((stmt, f"{prefix}{stmt.name}"))

    visit_body(tree.body)
    return rows


def _symbols_by_path_qualname(snapshot: "KgSnapshot") -> dict[tuple[str, str], JsonObject]:
    rows: dict[tuple[str, str], JsonObject] = {}
    for entity in snapshot.entities:
        if entity.get("kind") != "CodeSymbol":
            continue
        identity = entity.get("identity") or {}
        props = entity.get("properties") or {}
        path = str(props.get("path") or "")
        qualname = str(identity.get("qualname") or "")
        if path and qualname:
            rows[(_normalize_path(path), qualname)] = entity
    return rows


def _changed_head_entities(head_snapshot: "KgSnapshot", changed_symbols: list[JsonObject]) -> list[JsonObject]:
    wanted = {
        (_normalize_path(str(row.get("path") or "")), str(row.get("qualname") or row.get("qualified_name") or ""))
        for row in changed_symbols
        if isinstance(row, dict)
    }
    return [
        entity for entity in head_snapshot.entities
        if entity.get("kind") == "CodeSymbol"
        and (
            _normalize_path(str((entity.get("properties") or {}).get("path") or "")),
            str((entity.get("identity") or {}).get("qualname") or ""),
        ) in wanted
    ]


def _normalize_path(path: str) -> str:
    value = path.replace("\\", "/").strip()
    while value.startswith("./"):
        value = value[2:]
    return value


def _is_typescript_path(path: str) -> bool:
    return not path.endswith(".d.ts") and Path(path).suffix in TYPESCRIPT_EXTENSIONS


def _int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
