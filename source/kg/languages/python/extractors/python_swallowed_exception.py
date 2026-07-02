"""Adapter: Python swallowed-exception risk signals.

Walks parsed Python AST trees and emits ``code_risk_signal`` support-facts for
except handlers whose body is vacuous (pass / continue / return <constant> /
pure assignments with no calls) and whose exception type is broad
(bare ``except:``, ``except Exception:``, ``except BaseException:``).

If the handler body contains ANY call, raise, or import the signal is suppressed
(conservatism binding).

Per-symbol cap of 3 signals (lowest line first) is enforced before emitting.
Coverage refusal rows are emitted for unparseable files.

Reuse note:
  - ``ParsedPythonFile`` imported from ast_extractor (clean public dataclass).
  - parse_python_repo() mirrors parse_typescript_repo() from parser_bridge.py.
  - Entity identity shape mirrors PythonAstExtractor._symbol() (cited: ast_extractor.py:614-627).
  - bytes_ref shape mirrors PythonAstExtractor._bytes_ref() (cited: ast_extractor.py:911-918).
  New vs reused: symbol collection is replicated inline (private method, no public API).
"""
from __future__ import annotations

import ast
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from source.kg.core.models import Coverage, Entity, Evidence, Fact
from source.kg.core.repo_source import RepoSnapshot
from source.kg.core.tenant import resolve_tenant_id
from source.kg.extraction.framework.adapter import (
    AdapterCapability,
    AdapterResult,
    ExtractionContext,
)
from source.kg.languages.python.extractors.ast_extractor import ParsedPythonFile

_SOURCE_SYSTEM = "python_swallowed_exception_v0"
_PREDICATE = "code_risk_signal"
_RISK_FAMILY = "swallowed_exception"
_BROAD_EXCEPTION_NAMES: frozenset[str] = frozenset({"Exception", "BaseException"})
_PER_SYMBOL_CAP = 3


# ---------------------------------------------------------------------------
# Parsing helpers (mirror parse_typescript_repo pattern)
# ---------------------------------------------------------------------------

def parse_python_repo(
    repo: RepoSnapshot,
    ctx: ExtractionContext | None = None,
) -> dict[Path, ParsedPythonFile]:
    """Return per-file parsed Python ASTs, using ctx cache when available.

    Cache key mirrors PythonAstExtractor._repo_cache_key() so both share the
    same cached entries when running in the same extraction pass.
    """
    cache_key = f"{repo.root}:{repo.commit_sha}"
    if ctx is not None:
        python_cache = ctx.parsed_by_language.setdefault("python", {})
        cached = python_cache.get(cache_key)
        if isinstance(cached, dict):
            return cached  # type: ignore[return-value]

    result = _parse_python_repo_uncached(repo)
    if ctx is not None:
        ctx.parsed_by_language.setdefault("python", {})[cache_key] = result
    return result


def _parse_python_repo_uncached(repo: RepoSnapshot) -> dict[Path, ParsedPythonFile]:
    parsed: dict[Path, ParsedPythonFile] = {}
    for file_path in repo.files_by_language.get("python", ()):
        source = file_path.read_text(encoding="utf-8", errors="replace")
        line_count = len(source.splitlines())
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                tree = ast.parse(source, filename=str(file_path))
        except SyntaxError as exc:
            parsed[file_path] = ParsedPythonFile(
                tree=None, line_count=line_count, source_text=source, syntax_error=exc
            )
        else:
            parsed[file_path] = ParsedPythonFile(
                tree=tree, line_count=line_count, source_text=source
            )
    return parsed


# ---------------------------------------------------------------------------
# Symbol collection (minimal — only function/method qualnames + entity shape)
# Replicated from PythonAstExtractor._collect_symbols / _symbol (ast_extractor.py:567-627)
# because those are private methods with no public extraction API.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _SymbolRef:
    entity: Entity
    qualname: str
    symbol_kind: str
    line: int
    end_line: int


def _module_name(repo: RepoSnapshot, file_path: Path) -> str:
    relative = file_path.relative_to(repo.root).with_suffix("")
    parts = list(relative.parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) or repo.name


