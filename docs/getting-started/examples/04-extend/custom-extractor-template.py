#!/usr/bin/env python3
"""
Custom Extractor Template for SuperContext

Copy this file, rename it, and implement the logic for extracting specific facts
from your codebase. The template provides helper methods for emitting entities,
facts, and evidence records in the SuperContext KG format.

Extractors are used to pull semantic information from code that the standard
AST extractors don't capture — for example, Flask routes, Celery tasks, database
migrations, configuration patterns, or domain-specific annotations.

Usage:
    1. Copy this template to a new file (e.g., my_custom_extractor.py)
    2. Implement the extract() method to walk your codebase
    3. Use helper methods to emit entities, facts, and evidence
    4. Test with the provided test template
    5. Register the extractor in the KG builder

Example:
    extractor = CustomExtractor(repo_path=".")
    entities, facts, evidence = extractor.extract()
    print(f"Extracted {len(entities)} entities, {len(facts)} facts")
"""

from __future__ import annotations

import ast
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

from source.kg.core.models import Entity, Fact, Evidence


# ─────────────────────────────────────────────────────────────────────────────
# Data Models
# ─────────────────────────────────────────────────────────────────────────────


class ExtractionResult(NamedTuple):
    """Result of extraction: entities, facts, and evidence."""

    entities: list[Entity]
    facts: list[Fact]
    evidence: list[Evidence]


