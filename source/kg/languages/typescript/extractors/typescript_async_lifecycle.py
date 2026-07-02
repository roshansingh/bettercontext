"""Adapter: TS/JS async-lifecycle risk signals.

Consumes ``async_lifecycle_signals`` rows emitted by ts_parser.mjs and emits
``code_risk_signal`` support-facts on the enclosing CodeSymbol entity.

Two signal families:
  async_callback_in_iteration — forEach receiving an async callback.
  unawaited_async_call        — call to a same-file async fn whose result is
                                not awaited, returned, chained, or assigned.

Per-symbol cap of 3 signals (lowest line first) is enforced in the .mjs
collector; the Python adapter trusts that invariant.

Coverage rows (partially_instrumented) are emitted for any file whose
async_lifecycle_signals key is absent or whose parse_diagnostics are non-empty,
mirroring the proto_endpoints.py pattern.
"""
from __future__ import annotations

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
from source.kg.languages.typescript.extractors.parser_bridge import parse_typescript_repo

_SOURCE_SYSTEM = "typescript_async_lifecycle_v0"
_PREDICATE = "code_risk_signal"


def _language(file_path: Path) -> str:
    if file_path.suffix in {".ts", ".tsx", ".mts", ".cts"}:
        return "typescript"
    return "javascript"


def _module_name(repo: RepoSnapshot, file_path: Path) -> str:
    relative = file_path.relative_to(repo.root).with_suffix("")
    parts = [part for part in relative.parts if part != "index"]
    return ".".join(parts) or repo.name


def _symbol_entity(repo: RepoSnapshot, tenant_id: str, module_name: str, qualname: str) -> Entity:
    # symbol_kind is unknown here; use "function" as the most common case for
    # top-level async functions. The entity_id must match what the compiler API
    # extractor emits for the same symbol so facts can be correlated.
    return Entity(
        kind="CodeSymbol",
        identity={
            "tenant_id": tenant_id,
            "repo": repo.name,
            "module": module_name,
            "qualname": qualname,
            "symbol_kind": "function",
        },
        properties={},
    )


def _bytes_ref(repo: RepoSnapshot, relative_path: str, line: int) -> dict[str, Any]:
    return {
        "repo": repo.name,
        "commit_sha": repo.commit_sha,
        "path": relative_path,
        "line_start": line,
        "line_end": line,
    }


def _extract_signals(
    repo: RepoSnapshot,
    parsed_files: dict[str, Any],
    tenant_id: str,
) -> AdapterResult:
    result = AdapterResult()

    for file_path in repo.files_by_language.get("typescript", ()):
        relative_path = str(file_path.relative_to(repo.root))
        parsed_file = parsed_files.get(relative_path, {})
        if not isinstance(parsed_file, dict):
            continue

        signals = parsed_file.get("async_lifecycle_signals")
        diagnostics = parsed_file.get("parse_diagnostics", [])

        if signals is None or (isinstance(diagnostics, list) and diagnostics):
            # Unparseable or structurally unexpected — emit refusal coverage row
            result.coverage.append(
                Coverage(
                    tenant_id=tenant_id,
                    predicate=_PREDICATE,
                    scope_ref={
                        "repo": repo.name,
                        "path": relative_path,
                        "language": _language(file_path),
                        "reason": "parse_diagnostics" if diagnostics else "missing_signals_key",
                    },
                    state="partially_instrumented",
                    source_system=_SOURCE_SYSTEM,
                )
            )
            if signals is None:
                continue

        if not isinstance(signals, list):
            continue

        module_name = _module_name(repo, file_path)

        for row in signals:
            if not isinstance(row, dict):
                continue
            signal = row.get("signal")
            qualname = row.get("qualname")
            callee = row.get("callee")
            line = row.get("line")
            if not isinstance(signal, str) or not signal:
                continue
            if not isinstance(qualname, str) or not qualname:
                continue
            if not isinstance(callee, str):
                callee = ""
            if isinstance(line, bool) or not isinstance(line, int):
                continue

            subject = _symbol_entity(repo, tenant_id, module_name, qualname)
            qualifier: dict[str, Any] = {
                "risk_family": signal,
                "qualname": qualname,
                "callee": callee,
                "line": line,
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
                bytes_ref=_bytes_ref(repo, relative_path, line),
                confidence=1.0,
            )
            result.entities.append(subject)
            result.support_facts.append(fact)
            result.evidence.append(evidence)

    return result


@dataclass(frozen=True)
class TypeScriptAsyncLifecycleAdapter:
    capability = AdapterCapability(
        name="typescript-async-lifecycle",
        languages=("javascript", "typescript"),
        file_kinds=("javascript", "typescript"),
        framework_tags=(),
        produces_support_predicates=(_PREDICATE,),
        produces_entity_kinds=("CodeSymbol",),
        ontology_scope="implementation_support",
        derivation_classes=("deterministic_static",),
        source_system=_SOURCE_SYSTEM,
    )

    def applies_to(self, repo: RepoSnapshot, ctx: ExtractionContext) -> bool:
        return bool(repo.files_by_language.get("typescript", ()))

    def extract(self, repo: RepoSnapshot, ctx: ExtractionContext) -> AdapterResult:
        tenant_id = ctx.tenant_id if ctx is not None else resolve_tenant_id()
        parsed_files = parse_typescript_repo(repo, ctx)
        return _extract_signals(repo, parsed_files, tenant_id)


TYPESCRIPT_ASYNC_LIFECYCLE_ADAPTER = TypeScriptAsyncLifecycleAdapter()