def _collect_function_symbols(
    repo: RepoSnapshot,
    file_path: Path,
    module_name: str,
    tree: ast.AST,
    tenant_id: str,
) -> list[_SymbolRef]:
    """Collect top-level and class-nested functions/methods only.

    Mirrors PythonAstExtractor._collect_symbols (ast_extractor.py:578-593):
    recurses into ClassDef bodies but NOT into FunctionDef bodies, so nested
    functions (outer.inner) are never emitted.  entity_ids produced here must
    align with those from the main extractor for subject-id matching to work.
    """
    symbols: list[_SymbolRef] = []

    def visit(body: list[ast.stmt], prefix: str = "") -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                qualname = f"{prefix}.{node.name}" if prefix else node.name
                visit(node.body, qualname)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualname = f"{prefix}.{node.name}" if prefix else node.name
                kind: str
                # Mirror PythonAstExtractor._collect_symbols precedence
                # (ast_extractor.py:584-589): set async_function/function first,
                # then override to "method" when inside a class (prefix set).
                kind = "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function"
                if prefix:
                    kind = "method"
                line = getattr(node, "lineno", 1)
                end_line = getattr(node, "end_lineno", line)
                entity = Entity(
                    kind="CodeSymbol",
                    identity={
                        "tenant_id": tenant_id,
                        "repo": repo.name,
                        "module": module_name,
                        "qualname": qualname,
                        "symbol_kind": kind,
                    },
                    properties={"path": str(file_path.relative_to(repo.root)), "line": line},
                )
                symbols.append(_SymbolRef(entity, qualname, kind, line, end_line))
                # Do NOT recurse into function bodies: the main extractor does
                # not emit nested-function symbols, so we must not either.

    if isinstance(tree, ast.Module):
        visit(tree.body)
    return symbols


# ---------------------------------------------------------------------------
# Exception handler vacuousness check
# ---------------------------------------------------------------------------

def _is_broad_handler(handler: ast.excepthandler) -> tuple[bool, str]:
    """Return (is_broad, exception_type_label).

    Broad means: bare except OR except Exception OR except BaseException.
    Narrow (ValueError, KeyError, etc.) → not broad.
    """
    if not isinstance(handler, ast.ExceptHandler):
        return False, ""
    exc_type = handler.type
    if exc_type is None:
        return True, "bare"
    if isinstance(exc_type, ast.Name) and exc_type.id in _BROAD_EXCEPTION_NAMES:
        return True, exc_type.id
    if isinstance(exc_type, ast.Tuple):
        # except (Exception, SomeOther): — only broad if ALL are broad
        for elt in exc_type.elts:
            if not (isinstance(elt, ast.Name) and elt.id in _BROAD_EXCEPTION_NAMES):
                return False, ""
        return True, "tuple_broad"
    return False, ""


def _handler_body_is_vacuous(body: list[ast.stmt]) -> bool:
    """Return True iff every statement in body is pass/continue/return-constant/
    pure assignment (no calls, no raises, no augmented assigns, no imports,
    no nested try/with/for/while/if that could hide calls).

    Any call node anywhere in the body → False (conservatism binding).
    Any Raise node → False.
    """
    for stmt in body:
        if isinstance(stmt, ast.Pass):
            continue
        if isinstance(stmt, ast.Continue):
            continue
        if isinstance(stmt, ast.Return):
            value = stmt.value
            if value is None:
                # bare return
                continue
            if isinstance(value, ast.Constant):
                continue
            # return with non-constant expression → could be a call
            return False
        if isinstance(stmt, ast.Assign):
            # Vacuous only if ALL targets are simple names AND the RHS is a
            # constant (ast.Constant, which covers None, True, False, literals).
            # Attribute targets (self.x), subscript targets (d[k]), or any
            # non-constant RHS indicate state-changing assignments → not vacuous.
            if not all(isinstance(t, ast.Name) for t in stmt.targets):
                return False
            if not isinstance(stmt.value, ast.Constant):
                return False
            continue
        # Any raise, import, expr, augassign, annassign, for, while, with, if,
        # try, delete, global, nonlocal, assert, match → not vacuous
        return False
    return True


