from __future__ import annotations

import json
from json import JSONDecodeError
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from source.kg.core.display import display_entity
from source.kg.core.models import JsonObject
from source.kg.core.store import read_jsonl
from source.kg.file_formats._shared.common import endpoint_path_shape_matches_prefix, normalize_endpoint_path_shape

from . import aggregations, path_search
from .call_site import call_site_from_qualifier
from .reverse_impact import reverse_impact_packet


def _repo_anchor_matches(candidate: object, requested: object) -> bool:
    candidate_key = str(candidate or "").strip().lower()
    requested_key = str(requested or "").strip().lower()
    if not candidate_key or not requested_key:
        return False
    if candidate_key == requested_key:
        return True
    if "/" in candidate_key and "/" in requested_key:
        return False
    return candidate_key.rsplit("/", 1)[-1] == requested_key.rsplit("/", 1)[-1]


def _repo_request_is_owner_qualified(requested: object) -> bool:
    return "/" in str(requested or "").strip()


def _repo_identity_matches(value: object, requested: object) -> bool:
    if not isinstance(value, dict):
        return False
    name = value.get("name")
    owner = value.get("owner")
    if isinstance(owner, str) and isinstance(name, str):
        if _repo_request_is_owner_qualified(requested):
            return _repo_anchor_matches(f"{owner}/{name}", requested)
        return _repo_anchor_matches(name, requested)
    if _repo_anchor_matches(name, requested):
        return True
    return False


def _event_linkage_status(fact: JsonObject, channel: JsonObject) -> str:
    if (
        fact.get("predicate") in {"CONSUMES_EVENT", "PRODUCES_EVENT"}
        and _canonical_status(fact) == "canonical"
        and _canonical_status(channel) == "canonical"
    ):
        return "known_linked"
    return "candidate_or_unlinked"


def _canonical_status(row: JsonObject) -> str:
    value = row.get("canonical_status", "canonical")
    return value if isinstance(value, str) and value else "canonical"


def _event_channel_result_sort_key(row: JsonObject) -> tuple[str, str, str]:
    qualifier = row.get("qualifier") if isinstance(row.get("qualifier"), dict) else {}
    return (
        str(qualifier.get("path", "")),
        str(row.get("predicate", "")),
        str(row.get("object", "")),
    )


