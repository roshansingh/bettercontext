from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path

from source.kg.core.models import Coverage, Entity, Evidence, Fact, JsonObject, utc_now_iso
from source.kg.core.repo_source import RepoSnapshot, discover_repo
from source.kg.core.store import JsonlKgStore
from source.kg.core.tenant import resolve_tenant_id
from source.kg.build.event_channel_promotion import prune_uncorroborated_event_channels
from source.kg.extraction.framework import Adapter, ExtractionContext
from source.kg.extraction.framework.registry import validate_adapters
from source.kg.languages.dotnet.package_resolver import iter_dotnet_package_manifest_paths


def build_kg(
    repo_path: str | Path,
    output_dir: str | Path,
    strict_extractors: bool = False,
    tenant_id: str | None = None,
    repo_name: str | None = None,
) -> JsonObject:
    # Incompatible JSONL/manifest output changes must also bump
    # review_entry_resolution._SNAPSHOT_STORE_SCHEMA_VERSION.
    repo = discover_repo(repo_path, name=repo_name)
    resolved_tenant_id = resolve_tenant_id(tenant_id)
    build = extract_repo(repo, strict_extractors=strict_extractors, tenant_id=resolved_tenant_id)
    promotion = prune_uncorroborated_event_channels(build.entities, build.facts)
    entities = promotion.entities
    facts = promotion.facts
    coverage = build.coverage + promotion.coverage
    extractor_names = build.extractor_names
    manifest: JsonObject = {
        "repo_path": str(repo.root),
        "repo_name": repo.name,
        "owner": repo.owner,
        "tenant_id": resolved_tenant_id,
        "commit_sha": repo.commit_sha,
        "built_at": utc_now_iso(),
        "extractor": "+".join(extractor_names) if extractor_names else "none",
        "extractors": extractor_names,
        "extractor_errors": build.extractor_errors,
        "package_manifests": _package_manifest_fingerprints(repo.root),
        "counts": {
            "files_by_language": {
                language: len(paths) for language, paths in sorted(repo.files_by_language.items())
            },
            "unsupported_files_by_language": {
                language: len(paths) for language, paths in sorted(repo.unsupported_files_by_language.items())
            },
            "entities": len({entity.entity_id for entity in entities}),
            "facts": len({fact.fact_id for fact in facts}),
            "support_facts": len({fact.fact_id for fact in build.support_facts}),
            "evidence": len({row.evidence_id for row in build.evidence}),
            "coverage": len({row.coverage_id for row in coverage}),
        },
    }
    JsonlKgStore(output_dir).write(
        entities=entities,
        facts=facts,
        support_facts=build.support_facts,
        evidence=build.evidence,
        coverage=coverage,
        manifest=manifest,
    )
    return manifest


@dataclass
class RepoKgBuild:
    entities: list[Entity]
    facts: list[Fact]
    evidence: list[Evidence]
    coverage: list[Coverage]
    extractor_names: list[str]
    extractor_errors: list[JsonObject]
    support_facts: list[Fact] = field(default_factory=list)


def extract_repo(
    repo: RepoSnapshot,
    strict_extractors: bool = False,
    tenant_id: str | None = None,
) -> RepoKgBuild:
    from source.kg.extraction.framework import run_selected_adapters_with_support, select_applicable_adapter_specs
    from source.kg.file_formats import STATIC_CONFIG_ADAPTER, file_format_adapters
    from source.kg.languages import language_adapters

    ctx = ExtractionContext(tenant_id=resolve_tenant_id(tenant_id))
    adapters = _combined_adapters((STATIC_CONFIG_ADAPTER,), language_adapters(), file_format_adapters())
    selected = select_applicable_adapter_specs(repo, adapters, ctx=ctx)
    selected_adapters = [selection.adapter for selection in selected]
    adapter_run = run_selected_adapters_with_support(
        repo,
        selected_adapters,
        strict_extractors=strict_extractors,
        ctx=ctx,
    )
    extractor_names = list(dict.fromkeys(selection.capability.source_system for selection in selected))
    return RepoKgBuild(
        entities=adapter_run.entities,
        facts=adapter_run.facts,
        support_facts=adapter_run.support_facts,
        evidence=adapter_run.evidence,
        coverage=adapter_run.coverage,
        extractor_names=extractor_names,
        extractor_errors=adapter_run.errors,
    )


def _combined_adapters(*adapter_groups: tuple[Adapter, ...]) -> tuple[Adapter, ...]:
    adapters_by_name: dict[str, Adapter] = {}
    for group in adapter_groups:
        for adapter in group:
            name = adapter.capability.name
            if name in adapters_by_name:
                if adapters_by_name[name] is adapter:
                    continue
                raise ValueError(f"Duplicate adapter name: {name}")
            adapters_by_name[name] = adapter
    return validate_adapters(tuple(adapters_by_name.values()))


def _package_manifest_fingerprints(root: Path) -> list[JsonObject]:
    manifests: list[JsonObject] = []
    for filename in ("pyproject.toml", "package.json"):
        path = root / filename
        if not path.is_file():
            continue
        manifests.append({"path": filename, "sha256": sha256(path.read_bytes()).hexdigest()})
    if manifests:
        return manifests
    for path in iter_dotnet_package_manifest_paths(root):
        relative_path = path.relative_to(root)
        if not path.is_file():
            continue
        manifests.append({"path": relative_path.as_posix(), "sha256": sha256(path.read_bytes()).hexdigest()})
    return manifests