# ---------------------------------------------------------------------------
# Core signal extraction
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Signal:
    enclosing_entity: Entity
    enclosing_qualname: str
    exception_type: str
    line: int
    end_line: int


def _collect_signals_from_file(
    repo: RepoSnapshot,
    file_path: Path,
    tree: ast.AST,
    tenant_id: str,
) -> list[_Signal]:
    """Walk all except handlers in the file and return vacuous broad-exception ones.

    Uses an explicit traversal that tracks the current function-def stack instead
    of ast.walk(), so that handlers inside nested functions (or classes defined
    inside a function) are NOT attributed to the outer collected symbol.

    Conservatism binding: emit a signal ONLY when the handler's innermost enclosing
    FunctionDef/AsyncFunctionDef IS one of the collected symbols.  Handlers inside
    nested functions are skipped entirely (mirrors the TS detector's
    skip-when-unmatched rule).

    Limitation: class-in-function bodies are treated as opaque — any try/except
    inside them is skipped, matching the conservative skip-when-nested rule.
    """
    module_name = _module_name(repo, file_path)
    symbols = _collect_function_symbols(repo, file_path, module_name, tree, tenant_id)
    # Build a fast lookup: (line, end_line) → _SymbolRef for O(1) membership test
    collected_line_ranges: dict[tuple[int, int], _SymbolRef] = {
        (s.line, s.end_line): s for s in symbols
    }

    signals: list[_Signal] = []

    def _visit_body(body: list[ast.stmt], fn_stack: list[_SymbolRef | None]) -> None:
        """Recursively visit statement list.

        fn_stack: innermost-first list of enclosing FunctionDef/_SymbolRef entries.
        None entries mark nested (non-collected) function layers.
        """
        for node in body:
            if isinstance(node, ast.ClassDef):
                if fn_stack:
                    # Class defined inside a function — opaque, skip entirely
                    # (conservative: no collected symbol corresponds to any code here)
                    pass
                else:
                    # Top-level class: visit its methods as collected symbols
                    _visit_body(node.body, fn_stack)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                sym_key = (getattr(node, "lineno", -1), getattr(node, "end_lineno", -1))
                sym_ref = collected_line_ranges.get(sym_key)
                # Push the symbol (or None if not collected / nested)
                new_stack = [sym_ref] + fn_stack
                _visit_body(node.body, new_stack)
            elif isinstance(node, ast.Try):
                for handler in node.handlers:
                    if not isinstance(handler, ast.ExceptHandler):
                        continue
                    # Only attribute when innermost enclosing function IS collected
                    if not fn_stack or fn_stack[0] is None:
                        continue
                    enclosing = fn_stack[0]
                    is_broad, exc_type_label = _is_broad_handler(handler)
                    if not is_broad:
                        continue
                    if not _handler_body_is_vacuous(handler.body):
                        continue
                    h_line = getattr(handler, "lineno", enclosing.line)
                    h_end = getattr(handler, "end_lineno", h_line)
                    signals.append(_Signal(
                        enclosing_entity=enclosing.entity,
                        enclosing_qualname=enclosing.qualname,
                        exception_type=exc_type_label,
                        line=h_line,
                        end_line=h_end,
                    ))
                # Also walk the try body and else/final clauses for nested try blocks
                _visit_body(node.body, fn_stack)
                for handler in node.handlers:
                    if isinstance(handler, ast.ExceptHandler):
                        _visit_body(handler.body, fn_stack)
                if node.orelse:
                    _visit_body(node.orelse, fn_stack)
                if node.finalbody:
                    _visit_body(node.finalbody, fn_stack)
            else:
                # Visit sub-statements of any other compound node (for/while/if/with)
                sub_stmts: list[ast.stmt] = []
                for attr in ("body", "orelse", "finalbody"):
                    val = getattr(node, attr, None)
                    if isinstance(val, list):
                        sub_stmts.extend(s for s in val if isinstance(s, ast.stmt))
                if sub_stmts:
                    _visit_body(sub_stmts, fn_stack)

    if isinstance(tree, ast.Module):
        _visit_body(tree.body, [])
    return signals


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

