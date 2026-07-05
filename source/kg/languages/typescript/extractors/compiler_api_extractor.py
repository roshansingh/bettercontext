from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import json
import re

from source.kg.languages.typescript.normalization.imports import JsImportNormalizer, JsImportRef, NormalizedJsImport
from source.kg.core.models import Coverage, Entity, Evidence, Fact, JsonObject
from source.kg.core.repo_source import RepoSnapshot
from source.kg.core.tenant import resolve_tenant_id
from source.kg.extraction.framework.adapter import ExtractionContext
from source.kg.languages.typescript.extractors.parser_bridge import parse_typescript_repo


INTERNAL_IMPORT_CATEGORIES = frozenset(
    {
        "internal_module",
        "relative_internal_module",
        "resource_module",
        "relative_resource_module",
    }
)


@dataclass
class KgBuild:
    entities: list[Entity] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    coverage: list[Coverage] = field(default_factory=list)


@dataclass(frozen=True)
class SymbolDef:
    entity: Entity
    qualname: str
    symbol_kind: str
    line: int


class TypeScriptCompilerApiExtractor:
    source_system = "typescript_compiler_api_v0"

    def extract(self, repo: RepoSnapshot) -> KgBuild:
        return self.extract_with_context(repo, None)

    def extract_with_context(self, repo: RepoSnapshot, ctx: ExtractionContext | None) -> KgBuild:
        tenant_id = ctx.tenant_id if ctx is not None else resolve_tenant_id()
        build = KgBuild()
        repo_entity = self._repo_entity(repo, tenant_id)
        service_entity = self._service_entity(repo, tenant_id)
        build.entities.extend([repo_entity, service_entity])
        build.evidence.extend([self._repo_evidence(repo, repo_entity), self._service_evidence(repo, service_entity)])
        self._add_fact(build, "DEFINED_IN", service_entity, repo_entity, repo, self._package_json(repo), 1, 1)

        normalizer = JsImportNormalizer(repo)
        parsed_files = parse_typescript_repo(repo, ctx)
        for file_path in repo.files_by_language.get("typescript", ()):
            parsed_file = parsed_files.get(str(file_path.relative_to(repo.root)), {})
            self._extract_file(repo, file_path, repo_entity, service_entity, normalizer, parsed_file, build, ctx, tenant_id)
            for diagnostic in parsed_file.get("parse_diagnostics", []):
                build.coverage.append(
                    Coverage(
                        tenant_id=tenant_id,
                        predicate="PARSES",
                        scope_ref={
                            "repo": repo.name,
                            "language": self._language(file_path),
                            "path": str(file_path.relative_to(repo.root)),
                            "line": diagnostic.get("line", 1),
                            "message": diagnostic.get("message", "parse diagnostic"),
                        },
                        state="uninstrumented",
                        source_system=self.source_system,
                    )
                )

        build.coverage.append(
            Coverage(
                tenant_id=tenant_id,
                predicate="PARSES",
                scope_ref={"repo": repo.name, "language": "typescript/javascript", "path_prefix": "."},
                state="instrumented",
                source_system=self.source_system,
            )
        )
        return build

    def _extract_file(
        self,
        repo: RepoSnapshot,
        file_path: Path,
        repo_entity: Entity,
        service_entity: Entity,
        normalizer: JsImportNormalizer,
        parsed_file: JsonObject,
        build: KgBuild,
        ctx: ExtractionContext | None,
        tenant_id: str,
    ) -> None:
        source = file_path.read_text(encoding="utf-8", errors="replace")
        lines = source.splitlines()
        module_name = self._module_name(repo, file_path)
        module_entity = Entity(
            kind="CodeModule",
            identity={"tenant_id": tenant_id, "repo": repo.name, "module": module_name},
            properties={"path": str(file_path.relative_to(repo.root)), "language": self._language(file_path)},
        )
        build.entities.append(module_entity)
        build.evidence.append(self._entity_evidence(repo, module_entity, file_path, 1, max(1, len(lines))))
        self._add_fact(build, "DEFINED_IN", module_entity, repo_entity, repo, file_path, 1, 1)
        self._add_fact(build, "IMPLEMENTS", module_entity, service_entity, repo, file_path, 1, 1)

        relative_file_path = str(file_path.relative_to(repo.root))
        imports = [
            normalizer.normalize(self._import_ref(row), module_name, relative_file_path)
            for row in parsed_file.get("imports", [])
        ]
        if ctx is not None:
            ctx.import_roots_by_language.setdefault("javascript", set()).update(import_ref.import_root for import_ref in imports)
        imports_by_local = self._imports_by_local(imports)
        for import_ref in imports:
            dependency_entity = self._dependency_entity(repo, import_ref, tenant_id)
            build.entities.append(dependency_entity)
            build.evidence.append(self._entity_evidence(repo, dependency_entity, file_path, import_ref.line, import_ref.line))
            self._add_fact(
                build,
                "IMPORTS",
                module_entity,
                dependency_entity,
                repo,
                file_path,
                import_ref.line,
                import_ref.line,
                qualifier=self._import_qualifier(import_ref),
            )

        symbols = [
            self._symbol_from_row(repo, file_path, module_name, row, tenant_id)
            for row in parsed_file.get("symbols", [])
        ]
        symbols_by_short_name = {symbol.qualname.rsplit(".", 1)[-1]: symbol for symbol in symbols}
        for symbol in symbols:
            build.entities.append(symbol.entity)
            build.evidence.append(self._entity_evidence(repo, symbol.entity, file_path, symbol.line, symbol.line))
            self._add_fact(build, "DEFINED_IN", symbol.entity, module_entity, repo, file_path, symbol.line, symbol.line)

        for call in parsed_file.get("calls", []):
            caller = symbols_by_short_name.get(str(call.get("caller", "")))
            if not caller:
                continue
            call_name = str(call.get("name", ""))
            line_number = int(call.get("line") or caller.line)
            short_name = call_name.rsplit(".", 1)[-1]
            if short_name in symbols_by_short_name and symbols_by_short_name[short_name] != caller:
                self._add_fact(
                    build,
                    "CALLS",
                    caller.entity,
                    symbols_by_short_name[short_name].entity,
                    repo,
                    file_path,
                    line_number,
                    line_number,
                )
                continue
            root = call_name.split(".", 1)[0]
            import_ref = imports_by_local.get(root)
            if import_ref:
                self._add_fact(
                    build,
                    "CALLS",
                    caller.entity,
                    self._dependency_entity(repo, import_ref, tenant_id),
                    repo,
                    file_path,
                    line_number,
                    line_number,
                    qualifier={"call": call_name},
                )

    def _import_ref(self, row: JsonObject) -> JsImportRef:
        return JsImportRef(
            raw_target=str(row.get("raw_target", "")),
            line=int(row.get("line") or 1),
            imported_names=tuple(str(name) for name in row.get("imported_names", [])),
            local_names=tuple(str(name) for name in row.get("local_names", [])),
            is_type_only=bool(row.get("is_type_only", False)),
        )

    def _symbol_from_row(
        self,
        repo: RepoSnapshot,
        file_path: Path,
        module_name: str,
        row: JsonObject,
        tenant_id: str,
    ) -> SymbolDef:
        name = str(row.get("name", "anonymous"))
        kind = str(row.get("kind", "value"))
        line = int(row.get("line") or 1)
        entity = Entity(
            kind="CodeSymbol",
            identity={
                "tenant_id": tenant_id,
                "repo": repo.name,
                "module": module_name,
                "qualname": name,
                "symbol_kind": kind,
            },
            properties={
                "path": str(file_path.relative_to(repo.root)),
                "line": line,
                "end_line": int(row.get("end_line") or line),
                "language": self._language(file_path),
                "is_async": bool(row.get("is_async", False)),
                "returns_promise_type": bool(row.get("returns_promise_type", False)),
            },
        )
        return SymbolDef(entity=entity, qualname=name, symbol_kind=kind, line=line)

    def _imports_by_local(self, imports: list[NormalizedJsImport]) -> dict[str, NormalizedJsImport]:
        by_local: dict[str, NormalizedJsImport] = {}
        for import_ref in imports:
            for local_name in import_ref.local_names:
                by_local.setdefault(local_name, import_ref)
        return by_local

    def _dependency_entity(self, repo: RepoSnapshot, import_ref: NormalizedJsImport, tenant_id: str) -> Entity:
        if import_ref.category in INTERNAL_IMPORT_CATEGORIES:
            return Entity(
                kind="CodeModule",
                identity={"tenant_id": tenant_id, "repo": repo.name, "module": import_ref.target_name},
                properties={"dependency_category": import_ref.category},
            )
        return Entity(
            kind="ExternalPackage",
            identity={"tenant_id": tenant_id, "repo": repo.name, "name": import_ref.target_name},
            properties={
                "category": import_ref.category,
                "import_root": import_ref.import_root,
                "distribution_name": import_ref.distribution_name,
            },
        )

    def _import_qualifier(self, import_ref: NormalizedJsImport) -> JsonObject:
        return {
            "raw_import": import_ref.raw_import,
            "import_root": import_ref.import_root,
            "distribution_name": import_ref.distribution_name,
            "category": import_ref.category,
            "module_name": import_ref.module_name,
            "imported_names": list(import_ref.imported_names),
            "local_names": list(import_ref.local_names),
            "is_type_only": import_ref.is_type_only,
        }

    def _add_fact(
        self,
        build: KgBuild,
        predicate: str,
        subject: Entity,
        object_: Entity,
        repo: RepoSnapshot,
        file_path: Path,
        line_start: int,
        line_end: int,
        qualifier: JsonObject | None = None,
    ) -> None:
        fact = Fact(predicate=predicate, subject_id=subject.entity_id, object_id=object_.entity_id, qualifier=qualifier or {})
        build.facts.append(fact)
        build.evidence.append(
            Evidence(
                target_type="fact",
                target_id=fact.fact_id,
                derivation_class="deterministic_static",
                source_system=self.source_system,
                source_ref={"extractor": self.source_system, "predicate": predicate},
                bytes_ref=self._bytes_ref(repo, file_path, line_start, line_end),
                confidence=1.0,
            )
        )

    def _repo_entity(self, repo: RepoSnapshot, tenant_id: str) -> Entity:
        return Entity(
            kind="Repo",
            identity={"tenant_id": tenant_id, "host": "local", "owner": repo.owner, "name": repo.name},
            properties={"path": str(repo.root), "commit_sha": repo.commit_sha},
        )

    def _service_entity(self, repo: RepoSnapshot, tenant_id: str) -> Entity:
        return Entity(
            kind="Service",
            identity={
                "tenant_id": tenant_id,
                "namespace": "default",
                "repo": repo.name,
                "slug": self._service_slug(repo),
            },
            properties={"repo": repo.name},
        )

    def _repo_evidence(self, repo: RepoSnapshot, entity: Entity) -> Evidence:
        return Evidence(
            target_type="entity",
            target_id=entity.entity_id,
            derivation_class="authoritative_declared",
            source_system="git",
            source_ref={"repo_path": str(repo.root), "commit_sha": repo.commit_sha},
            confidence=1.0,
        )

    def _service_evidence(self, repo: RepoSnapshot, entity: Entity) -> Evidence:
        package_json = self._package_json(repo)
        return Evidence(
            target_type="entity",
            target_id=entity.entity_id,
            derivation_class="authoritative_declared",
            source_system="package_json",
            source_ref={"package_name": self._package_name(repo)},
            bytes_ref=self._bytes_ref(repo, package_json, 1, 1) if package_json.exists() else None,
            confidence=1.0,
        )

    def _entity_evidence(self, repo: RepoSnapshot, entity: Entity, file_path: Path, line_start: int, line_end: int) -> Evidence:
        return Evidence(
            target_type="entity",
            target_id=entity.entity_id,
            derivation_class="deterministic_static",
            source_system=self.source_system,
            source_ref={"extractor": self.source_system, "entity_kind": entity.kind},
            bytes_ref=self._bytes_ref(repo, file_path, line_start, line_end),
            confidence=1.0,
        )

    def _bytes_ref(self, repo: RepoSnapshot, file_path: Path, line_start: int, line_end: int) -> JsonObject:
        return {
            "repo": repo.name,
            "commit_sha": repo.commit_sha,
            "path": str(file_path.relative_to(repo.root)),
            "line_start": line_start,
            "line_end": line_end,
        }

    def _module_name(self, repo: RepoSnapshot, file_path: Path) -> str:
        relative = file_path.relative_to(repo.root).with_suffix("")
        parts = [part for part in relative.parts if part != "index"]
        return ".".join(parts) or repo.name

    def _language(self, file_path: Path) -> str:
        if file_path.suffix in {".ts", ".tsx", ".mts", ".cts"}:
            return "typescript"
        return "javascript"

    def _package_json(self, repo: RepoSnapshot) -> Path:
        return repo.root / "package.json"

    def _package_name(self, repo: RepoSnapshot) -> str:
        package_json = self._package_json(repo)
        if not package_json.exists():
            return repo.name
        try:
            data = json.loads(package_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return repo.name
        return str(data.get("name") or repo.name)

    def _service_slug(self, repo: RepoSnapshot) -> str:
        return re.sub(r"[^a-z0-9]+", "-", self._package_name(repo).lower()).strip("-") or repo.name