@dataclass
class CustomExtractor:
    """
    Base template for a custom code extractor.

    This class walks your codebase and extracts semantic information
    that will be stored in the SuperContext KG.

    Attributes:
        repo_path: Path to the repository to analyze
        repo_name: Name/slug for the repository
        tenant_id: Tenant identifier (default: "default")
    """

    repo_path: str = "."
    repo_name: str = "default"
    tenant_id: str = "default"

    def extract(self) -> ExtractionResult:
        """
        Main extraction method.

        TODO 1: Walk the codebase (os.walk, Path.rglob, etc.)
        TODO 2: For each file, parse it (AST, config, regex, etc.)
        TODO 3: For each pattern found, emit entities, facts, evidence

        Returns:
            ExtractionResult with lists of entities, facts, and evidence
        """
        entities: list[Entity] = []
        facts: list[Fact] = []
        evidence: list[Evidence] = []

        # TODO: Replace with your extraction logic
        # Example structure:
        for root, dirs, files in os.walk(self.repo_path):
            # Filter directories to skip (e.g., node_modules, __pycache__)
            dirs[:] = [d for d in dirs if d not in {".git", "__pycache__", "node_modules"}]

            for filename in files:
                filepath = os.path.join(root, filename)
                relpath = os.path.relpath(filepath, self.repo_path)

                # TODO: Change extension filter as needed
                if not filename.endswith(".py"):
                    continue

                try:
                    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                        source_code = f.read()
                except (IOError, OSError):
                    continue

                # TODO 2: Parse the file
                # tree = ast.parse(source_code)
                # Or use regex, config parser, etc.

                # TODO 3: Walk the AST/results and emit entities/facts/evidence
                # For example:
                # visitor = CustomVisitor(relpath, self.repo_name, self.tenant_id)
                # visitor.visit(tree)
                # entities.extend(visitor.entities)
                # facts.extend(visitor.facts)
                # evidence.extend(visitor.evidence)

        return ExtractionResult(entities=entities, facts=facts, evidence=evidence)

    def _emit_entity(
        self,
        kind: str,
        identity: dict[str, Any],
        properties: dict[str, Any] | None = None,
    ) -> Entity:
        """
        Helper: Create an entity (code object).

        Args:
            kind: Entity type (e.g., "CodeSymbol", "Endpoint", "Service")
            identity: Unique identifier fields (used to generate URN)
            properties: Optional extra properties

        Returns:
            Entity object ready to be stored

        Example:
            entity = self._emit_entity(
                kind="Endpoint",
                identity={
                    "tenant_id": "default",
                    "repo": "flask",
                    "protocol": "http",
                    "method": "GET",
                    "path": "/api/users",
                }
            )
        """
        if properties is None:
            properties = {}

        return Entity(
            kind=kind,
            identity=identity,
            properties=properties,
            canonical_status="canonical",
        )

    def _emit_fact(
        self,
        predicate: str,
        subject_id: str,
        object_id: str,
        qualifier: dict[str, Any] | None = None,
    ) -> Fact:
        """
        Helper: Create a fact (relationship between entities).

        Args:
            predicate: Relation type (e.g., "CALLS", "HOSTS", "IMPORTS")
            subject_id: URN of the source entity
            object_id: URN of the target entity
            qualifier: Optional role or context (e.g., {"role": "route_handler"})

        Returns:
            Fact object ready to be stored

        Example:
            fact = self._emit_fact(
                predicate="HOSTS",
                subject_id="supercontext://service/api/users-service",
                object_id="supercontext://endpoint/http/GET/api/users",
                qualifier={"path": "/api/users"}
            )
        """
        if qualifier is None:
            qualifier = {}

        return Fact(
            predicate=predicate,
            subject_id=subject_id,
            object_id=object_id,
            qualifier=qualifier,
            canonical_status="canonical",
        )

    def _emit_evidence(
        self,
        target_type: str,
        target_id: str,
        derivation_class: str,
        source_system: str,
        source_ref: dict[str, Any],
        bytes_ref: dict[str, Any] | None = None,
    ) -> Evidence:
        """
        Helper: Create evidence (proof of a fact or entity).

        Evidence links back to the source code that supports a fact.
        This allows the KG to show where information came from.

        Args:
            target_type: "entity" or "fact"
            target_id: entity_id or fact_id being cited
            derivation_class: How the fact was derived
                - "deterministic_static": From deterministic AST/config parsing
                - "inferred_llm": From LLM inference
                - "runtime_observed": From runtime analysis
                - "manual_override": Manually entered
            source_system: Name of the extractor (e.g., "custom-flask-routes")
            source_ref: Metadata about the source (e.g., {"parser": "ast"})
            bytes_ref: Optional code location (repo, commit, file, line_start, line_end)

        Returns:
            Evidence object ready to be stored

        Example:
            evidence = self._emit_evidence(
                target_type="fact",
                target_id="fact_abc123",
                derivation_class="deterministic_static",
                source_system="flask-routes-extractor",
                source_ref={"parser": "ast", "decorator": "@app.route"},
                bytes_ref={
                    "repo": "flask",
                    "commit_sha": "abc123def456",
                    "path": "app.py",
                    "line_start": 10,
                    "line_end": 15,
                }
            )
        """
        return Evidence(
            target_type=target_type,
            target_id=target_id,
            derivation_class=derivation_class,
            source_system=source_system,
            source_ref=source_ref,
            bytes_ref=bytes_ref,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Example: Custom AST Visitor
# ─────────────────────────────────────────────────────────────────────────────


class CustomVisitor(ast.NodeVisitor):
    """
    Example AST visitor for extracting patterns.

    Extend this to implement your custom extraction logic.
    """

    def __init__(self, filepath: str, repo_name: str, tenant_id: str):
        self.filepath = filepath
        self.repo_name = repo_name
        self.tenant_id = tenant_id
        self.entities: list[Entity] = []
        self.facts: list[Fact] = []
        self.evidence: list[Evidence] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """
        Visit a function definition.

        TODO: Extend this to extract patterns you care about.
        Example: Look for decorators like @app.route, @task, @migration
        """
        # self.generic_visit(node)
        pass

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """Visit a class definition."""
        # self.generic_visit(node)
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────


def main() -> None:
    """Example usage of the custom extractor."""
    extractor = CustomExtractor(repo_path=".")
    result = extractor.extract()

    print(f"Extraction Results:")
    print(f"  Entities: {len(result.entities)}")
    print(f"  Facts: {len(result.facts)}")
    print(f"  Evidence: {len(result.evidence)}")

    # Print first few entities as examples
    if result.entities:
        print(f"\nFirst 5 Entities:")
        for entity in result.entities[:5]:
            print(f"  - {entity.kind}: {entity.identity}")


if __name__ == "__main__":
    main()