def _bytes_ref(repo: RepoSnapshot, file_path: Path, line_start: int, line_end: int) -> dict[str, Any]:
    return {
        "repo": repo.name,
        "commit_sha": repo.commit_sha,
        "path": str(file_path.relative_to(repo.root)),
        "line_start": line_start,
        "line_end": line_end,
    }


def _extract_swallowed_exceptions(
    repo: RepoSnapshot,
    parsed_files: dict[Path, ParsedPythonFile],
    tenant_id: str,
) -> AdapterResult:
    result = AdapterResult()

    for file_path, parsed in parsed_files.items():
        relative_path = str(file_path.relative_to(repo.root))

        if parsed.tree is None:
            # Unparseable file → coverage refusal row
            result.coverage.append(
                Coverage(
                    tenant_id=tenant_id,
                    predicate=_PREDICATE,
                    scope_ref={
                        "repo": repo.name,
                        "path": relative_path,
                        "language": "python",
                        "reason": "syntax_error",
                    },
                    state="partially_instrumented",
                    source_system=_SOURCE_SYSTEM,
                )
            )
            continue

        raw_signals = _collect_signals_from_file(repo, file_path, parsed.tree, tenant_id)

        # Apply per-symbol cap (3, lowest line first) — group by enclosing entity_id
        by_symbol: dict[str, list[_Signal]] = {}
        for sig in raw_signals:
            key = sig.enclosing_entity.entity_id
            by_symbol.setdefault(key, []).append(sig)

        for sym_signals in by_symbol.values():
            sym_signals.sort(key=lambda s: s.line)
            for sig in sym_signals[:_PER_SYMBOL_CAP]:
                subject = sig.enclosing_entity
                qualifier: dict[str, Any] = {
                    "risk_family": _RISK_FAMILY,
                    "exception_type": sig.exception_type,
                    "detail": f"broad_except_vacuous_handler:{sig.exception_type}",
                    "qualname": sig.enclosing_qualname,
                    "line": sig.line,
                }
                fact = Fact(
                    predicate=_PREDICATE,
                    subject_id=subject.entity_id,
                    object_id=subject.entity_id,
                    qualifier=qualifier,
                )
                evidence = Evidence(
                    target_type="fact",
                    target_id=fact.fact_id,
                    derivation_class="deterministic_static",
                    source_system=_SOURCE_SYSTEM,
                    source_ref={"extractor": _SOURCE_SYSTEM, "predicate": _PREDICATE},
                    bytes_ref=_bytes_ref(repo, file_path, sig.line, sig.end_line),
                    confidence=1.0,
                )
                result.entities.append(subject)
                result.support_facts.append(fact)
                result.evidence.append(evidence)

    return result


@dataclass(frozen=True)
class PythonSwallowedExceptionAdapter:
    capability = AdapterCapability(
        name="python-swallowed-exception",
        languages=("python",),
        file_kinds=("python",),
        framework_tags=(),
        produces_support_predicates=(_PREDICATE,),
        produces_entity_kinds=("CodeSymbol",),
        ontology_scope="implementation_support",
        derivation_classes=("deterministic_static",),
        source_system=_SOURCE_SYSTEM,
    )

    def applies_to(self, repo: RepoSnapshot, ctx: ExtractionContext) -> bool:
        return bool(repo.files_by_language.get("python", ()))

    def extract(self, repo: RepoSnapshot, ctx: ExtractionContext) -> AdapterResult:
        tenant_id = ctx.tenant_id if ctx is not None else resolve_tenant_id()
        parsed_files = parse_python_repo(repo, ctx)
        return _extract_swallowed_exceptions(repo, parsed_files, tenant_id)


PYTHON_SWALLOWED_EXCEPTION_ADAPTER = PythonSwallowedExceptionAdapter()