class KgSnapshot:
    """Read-only KG snapshot plus query helpers shared by query submodules.

    Underscore-prefixed symbol/fact helpers below are internal to the query
    layer, not public CLI/API contracts. Query modules such as
    reverse_impact may reuse them to keep symbol resolution, public row
    formatting, import-consumer leads, and disambiguation behavior consistent.
    """

    def __init__(self, snapshot_dir: str | Path) -> None:
        root = Path(snapshot_dir).expanduser().resolve()
        self.root: Path = root
        self.entities = read_jsonl(root / "entities.jsonl")
        self.facts = read_jsonl(root / "facts.jsonl")
        support_facts_path = root / "support_facts.jsonl"
        self.support_facts = read_jsonl(support_facts_path) if support_facts_path.exists() else []
        self.evidence = read_jsonl(root / "evidence.jsonl")
        self.coverage = read_jsonl(root / "coverage.jsonl")
        self.manifest: JsonObject = _read_manifest(root / "manifest.json")
        self.entities_by_id = {entity["entity_id"]: entity for entity in self.entities}
        self.evidence_by_target = defaultdict(list)
        for row in self.evidence:
            self.evidence_by_target[row["target_id"]].append(row)
        self._reverse_impact_incoming_call_facts_cache: dict[str, list[JsonObject]] | None = None
        self._reverse_impact_class_symbol_index_cache: dict[tuple[object, object, object, object], JsonObject] | None = None
        self._reverse_impact_init_symbol_index_cache: dict[tuple[object, object, object, object], JsonObject] | None = None

    def summary(self) -> JsonObject:
        by_kind: dict[str, int] = defaultdict(int)
        by_predicate: dict[str, int] = defaultdict(int)
        for entity in self.entities:
            by_kind[entity["kind"]] += 1
        for fact in self.facts:
            by_predicate[fact["predicate"]] += 1
        return {
            "entity_kinds": dict(sorted(by_kind.items())),
            "predicates": dict(sorted(by_predicate.items())),
            "coverage": self.coverage,
        }

    def find_callers(
        self,
        symbol_query: str,
        limit: int = 25,
        path: str | None = None,
        line: int | None = None,
        include_all: bool = False,
    ) -> JsonObject:
        resolution = self._resolve_symbol(symbol_query, limit=limit, path=path, line=line, allow_fuzzy=False)
        if resolution["status"] == "not_found":
            return {"status": "not_found", "target": resolution, "callers": []}
        if resolution["status"] == "ambiguous" and not include_all:
            return {
                "status": "ambiguous",
                "target": resolution,
                "callers": [],
                **self._symbol_disambiguation_payload(resolution, result_kind="callers"),
            }

        if include_all:
            target_ids = {candidate["symbol_id"] for candidate in resolution["candidates"]}
        else:
            target_ids = {resolution["resolved_symbol"]["symbol_id"]}
        results: list[JsonObject] = []
        for fact in self.facts:
            if fact["predicate"] != "CALLS" or fact["object_id"] not in target_ids:
                continue
            caller = self.entities_by_id.get(fact["subject_id"])
            callee = self.entities_by_id.get(fact["object_id"])
            if caller and callee:
                results.append(self._fact_result(fact, caller, callee))
                if len(results) >= limit:
                    break
        return {
            "status": "found" if results else "not_found",
            "target": resolution,
            "caller_count": len(results),
            "callers": results,
            "import_consumer_leads": (
                self._symbol_import_consumer_leads(resolution, limit=limit)
                if not results
                else {
                    "status": "not_applicable",
                    "reason": "proven CALLS callers were found; import leads are only returned on caller misses",
                    "lead_count": 0,
                    "returned_count": 0,
                    "leads": [],
                }
            ),
        }

    def find_callees(
        self,
        symbol_query: str,
        limit: int = 25,
        path: str | None = None,
        line: int | None = None,
        include_all: bool = False,
    ) -> JsonObject:
        resolution = self._resolve_symbol(symbol_query, limit=limit, path=path, line=line, allow_fuzzy=False)
        if resolution["status"] == "not_found":
            return {"status": "not_found", "source": resolution, "callees": []}
        if resolution["status"] == "ambiguous" and not include_all:
            return {
                "status": "ambiguous",
                "source": resolution,
                "callees": [],
                **self._symbol_disambiguation_payload(resolution, result_kind="callees"),
            }

        if include_all:
            source_ids = {candidate["symbol_id"] for candidate in resolution["candidates"]}
        else:
            source_ids = {resolution["resolved_symbol"]["symbol_id"]}
        results: list[JsonObject] = []
        for fact in self.facts:
            if fact["predicate"] != "CALLS" or fact["subject_id"] not in source_ids:
                continue
            caller = self.entities_by_id.get(fact["subject_id"])
            callee = self.entities_by_id.get(fact["object_id"])
            if caller and callee:
                results.append(self._fact_result(fact, caller, callee))
                if len(results) >= limit:
                    break
        return {
            "status": "found" if results else "not_found",
            "source": resolution,
            "callee_count": len(results),
            "callees": results,
        }

    def blast_radius(
        self,
        symbol_query: str,
        depth: int = 2,
        limit: int = 25,
        path: str | None = None,
        line: int | None = None,
        include_all: bool = False,
    ) -> JsonObject:
        resolution = self._resolve_symbol(symbol_query, limit=limit, path=path, line=line, allow_fuzzy=False)
        if resolution["status"] == "not_found":
            return {"status": "not_found", "source": resolution, "edges": []}
        if resolution["status"] == "ambiguous" and not include_all:
            return {
                "status": "ambiguous",
                "source": resolution,
                "edges": [],
                **self._symbol_disambiguation_payload(resolution, result_kind="downstream impact"),
            }

        if include_all:
            root_ids = {candidate["symbol_id"] for candidate in resolution["candidates"]}
        else:
            root_ids = {resolution["resolved_symbol"]["symbol_id"]}
        graph: dict[str, list[JsonObject]] = defaultdict(list)
        for fact in self.facts:
            if fact["predicate"] == "CALLS":
                graph[fact["subject_id"]].append(fact)

        seen = set(root_ids)
        queue = deque((root_id, 0) for root_id in root_ids)
        results: list[JsonObject] = []
        while queue:
            current_id, current_depth = queue.popleft()
            if current_depth >= depth:
                continue
            for fact in graph.get(current_id, []):
                callee = self.entities_by_id.get(fact["object_id"])
                caller = self.entities_by_id.get(fact["subject_id"])
                if not caller or not callee:
                    continue
                results.append(self._fact_result(fact, caller, callee, depth=current_depth + 1))
                if len(results) >= limit:
                    return {
                        "status": "found",
                        "source": resolution,
                        "depth": depth,
                        "edge_count": len(results),
                        "edges": results,
                    }
                if callee["entity_id"] not in seen:
                    seen.add(callee["entity_id"])
                    queue.append((callee["entity_id"], current_depth + 1))
        return {
            "status": "found" if results else "not_found",
            "source": resolution,
            "depth": depth,
            "edge_count": len(results),
            "edges": results,
        }

    def reverse_impact(
        self,
        symbol_query: str,
        depth: int = 3,
        limit: int = 25,
        path: str | None = None,
        line: int | None = None,
        include_all: bool = False,
    ) -> JsonObject:
        return reverse_impact_packet(
            self,
            symbol_query,
            depth=depth,
            limit=limit,
            path=path,
            line=line,
            include_all=include_all,
        )

    def modules_importing(self, package_name: str, limit: int = 25) -> list[JsonObject]:
        results: list[JsonObject] = []
        for fact in self.facts:
            if fact["predicate"] != "IMPORTS":
                continue
            package = self.entities_by_id.get(fact["object_id"])
            module = self.entities_by_id.get(fact["subject_id"])
            if not package or not module:
                continue
            if self._import_matches(fact, package, package_name):
                results.append(self._fact_result(fact, module, package))
                if len(results) >= limit:
                    return results
        return results

    def import_matches(self, fact: JsonObject, package: JsonObject, package_name: str) -> bool:
        return self._import_matches(fact, package, package_name)

    def top_dependencies(self, limit: int = 25, exclude_stdlib: bool = True, exclude_unknown: bool = True) -> list[JsonObject]:
        counts: dict[str, JsonObject] = {}
        for fact in self.facts:
            if fact["predicate"] != "IMPORTS":
                continue
            qualifier = fact.get("qualifier", {})
            category = qualifier.get("category")
            if exclude_stdlib and category in {"stdlib", "node_builtin"}:
                continue
            if exclude_unknown and category == "unknown":
                continue
            package = self.entities_by_id.get(fact["object_id"])
            if not package or package["kind"] != "ExternalPackage":
                continue
            name = str(package["identity"].get("name"))
            row = counts.setdefault(
                name,
                {
                    "name": name,
                    "category": category,
                    "import_root": qualifier.get("import_root"),
                    "distribution_name": qualifier.get("distribution_name"),
                    "importer_count": 0,
                    "sample_evidence": [],
                },
            )
            row["importer_count"] += 1
            if len(row["sample_evidence"]) < 3:
                row["sample_evidence"].extend(self.evidence_by_target.get(fact["fact_id"], [])[:1])
        return sorted(counts.values(), key=lambda row: (-row["importer_count"], row["name"]))[:limit]

    def dependency_info(self, package_name: str) -> list[JsonObject]:
        seen: dict[str, JsonObject] = {}
        for fact in self.facts:
            if fact["predicate"] != "IMPORTS":
                continue
            package = self.entities_by_id.get(fact["object_id"])
            if not package or not self._import_matches(fact, package, package_name):
                continue
            qualifier = fact.get("qualifier", {})
            name = str(package["identity"].get("name"))
            seen[name] = {
                "name": name,
                "kind": package["kind"],
                "category": qualifier.get("category"),
                "import_root": qualifier.get("import_root"),
                "distribution_name": qualifier.get("distribution_name"),
            }
        return sorted(seen.values(), key=lambda row: row["name"])

    def who_imports(
        self,
        target: str,
        group_prefix_depth: int = 2,
        no_grouping: bool = False,
        limit: int = 25,
    ) -> JsonObject:
        return aggregations.who_imports(
            target=target,
            entities_by_id=self.entities_by_id,
            facts=self.facts,
            evidence_by_target=self.evidence_by_target,
            group_prefix_depth=group_prefix_depth,
            no_grouping=no_grouping,
            limit=limit,
        )

    def top_internal_dependencies(self, relative_only: bool = False, limit: int = 25) -> JsonObject:
        return aggregations.top_internal_dependencies(
            entities_by_id=self.entities_by_id,
            facts=self.facts,
            evidence_by_target=self.evidence_by_target,
            relative_only=relative_only,
            limit=limit,
        )

    def top_fan_in_symbols(self, include_external: bool = False, limit: int = 25) -> JsonObject:
        return aggregations.top_fan_in_symbols(
            entities_by_id=self.entities_by_id,
            facts=self.facts,
            evidence_by_target=self.evidence_by_target,
            include_external=include_external,
            limit=limit,
        )

    def modules_importing_both(
        self,
        left: str,
        right: str,
        category_filter: set[str] | None = None,
        limit: int = 25,
    ) -> JsonObject:
        return aggregations.modules_importing_both(
            left=left,
            right=right,
            entities_by_id=self.entities_by_id,
            facts=self.facts,
            evidence_by_target=self.evidence_by_target,
            category_filter=category_filter,
            limit=limit,
        )

    def dependency_path(
        self,
        source_query: str,
        target_query: str,
        path: str | None = None,
        line: int | None = None,
        include_all: bool = False,
        max_depth: int = 4,
        limit: int = 5,
    ) -> JsonObject:
        max_depth = min(max(1, max_depth), 6)
        limit = min(max(1, limit), 25)
        source_resolution = self._resolve_symbol(source_query, limit=limit, path=path, line=line, allow_fuzzy=False)
        if source_resolution["status"] == "not_found":
            return self._dependency_path_response(
                "not_found",
                source_resolution,
                None,
                max_depth=max_depth,
                limit=limit,
                paths=[],
            )
        if source_resolution["status"] == "ambiguous" and not include_all:
            return self._dependency_path_response(
                "ambiguous",
                source_resolution,
                None,
                max_depth=max_depth,
                limit=limit,
                paths=[],
            )

        if include_all:
            source_ids = {candidate["symbol_id"] for candidate in source_resolution["candidates"]}
        else:
            source_ids = {source_resolution["resolved_symbol"]["symbol_id"]}

        target_resolution = aggregations.resolve_import_target(target_query, self.facts, self.entities_by_id)
        if target_resolution["status"] != "resolved":
            return self._dependency_path_response(
                target_resolution["status"],
                source_resolution,
                target_resolution,
                max_depth=max_depth,
                limit=limit,
                paths=[],
            )

        paths = path_search.find_dependency_paths(
            self,
            source_ids,
            target_resolution,
            max_depth=max_depth,
            limit=limit,
        )
        return self._dependency_path_response(
            "resolved" if paths else "empty",
            source_resolution,
            target_resolution,
            max_depth=max_depth,
            limit=limit,
            paths=[path_search.path_to_dict(self, result_path) for result_path in paths],
        )

    def cross_repo_links(self, limit: int = 25) -> JsonObject:
        limit = self._clamp_limit(limit)
        links = []
        for fact in self.facts:
            if fact["predicate"] not in {"RESOLVES_TO_REPO", "RESOLVES_TO_SERVICE"}:
                continue
            package = self.entities_by_id.get(fact["subject_id"])
            target = self.entities_by_id.get(fact["object_id"])
            if not package or not target:
                continue
            links.append(self._fact_result(fact, package, target))
        links = sorted(
            links,
            key=lambda row: (
                str(row.get("qualifier", {}).get("consumer_repo", "")),
                str(row.get("qualifier", {}).get("package_name", "")),
                row["predicate"],
            ),
        )
        returned = links[:limit]
        return {
            "status": "found" if links else "not_found",
            "link_count": len(links),
            "returned_count": len(returned),
            "links": returned,
        }

    def repo_dependencies(self, repo: str, limit: int = 25) -> JsonObject:
        limit = self._clamp_limit(limit)
        links = []
        for fact in self.facts:
            if fact["predicate"] != "RESOLVES_TO_REPO":
                continue
            package = self.entities_by_id.get(fact["subject_id"])
            target_repo = self.entities_by_id.get(fact["object_id"])
            if not package or not target_repo:
                continue
            qualifier = fact.get("qualifier", {})
            if not isinstance(qualifier, dict):
                continue
            identity_matches = _repo_identity_matches(qualifier.get("consumer_repo_identity"), repo)
            consumer_identities = qualifier.get("consumer_repo_identities")
            if not identity_matches and isinstance(consumer_identities, list):
                identity_matches = any(_repo_identity_matches(row, repo) for row in consumer_identities)
            has_identity = isinstance(qualifier.get("consumer_repo_identity"), dict) or (
                isinstance(consumer_identities, list) and any(isinstance(row, dict) for row in consumer_identities)
            )
            anchor_matches = _repo_anchor_matches(qualifier.get("consumer_repo"), repo)
            if _repo_request_is_owner_qualified(repo) and has_identity:
                if not identity_matches:
                    continue
            elif not anchor_matches and not identity_matches:
                continue
            links.append(self._fact_result(fact, package, target_repo))
        links = sorted(
            links,
            key=lambda row: (
                str(row.get("object")),
                str(row.get("qualifier", {}).get("package_name", "")),
            ),
        )
        returned = links[:limit]
        return {
            "status": "found" if links else "not_found",
            "repo": repo,
            "dependency_count": len(links),
            "returned_count": len(returned),
            "dependencies": returned,
        }

    def domain_references(self, domain_query: str, limit: int = 25) -> JsonObject:
        limit = self._clamp_limit(limit, max_limit=100)
        domains = [
            entity
            for entity in self.entities
            if entity["kind"] == "Domain" and domain_query.lower() in str(entity["identity"].get("name", "")).lower()
        ]
        domain_ids = {entity["entity_id"] for entity in domains}
        references = []
        env_var_ids = set()
        for fact in self.facts:
            if fact["predicate"] not in {"REFERENCES_DOMAIN", "ROUTES_DOMAIN_TO_DEPLOY"}:
                continue
            if fact["subject_id"] not in domain_ids and fact["object_id"] not in domain_ids:
                continue
            subject = self.entities_by_id.get(fact["subject_id"])
            object_ = self.entities_by_id.get(fact["object_id"])
            if not subject or not object_:
                continue
            references.append(self._fact_result(fact, subject, object_))
            if fact["predicate"] == "REFERENCES_DOMAIN" and subject.get("kind") == "EnvVar":
                env_var_ids.add(subject["entity_id"])
        for fact in self.facts:
            if fact["predicate"] != "REFERENCES_ENV_VAR" or fact["object_id"] not in env_var_ids:
                continue
            qualifier = fact.get("qualifier", {})
            if not isinstance(qualifier, dict) or qualifier.get("reference_kind") not in {
                "code_access",
                "endpoint_env_host",
            }:
                continue
            subject = self.entities_by_id.get(fact["subject_id"])
            object_ = self.entities_by_id.get(fact["object_id"])
            if subject and object_:
                references.append(self._fact_result(fact, subject, object_))
        references = _dedupe_fact_results(references)
        references = sorted(references, key=lambda row: (str(row.get("object")), str(row.get("subject"))))
        returned = references[:limit]
        return {
            "status": "found" if references else "not_found",
            "query": domain_query,
            "domain_count": len(domain_ids),
            "reference_count": len(references),
            "returned_count": len(returned),
            "references": returned,
        }

    def endpoints(self, path_query: str | None = None, limit: int = 25) -> JsonObject:
        limit = self._clamp_limit(limit, max_limit=100)
        rows = []
        for fact in self.facts:
            if fact["predicate"] not in {"EXPOSES_ENDPOINT", "CALLS_ENDPOINT", "DOCUMENTS_ENDPOINT"}:
                continue
            subject = self.entities_by_id.get(fact["subject_id"])
            endpoint = self.entities_by_id.get(fact["object_id"])
            if not subject or not endpoint or endpoint.get("kind") != "Endpoint":
                continue
            path = str(endpoint.get("identity", {}).get("path", ""))
            if path_query:
                query = str(path_query)
                path_haystacks = {path.lower(), normalize_endpoint_path_shape(path).lower()}
                query_needles = {query.lower(), normalize_endpoint_path_shape(query).lower()}
                if not any(needle in haystack for needle in query_needles for haystack in path_haystacks):
                    continue
            rows.append(self._fact_result(fact, subject, endpoint))
        rows = sorted(
            rows,
            key=lambda row: (
                str(row["qualifier"].get("path", "")),
                str(row.get("predicate", "")),
                str(row.get("object", "")),
            ),
        )
        returned = rows[:limit]
        return {
            "status": "found" if rows else "not_found",
            "query": path_query,
            "endpoint_fact_count": len(rows),
            "returned_count": len(returned),
            "endpoints": returned,
        }

    def reconcile_endpoints(
        self,
        docs_scope: list[str] | tuple[str, ...] = (),
        backend_scope: list[str] | tuple[str, ...] = (),
        client_scope: list[str] | tuple[str, ...] = (),
        path_prefix: str | None = None,
    ) -> JsonObject:
        from source.kg.product.contract_reconciliation import ContractReconciliationSpec, ContractSide, reconcile_contract

        docs_vs_backend = reconcile_contract(
            self,
            ContractReconciliationSpec(
                name="docs_vs_backend_endpoints",
                identity_key="endpoint_path",
                left=ContractSide(
                    name="documented",
                    predicates=("DOCUMENTS_ENDPOINT",),
                    repos=tuple(docs_scope),
                    path_prefix=path_prefix,
                ),
                right=ContractSide(
                    name="implemented",
                    predicates=("EXPOSES_ENDPOINT",),
                    repos=tuple(backend_scope),
                    path_prefix=path_prefix,
                ),
            ),
        )
        docs_vs_client = reconcile_contract(
            self,
            ContractReconciliationSpec(
                name="docs_vs_client_endpoints",
                identity_key="endpoint_path",
                left=ContractSide(
                    name="documented",
                    predicates=("DOCUMENTS_ENDPOINT",),
                    repos=tuple(docs_scope),
                    path_prefix=path_prefix,
                ),
                right=ContractSide(
                    name="called",
                    predicates=("CALLS_ENDPOINT",),
                    repos=tuple(client_scope),
                    path_prefix=path_prefix,
                ),
            ),
        )
        return {
            "status": "found" if docs_vs_backend["status"] == "found" or docs_vs_client["status"] == "found" else "not_found",
            "docs_scope": list(docs_scope),
            "backend_scope": list(backend_scope),
            "client_scope": list(client_scope),
            "path_prefix": path_prefix,
            "documented_AND_implemented": docs_vs_backend["matched"],
            "documented_NOT_implemented": docs_vs_backend["left_only"],
            "implemented_NOT_documented": docs_vs_backend["right_only"],
            "documented_AND_called": docs_vs_client["matched"],
            "documented_NOT_called": docs_vs_client["left_only"],
            "coverage_warnings": self._endpoint_reconciliation_warnings(docs_scope, backend_scope, client_scope, path_prefix),
        }

    def event_channels(self, channel_query: str | None = None, limit: int = 25) -> JsonObject:
        limit = self._clamp_limit(limit, max_limit=100)
        known_rows = []
        candidate_rows = []
        for fact in self.facts:
            if fact["predicate"] not in {"REFERENCES_EVENT_CHANNEL", "CONSUMES_EVENT", "PRODUCES_EVENT"}:
                continue
            subject = self.entities_by_id.get(fact["subject_id"])
            channel = self.entities_by_id.get(fact["object_id"])
            if not subject or not channel or channel.get("kind") != "EventChannel":
                continue
            identity = channel.get("identity", {})
            name = str(identity.get("channel_address") or identity.get("name") or "")
            if channel_query and channel_query.lower() not in name.lower():
                continue
            linkage_status = _event_linkage_status(fact, channel)
            row = self._fact_result(
                fact,
                subject,
                channel,
                canonical_status=_canonical_status(fact),
                channel_canonical_status=_canonical_status(channel),
                linkage_status=linkage_status,
            )
            if linkage_status == "known_linked":
                known_rows.append(row)
            else:
                candidate_rows.append(row)
        known_rows = sorted(known_rows, key=_event_channel_result_sort_key)
        candidate_rows = sorted(candidate_rows, key=_event_channel_result_sort_key)
        returned = known_rows[:limit]
        remaining_limit = max(0, limit - len(returned))
        returned_candidates = candidate_rows[:remaining_limit]
        total_count = len(known_rows) + len(candidate_rows)
        returned_count = len(returned) + len(returned_candidates)
        return {
            "status": "found" if total_count else "not_found",
            "query": channel_query,
            "event_fact_count": total_count,
            "known_linked_count": len(known_rows),
            "candidate_or_unlinked_count": len(candidate_rows),
            "returned_count": returned_count,
            "candidate_returned_count": len(returned_candidates),
            "evidence_buckets": ["known_linked", "candidate_or_unlinked"],
            "event_channels": returned,
            "candidate_or_unlinked": returned_candidates,
        }

    def _endpoint_reconciliation_warnings(
        self,
        docs_scope: list[str] | tuple[str, ...],
        backend_scope: list[str] | tuple[str, ...],
        client_scope: list[str] | tuple[str, ...],
        path_prefix: str | None,
    ) -> list[JsonObject]:
        warnings: list[JsonObject] = []
        if docs_scope and not self._has_endpoint_fact("DOCUMENTS_ENDPOINT", docs_scope, path_prefix):
            warnings.append(
                {
                    "scope": "docs",
                    "warning": "no_endpoint_documentation_evidence",
                    "coverage": self._endpoint_coverage_rows("DOCUMENTS_ENDPOINT", docs_scope),
                }
            )
        if backend_scope and not self._has_endpoint_fact("EXPOSES_ENDPOINT", backend_scope, path_prefix):
            warnings.append(
                {
                    "scope": "backend",
                    "warning": "no_endpoint_extractor_matched",
                    "coverage": self._endpoint_coverage_rows("EXPOSES_ENDPOINT", backend_scope),
                }
            )
        if client_scope and not self._has_endpoint_fact("CALLS_ENDPOINT", client_scope, path_prefix):
            warnings.append(
                {
                    "scope": "client",
                    "warning": "no_client_call_evidence",
                    "coverage": self._endpoint_coverage_rows("CALLS_ENDPOINT", client_scope),
                }
            )
        return warnings

    def _has_endpoint_fact(self, predicate: str, repos: list[str] | tuple[str, ...], path_prefix: str | None = None) -> bool:
        repo_filter = set(repos)
        for fact in self.facts:
            if fact.get("predicate") != predicate:
                continue
            subject = self.entities_by_id.get(fact["subject_id"])
            object_ = self.entities_by_id.get(fact["object_id"])
            if not subject or not object_:
                continue
            if path_prefix and not self._endpoint_path_matches(object_, path_prefix):
                continue
            if self._entity_repo(subject) in repo_filter or self._entity_repo(object_) in repo_filter:
                return True
        return False

    def _endpoint_coverage_rows(
        self,
        predicate: str,
        repos: list[str] | tuple[str, ...],
    ) -> list[JsonObject]:
        repo_filter = set(repos)
        return [
            row
            for row in self.coverage
            if row.get("predicate") == predicate and str(row.get("scope_ref", {}).get("repo")) in repo_filter
        ]

    def _endpoint_path_matches(self, entity: JsonObject, path_prefix: str) -> bool:
        if entity.get("kind") != "Endpoint":
            return False
        return endpoint_path_shape_matches_prefix(str(entity.get("identity", {}).get("path", "")), path_prefix)

    def _entity_repo(self, entity: JsonObject) -> str | None:
        identity = entity.get("identity", {})
        properties = entity.get("properties", {})
        return identity.get("repo") or properties.get("repo")

    def deploy_mappings(self, target_query: str | None = None, limit: int = 25) -> JsonObject:
        limit = self._clamp_limit(limit, max_limit=100)
        known_rows = []
        candidate_rows = []
        for fact in self.facts:
            if fact["predicate"] not in {"ROUTES_DOMAIN_TO_DEPLOY", "DEPLOYS_VIA_CONFIG"}:
                continue
            subject = self.entities_by_id.get(fact["subject_id"])
            target = self.entities_by_id.get(fact["object_id"])
            if not subject or not target:
                continue
            haystack = f"{self._display(subject)} {self._display(target)} {fact.get('predicate')} {fact.get('qualifier', {})}".lower()
            if target_query and target_query.lower() not in haystack:
                continue
            row = self._fact_result(fact, subject, target)
            if row.get("linkage_status") == "candidate_or_unlinked":
                candidate_rows.append(row)
            else:
                known_rows.append(row)
        known_rows = sorted(known_rows, key=lambda row: (str(row.get("subject")), str(row.get("object"))))
        candidate_rows = sorted(candidate_rows, key=lambda row: (str(row.get("subject")), str(row.get("object"))))
        returned = known_rows[:limit]
        remaining = max(0, limit - len(returned))
        returned_candidates = candidate_rows[:remaining]
        return {
            "status": "found" if known_rows or candidate_rows else "not_found",
            "query": target_query,
            "deploy_mapping_fact_count": len(known_rows) + len(candidate_rows),
            "known_linked_count": len(known_rows),
            "mapping_count": len(known_rows),
            "candidate_or_unlinked_count": len(candidate_rows),
            "returned_count": len(returned) + len(returned_candidates),
            "known_returned_count": len(returned),
            "candidate_returned_count": len(returned_candidates),
            "evidence_buckets": ["known_linked", "candidate_or_unlinked"],
            "mappings": returned,
            "candidate_or_unlinked": returned_candidates,
        }

    def _clamp_limit(self, limit: int, max_limit: int = 100) -> int:
        return min(max(1, limit), max_limit)

    def lookup_symbol(
        self,
        symbol_query: str,
        limit: int = 25,
        path: str | None = None,
        line: int | None = None,
    ) -> JsonObject:
        return self._resolve_symbol(symbol_query, limit=limit, path=path, line=line, allow_fuzzy=True)

    def symbols_in_file(self, file_path: str, limit: int = 100) -> JsonObject:
        normalized_path = self._normalize_path(file_path)
        symbols = sorted(
            [
                self._symbol_result(entity)
                for entity in self._symbol_entities()
                if self._normalize_path(str(entity.get("properties", {}).get("path", ""))) == normalized_path
            ],
            key=lambda row: (row.get("line") or 0, row["display_name"]),
        )
        returned_symbols = symbols[:limit]
        return {
            "status": "found" if symbols else "not_found",
            "path": normalized_path,
            "symbol_count": len(symbols),
            "returned_count": len(returned_symbols),
            "symbols": returned_symbols,
        }

    def evidence_for_call(
        self,
        caller_query: str,
        callee_query: str,
        path: str | None = None,
        line: int | None = None,
        limit: int = 25,
    ) -> JsonObject:
        caller_resolution = self._resolve_symbol(caller_query, limit=limit, path=path, line=line, allow_fuzzy=False)
        if caller_resolution["status"] != "resolved":
            return {
                "status": "ambiguous" if caller_resolution["status"] == "ambiguous" else "not_found",
                "caller": caller_resolution,
                "callee": self._resolve_symbol(callee_query, limit=limit, allow_fuzzy=False),
                "matches": [],
            }

        caller_id = caller_resolution["resolved_symbol"]["symbol_id"]
        coordinate_matches = self._call_facts_at_coordinate(caller_id, callee_query, path=path, line=line)
        if path or line is not None:
            return self._call_evidence_result(
                caller_resolution=caller_resolution,
                callee_query=callee_query,
                facts=coordinate_matches,
                limit=limit,
                path=path,
                line=line,
            )

        callee_resolution = self._resolve_symbol(callee_query, limit=limit, allow_fuzzy=False)
        if callee_resolution["status"] != "resolved":
            return {
                "status": "ambiguous" if callee_resolution["status"] == "ambiguous" else "not_found",
                "caller": caller_resolution,
                "callee": callee_resolution,
                "matches": [],
            }

        callee_id = callee_resolution["resolved_symbol"]["symbol_id"]
        facts = [
            fact
            for fact in self.facts
            if fact["predicate"] == "CALLS" and fact["subject_id"] == caller_id and fact["object_id"] == callee_id
        ]
        return self._call_evidence_result(
            caller_resolution=caller_resolution,
            callee_query=callee_query,
            facts=facts,
            limit=limit,
            path=path,
            line=line,
            callee_resolution=callee_resolution,
        )

    def _call_evidence_result(
        self,
        caller_resolution: JsonObject,
        callee_query: str,
        facts: list[JsonObject],
        limit: int,
        path: str | None,
        line: int | None,
        callee_resolution: JsonObject | None = None,
    ) -> JsonObject:
        matches: list[JsonObject] = []
        callee_entities: dict[str, JsonObject] = {}
        for fact in facts:
            evidence_rows = self.evidence_by_target.get(fact["fact_id"], [])
            if path or line is not None:
                evidence_rows = [
                    row
                    for row in evidence_rows
                    if self._evidence_matches_coordinate(row, path=path, line=line)
                ]
            if not evidence_rows:
                continue
            caller = self.entities_by_id.get(fact["subject_id"])
            callee = self.entities_by_id.get(fact["object_id"])
            if caller and callee:
                callee_entities[callee["entity_id"]] = callee
                matches.append({**self._fact_result(fact, caller, callee), "evidence": evidence_rows})
                if len(matches) >= limit:
                    break
        callee_resolution = callee_resolution or self._callee_resolution_from_matches(callee_query, list(callee_entities.values()), limit)
        return {
            "status": "found" if matches else "not_found",
            "caller": caller_resolution,
            "callee": callee_resolution,
            "matches": matches,
        }

    def _dependency_path_response(
        self,
        status: str,
        source_resolution: JsonObject,
        target_resolution: JsonObject | None,
        max_depth: int,
        limit: int,
        paths: list[JsonObject],
    ) -> JsonObject:
        response: JsonObject = {
            "status": status,
            "source": source_resolution,
            "max_depth": max_depth,
            "limit": limit,
            "path_count": len(paths),
            "returned_count": len(paths),
            "paths": paths,
        }
        if target_resolution is not None:
            response["target"] = target_resolution
        return response

    def _call_facts_at_coordinate(
        self,
        caller_id: str,
        callee_query: str,
        path: str | None,
        line: int | None,
    ) -> list[JsonObject]:
        facts: list[JsonObject] = []
        for fact in self.facts:
            if fact["predicate"] != "CALLS" or fact["subject_id"] != caller_id:
                continue
            callee = self.entities_by_id.get(fact["object_id"])
            if not callee or not self._symbol_query_matches_entity(callee_query, callee):
                continue
            evidence_rows = self.evidence_by_target.get(fact["fact_id"], [])
            if any(self._evidence_matches_coordinate(row, path=path, line=line) for row in evidence_rows):
                facts.append(fact)
        return facts

    def _callee_resolution_from_matches(self, callee_query: str, entities: list[JsonObject], limit: int) -> JsonObject:
        if not entities:
            return self._resolve_symbol(callee_query, limit=limit, allow_fuzzy=False)
        return self._resolution_result(callee_query, "exact_name", entities, limit)

    def symbol_import_consumer_leads(self, resolution: JsonObject, *, limit: int) -> JsonObject:
        return self._symbol_import_consumer_leads(resolution, limit=limit)

    def _symbol_import_consumer_leads(self, resolution: JsonObject, *, limit: int) -> JsonObject:
        if resolution.get("status") != "resolved":
            return {
                "status": "not_computed",
                "reason": "requires one resolved symbol",
                "lead_count": 0,
                "returned_count": 0,
                "leads": [],
            }
        resolved = resolution.get("resolved_symbol")
        if not isinstance(resolved, dict):
            return {
                "status": "not_computed",
                "reason": "resolved symbol missing",
                "lead_count": 0,
                "returned_count": 0,
                "leads": [],
            }
        symbol = self.entities_by_id.get(resolved.get("symbol_id"))
        if not symbol or symbol.get("kind") != "CodeSymbol":
            return {
                "status": "not_computed",
                "reason": "resolved target is not a local CodeSymbol",
                "lead_count": 0,
                "returned_count": 0,
                "leads": [],
            }
        module = self._module_entity_for_symbol(symbol)
        if module is None:
            return {
                "status": "missing_module",
                "lead_count": 0,
                "returned_count": 0,
                "leads": [],
            }
        imported_names = self._imported_name_candidates_for_symbol(symbol)
        leads = []
        linked_package_ids = self._linked_package_ids_for_symbol_repo(symbol)
        for fact in self.facts:
            if fact.get("predicate") != "IMPORTS":
                continue
            object_id = fact.get("object_id")
            fact_object = self.entities_by_id.get(object_id)
            if object_id == module["entity_id"]:
                match = self._symbol_import_match(fact, imported_names)
            elif object_id in linked_package_ids:
                match = self._linked_package_symbol_import_match(fact, symbol, imported_names)
            else:
                continue
            if match is None:
                continue
            importer = self.entities_by_id.get(fact.get("subject_id"))
            if not importer or importer.get("kind") != "CodeModule":
                continue
            leads.append(self._symbol_import_consumer_lead(fact, importer, module, symbol, match, fact_object or module))
        leads = sorted(
            leads,
            key=lambda row: (row["repo_relation"] != "cross_repo", row["importer"]["display_name"] or ""),
        )
        returned = leads[:limit]
        return {
            "status": "found" if returned else "empty",
            "contract": (
                "Import consumer leads are source-inspection leads derived from IMPORTS facts. "
                "They show modules that import the target symbol or its module; they are not proof of runtime execution. "
                "Module-import matches can be broad because importing a module makes multiple exported symbols available."
            ),
            "lead_count": len(leads),
            "returned_count": len(returned),
            "leads": returned,
        }

    def _linked_package_ids_for_symbol_repo(self, symbol: JsonObject) -> set[str]:
        identity = symbol.get("identity", {})
        tenant_id = identity.get("tenant_id")
        repo_name = identity.get("repo")
        if not isinstance(repo_name, str):
            return set()
        repo_entity_ids = {
            entity["entity_id"]
            for entity in self.entities
            if entity.get("kind") == "Repo" and entity.get("identity", {}).get("name") == repo_name
            and entity.get("identity", {}).get("tenant_id") == tenant_id
        }
        if not repo_entity_ids:
            return set()
        package_ids = set()
        for fact in self.facts:
            if fact.get("predicate") != "RESOLVES_TO_REPO" or fact.get("object_id") not in repo_entity_ids:
                continue
            package = self.entities_by_id.get(fact.get("subject_id"))
            if package and package.get("kind") == "ExternalPackage":
                package_ids.add(str(package["entity_id"]))
        return package_ids

    def _module_entity_for_symbol(self, symbol: JsonObject) -> JsonObject | None:
        identity = symbol.get("identity", {})
        repo = identity.get("repo")
        module_name = identity.get("module")
        if not isinstance(repo, str) or not isinstance(module_name, str):
            return None
        for entity in self.entities:
            if entity.get("kind") != "CodeModule":
                continue
            entity_identity = entity.get("identity", {})
            if (
                entity_identity.get("tenant_id") == identity.get("tenant_id")
                and entity_identity.get("repo") == repo
                and entity_identity.get("module") == module_name
            ):
                return entity
        return None

    def _imported_name_candidates_for_symbol(self, symbol: JsonObject) -> set[str]:
        identity = symbol.get("identity", {})
        qualname = identity.get("qualname")
        if not isinstance(qualname, str) or not qualname:
            return set()
        names = {qualname}
        outer_name = qualname.split(".", 1)[0]
        if outer_name:
            names.add(outer_name)
        return names

    def _symbol_import_match(self, fact: JsonObject, imported_names: set[str]) -> JsonObject | None:
        qualifier = fact.get("qualifier", {})
        raw_imported_names = qualifier.get("imported_names")
        if isinstance(raw_imported_names, list):
            names = {name for name in raw_imported_names if isinstance(name, str) and name}
            if not names:
                return {"match_kind": "module_import", "matched_imported_names": []}
            matched = sorted(names & imported_names)
            if matched:
                return {"match_kind": "imported_name", "matched_imported_names": matched}
            return None
        return None

    def _linked_package_symbol_import_match(
        self,
        fact: JsonObject,
        symbol: JsonObject,
        imported_names: set[str],
    ) -> JsonObject | None:
        qualifier = fact.get("qualifier", {})
        identity = symbol.get("identity", {})
        module_name = identity.get("module")
        if not isinstance(module_name, str) or not module_name:
            return None
        imported_module = qualifier.get("module_name") or qualifier.get("raw_import")
        if imported_module != module_name:
            return None
        match = self._symbol_import_match(fact, imported_names)
        if match is None:
            return None
        return {
            **match,
            "match_kind": f"linked_package_{match['match_kind']}",
        }

    def _symbol_import_consumer_lead(
        self,
        fact: JsonObject,
        importer: JsonObject,
        imported_module: JsonObject,
        symbol: JsonObject,
        match: JsonObject,
        fact_object: JsonObject,
    ) -> JsonObject:
        importer_identity = importer.get("identity", {})
        symbol_identity = symbol.get("identity", {})
        importer_repo = importer_identity.get("repo")
        symbol_repo = symbol_identity.get("repo")
        return {
            "lead_kind": "import_consumer",
            "interpretation": (
                "Importer module imports the changed symbol's module/name. "
                "Treat as a source-inspection lead, not as proven runtime caller impact."
            ),
            "repo_relation": "same_repo" if importer_repo == symbol_repo else "cross_repo",
            "importer": self._entity_reference(importer),
            "imported_module": self._entity_reference(imported_module),
            "imported_symbol": self._symbol_result(symbol),
            "match": match,
            "fact": self._fact_result(fact, importer, fact_object),
            "importer_module_symbols": self._module_symbols(importer, limit=5),
        }

    def _module_symbols(self, module: JsonObject, *, limit: int) -> list[JsonObject]:
        identity = module.get("identity", {})
        repo = identity.get("repo")
        module_name = identity.get("module")
        if not isinstance(repo, str) or not isinstance(module_name, str):
            return []
        rows = [
            self._symbol_result(entity)
            for entity in self._symbol_entities()
            if entity.get("kind") == "CodeSymbol"
            and entity.get("identity", {}).get("repo") == repo
            and entity.get("identity", {}).get("module") == module_name
        ]
        rows = sorted(rows, key=self._symbol_sort_key)
        return rows[:limit]

    def _entity_reference(self, entity: JsonObject) -> JsonObject:
        identity = entity.get("identity", {})
        properties = entity.get("properties", {})
        return {
            "entity_id": entity.get("entity_id"),
            "kind": entity.get("kind"),
            "display_name": self._display(entity),
            "repo": identity.get("repo"),
            "module": identity.get("module"),
            "path": properties.get("path"),
        }

    def _matching_symbols(self, symbol_query: str) -> list[JsonObject]:
        needle = symbol_query.lower()
        return [
            entity
            for entity in self.entities
            if entity["kind"] == "CodeSymbol"
            and needle in f"{entity['identity'].get('module', '')}.{entity['identity'].get('qualname', '')}".lower()
        ]

    def _resolve_symbol(
        self,
        symbol_query: str,
        limit: int = 25,
        path: str | None = None,
        line: int | None = None,
        *,
        allow_fuzzy: bool,
    ) -> JsonObject:
        query = symbol_query.strip()
        if not query:
            return {
                "status": "not_found",
                "query": symbol_query,
                "confidence": "empty_query",
                "resolved_symbol": None,
                "candidates": [],
            }

        exact_qualified = []
        exact_name = []
        fuzzy = []
        needle = query.lower()
        query_is_qualified = "." in query
        for entity in self._symbol_entities():
            qualified = self._display(entity)
            qualname = str(entity["identity"].get("qualname", ""))
            short_name = qualname.rsplit(".", 1)[-1]
            if qualified.lower() == needle:
                exact_qualified.append(entity)
            elif query_is_qualified and qualname.lower() == needle:
                exact_name.append(entity)
            elif not query_is_qualified and (qualname.lower() == needle or short_name.lower() == needle):
                exact_name.append(entity)
            elif allow_fuzzy and needle in qualified.lower():
                fuzzy.append(entity)

        if exact_qualified:
            return self._resolution_result_for_coordinate(
                query, "exact_qualified", exact_qualified, limit, path=path, line=line
            )
        if exact_name:
            return self._resolution_result_for_coordinate(query, "exact_name", exact_name, limit, path=path, line=line)
        if allow_fuzzy and fuzzy:
            return self._resolution_result_for_coordinate(query, "fuzzy", fuzzy, limit, path=path, line=line)
        return {
            "status": "not_found",
            "query": query,
            "confidence": "not_found",
            "resolved_symbol": None,
            "candidates": [],
        }

    def _resolution_result_for_coordinate(
        self,
        query: str,
        match_type: str,
        entities: list[JsonObject],
        limit: int,
        *,
        path: str | None,
        line: int | None,
    ) -> JsonObject:
        if not path and line is None:
            return self._resolution_result(query, match_type, entities, limit)

        # Coordinate constraints are part of anchor resolution. A bad coordinate
        # should not be overridden by include_all-style aggregation upstream.
        coordinate_matches = [
            entity for entity in entities if self._symbol_matches_coordinate(entity, path=path, line=line)
        ]
        if coordinate_matches:
            return self._resolution_result(query, match_type, coordinate_matches, limit)
        return self._coordinate_mismatch_result(query, match_type, entities, limit, path=path, line=line)

    def _resolution_result(self, query: str, match_type: str, entities: list[JsonObject], limit: int) -> JsonObject:
        candidates = sorted((self._symbol_result(entity) for entity in entities), key=self._symbol_sort_key)
        is_unique = len(candidates) == 1
        confidence = {
            "exact_qualified": "exact_unique" if is_unique else "exact_multiple",
            "exact_name": "exact_unique" if is_unique else "exact_multiple",
            "fuzzy": "fuzzy_unique" if is_unique else "fuzzy_multiple",
        }[match_type]
        return {
            "status": "resolved" if is_unique else "ambiguous",
            "query": query,
            "confidence": confidence,
            "resolved_symbol": candidates[0] if is_unique else None,
            "candidates": candidates[:limit],
            "candidate_count": len(candidates),
        }

    def _coordinate_mismatch_result(
        self,
        query: str,
        match_type: str,
        entities: list[JsonObject],
        limit: int,
        *,
        path: str | None,
        line: int | None,
    ) -> JsonObject:
        candidates = sorted((self._symbol_result(entity) for entity in entities), key=self._symbol_sort_key)
        retry_arguments = [self._symbol_retry_arguments(candidate, query) for candidate in candidates[:limit]]
        requested_coordinate: JsonObject = {}
        if path:
            requested_coordinate["path"] = self._normalize_path(path)
        if line is not None:
            requested_coordinate["line"] = line
        return {
            "status": "not_found",
            "query": query,
            "confidence": "coordinate_mismatch",
            "resolved_symbol": None,
            "candidates": candidates[:limit],
            "candidate_count": len(candidates),
            "coordinate_mismatch": {
                "status": "symbol_found_at_different_coordinate",
                "message": (
                    f"Symbol {query!r} matched by {match_type}, but no matching symbol exists at the requested "
                    "path/line. Retry with one of these candidate retry arguments before interpreting this as a "
                    "missing symbol or an empty result."
                ),
                "requested": requested_coordinate,
                "match_type": match_type,
                "candidate_count": len(candidates),
                "candidates": [self._symbol_disambiguation_candidate(candidate) for candidate in candidates[:limit]],
                "retry_arguments": retry_arguments,
            },
            "next_actions": [
                "The symbol exists in the graph, but no candidate matched the requested path/line.",
                "Retry with one coordinate_mismatch.retry_arguments entry before treating this as a missing symbol or an empty result.",
            ],
        }

    def _symbol_disambiguation_payload(self, resolution: JsonObject, *, result_kind: str) -> JsonObject:
        candidates = list(resolution.get("candidates", []))
        retry_arguments = [self._symbol_retry_arguments(candidate, resolution["query"]) for candidate in candidates]
        return {
            "result_computed": False,
            "disambiguation": {
                "status": "required",
                "reason": "ambiguous_symbol",
                "message": (
                    f"Multiple symbols matched {resolution['query']!r}; no {result_kind} result was computed. "
                    "Retry with one candidate's exact qualified name or with path and line."
                ),
                "candidate_count": resolution.get("candidate_count", len(candidates)),
                "candidates": [self._symbol_disambiguation_candidate(candidate) for candidate in candidates],
                "retry_arguments": retry_arguments,
            },
            "next_actions": [
                "Pick one candidate from disambiguation.candidates before treating this as evidence.",
                "Retry with `symbol` set to a candidate `qualified_name`, or keep the original symbol and add the candidate `path` and `line`.",
                "Use `include_all=true` only for exploratory aggregation across all matching symbols, not for precise impact answers.",
            ],
        }

    def _symbol_disambiguation_candidate(self, candidate: JsonObject) -> JsonObject:
        return {
            key: candidate.get(key)
            for key in ("qualified_name", "repo", "module", "qualname", "symbol_kind", "path", "line")
            if candidate.get(key) is not None
        }

    def _symbol_retry_arguments(self, candidate: JsonObject, original_query: str) -> JsonObject:
        path = candidate.get("path")
        line = candidate.get("line")
        qualified_name = candidate.get("qualified_name")
        retry: JsonObject = {
            "symbol": qualified_name if isinstance(qualified_name, str) and qualified_name else original_query,
        }
        if isinstance(path, str) and path:
            retry["path"] = path
        if isinstance(line, int):
            retry["line"] = line
        return retry

    def _symbol_entities(self) -> list[JsonObject]:
        return [entity for entity in self.entities if entity["kind"] in {"CodeSymbol", "ExternalSymbol"}]

    def _symbol_result(self, entity: JsonObject) -> JsonObject:
        identity = entity["identity"]
        properties = entity.get("properties", {})
        if entity["kind"] == "ExternalSymbol":
            module = identity.get("module")
            name = identity.get("name")
            qualified_name = f"{module}.{name}" if module and name else str(name or "")
            return {
                "symbol_id": entity["entity_id"],
                "display_name": self._display(entity),
                "qualified_name": qualified_name,
                "repo": identity.get("repo"),
                "module": module,
                "qualname": name,
                "symbol_kind": identity.get("symbol_kind"),
                "path": properties.get("path"),
                "line": properties.get("line"),
                "end_line": properties.get("end_line"),
                "evidence": self.evidence_by_target.get(entity["entity_id"], []),
            }
        return {
            "symbol_id": entity["entity_id"],
            "display_name": self._display(entity),
            "qualified_name": f"{identity.get('module')}.{identity.get('qualname')}",
            "repo": identity.get("repo"),
            "module": identity.get("module"),
            "qualname": identity.get("qualname"),
            "symbol_kind": identity.get("symbol_kind"),
            "path": properties.get("path"),
            "line": properties.get("line"),
            "end_line": properties.get("end_line"),
            "evidence": self.evidence_by_target.get(entity["entity_id"], []),
        }

    def _symbol_sort_key(self, row: JsonObject) -> tuple[str, int, str]:
        return (str(row.get("path") or ""), int(row.get("line") or 0), row["display_name"])

    def _evidence_matches_coordinate(self, evidence: JsonObject, path: str | None, line: int | None) -> bool:
        bytes_ref = evidence.get("bytes_ref") or {}
        if path and self._normalize_path(str(bytes_ref.get("path", ""))) != self._normalize_path(path):
            return False
        if line is None:
            return True
        line_start = int(bytes_ref.get("line_start") or 0)
        line_end = int(bytes_ref.get("line_end") or line_start)
        return line_start <= line <= line_end

    def _symbol_matches_coordinate(self, entity: JsonObject, path: str | None, line: int | None) -> bool:
        properties = entity.get("properties", {})
        if path and self._normalize_path(str(properties.get("path", ""))) != self._normalize_path(path):
            return False
        if line is None:
            return True

        start = properties.get("line")
        end = properties.get("end_line")
        if start is not None:
            line_start = int(start)
            line_end = int(end or start)
            if line_start <= line <= line_end:
                return True
        return any(
            self._evidence_matches_coordinate(row, path=path, line=line)
            for row in self.evidence_by_target.get(entity["entity_id"], [])
        )

    def _symbol_query_matches_entity(self, symbol_query: str, entity: JsonObject) -> bool:
        if entity["kind"] == "ExternalSymbol":
            query = symbol_query.strip().lower()
            identity = entity["identity"]
            name = str(identity.get("name", "")).lower()
            module = str(identity.get("module", "")).lower()
            qualified = f"{module}.{name}" if module and name else name
            return query in {name, qualified}
        if entity["kind"] != "CodeSymbol":
            return False
        query = symbol_query.strip().lower()
        identity = entity["identity"]
        qualified = self._display(entity).lower()
        qualname = str(identity.get("qualname", "")).lower()
        short_name = qualname.rsplit(".", 1)[-1]
        if "." in query:
            return query in {qualified, qualname}
        return query in {qualname, short_name}

    def _normalize_path(self, file_path: str) -> str:
        return file_path.replace("\\", "/").lstrip("./")

    def _import_matches(self, fact: JsonObject, package: JsonObject, package_name: str) -> bool:
        return aggregations.import_matches_target(fact, package, package_name)

    def _fact_result(self, fact: JsonObject, subject: JsonObject, object_: JsonObject, **extra: Any) -> JsonObject:
        row = {
            **extra,
            "fact_id": fact["fact_id"],
            "predicate": fact["predicate"],
            "canonical_status": _canonical_status(fact),
            "subject": self._display(subject),
            "object": self._display(object_),
            "qualifier": fact.get("qualifier", {}),
            "evidence": self.evidence_by_target.get(fact["fact_id"], []),
        }
        if fact.get("predicate") == "DEPLOYS_VIA_CONFIG":
            row["linkage_status"] = (
                "known_linked" if _canonical_status(fact) == "canonical" else "candidate_or_unlinked"
            )
        call_site = call_site_from_qualifier(fact.get("qualifier", {}))
        if call_site is not None:
            row["call_site"] = call_site
        return row

    def _display(self, entity: JsonObject) -> str:
        return display_entity(entity)


def _read_manifest(path: Path) -> JsonObject:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except JSONDecodeError as exc:
        raise ValueError(f"Invalid KG manifest JSON: {path}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"KG manifest must be a JSON object: {path}")
    return data


def _dedupe_fact_results(rows: list[JsonObject]) -> list[JsonObject]:
    seen = set()
    deduped = []
    for row in rows:
        fact_id = row.get("fact_id")
        if fact_id in seen:
            continue
        seen.add(fact_id)
        deduped.append(row)
    return deduped
