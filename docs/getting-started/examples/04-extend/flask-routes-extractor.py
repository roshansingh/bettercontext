#!/usr/bin/env python3
"""
Flask Routes Extractor for SuperContext

This is a complete working example of a custom extractor. It demonstrates:

1. Walking a Flask repository for route definitions
2. Parsing Python decorators (@app.route, @bp.route, etc.)
3. Extracting HTTP method, path, and handler information
4. Emitting SuperContext KG entities (Endpoints) and facts (HOSTS relations)
5. Including evidence citations back to source code

The extractor finds patterns like:

    @app.route('/api/users', methods=['GET'])
    def list_users():
        ...

    @bp.route('/users/<int:id>', methods=['PUT', 'POST'])
    def update_user(id):
        ...

And emits:
  - Entity: Endpoint (http, GET, /api/users)
  - Entity: Endpoint (http, PUT, /users/<int:id>)
  - Entity: Endpoint (http, POST, /users/<int:id>)
  - Fact: Service HOSTS each Endpoint
  - Evidence: Source code lines with decorator

Usage:
    python flask-routes-extractor.py /path/to/flask/repo
"""

from __future__ import annotations

import ast
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

from source.kg.core.models import Entity, Fact, Evidence


# ─────────────────────────────────────────────────────────────────────────────
# Data Models
# ─────────────────────────────────────────────────────────────────────────────


class ExtractionResult(NamedTuple):
    """Result of Flask route extraction."""

    entities: list[Entity]
    facts: list[Fact]
    evidence: list[Evidence]


class RouteInfo(NamedTuple):
    """Information extracted from a Flask route decorator."""

    path: str
    methods: list[str]
    handler_name: str
    handler_urn: str
    filepath: str
    line_start: int
    line_end: int


# ─────────────────────────────────────────────────────────────────────────────
# Route Visitor: AST Walker
# ─────────────────────────────────────────────────────────────────────────────


class FlaskRouteVisitor(ast.NodeVisitor):
    """
    AST visitor that walks Python code and finds Flask route decorators.

    Looks for patterns:
      - @app.route(...)
      - @bp.route(...) where bp is a Blueprint
      - Methods extracted from decorator arguments
      - Fallback to ['GET'] if methods not specified
    """

    def __init__(self, filepath: str, repo_name: str, tenant_id: str, source_code: str):
        self.filepath = filepath
        self.repo_name = repo_name
        self.tenant_id = tenant_id
        self.source_code = source_code
        self.routes: list[RouteInfo] = []
        self.line_mapping = source_code.split("\n")

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """Visit a function definition and check for Flask route decorators."""
        for decorator in node.decorator_list:
            route_info = self._extract_route_from_decorator(decorator, node)
            if route_info:
                self.routes.append(route_info)

        self.generic_visit(node)

    def _extract_route_from_decorator(self, decorator: ast.expr, func_node: ast.FunctionDef) -> RouteInfo | None:
        """
        Extract route info from a decorator node.

        Handles:
          @app.route('/path')
          @app.route('/path', methods=['GET', 'POST'])
          @bp.route('/path', methods=['PUT'])
        """
        # Pattern: decorator is a Call node (e.g., @app.route(...))
        if not isinstance(decorator, ast.Call):
            return None

        # Check if it's app.route() or bp.route()
        if not self._is_flask_route_call(decorator):
            return None

        # Extract the path (first positional arg)
        path = self._extract_path_from_call(decorator)
        if not path:
            return None

        # Extract methods (from 'methods' keyword arg)
        methods = self._extract_methods_from_call(decorator)
        if not methods:
            methods = ["GET"]

        # Build handler URN (simple: repo:module:function)
        handler_name = func_node.name
        handler_urn = f"supercontext://code-symbol/{self.repo_name}/{self.filepath}/{handler_name}"

        return RouteInfo(
            path=path,
            methods=methods,
            handler_name=handler_name,
            handler_urn=handler_urn,
            filepath=self.filepath,
            line_start=decorator.lineno,
            line_end=decorator.end_lineno or decorator.lineno,
        )

    def _is_flask_route_call(self, node: ast.Call) -> bool:
        """Check if this is a call to app.route() or bp.route()."""
        if isinstance(node.func, ast.Attribute):
            # Pattern: something.route (e.g., app.route, bp.route)
            if node.func.attr == "route":
                return True
        return False

    def _extract_path_from_call(self, node: ast.Call) -> str | None:
        """Extract the path from the first positional argument."""
        if not node.args:
            return None

        first_arg = node.args[0]
        if isinstance(first_arg, ast.Constant):
            return str(first_arg.value)
        if isinstance(first_arg, ast.Str):  # Python < 3.8
            return first_arg.s

        return None

    def _extract_methods_from_call(self, node: ast.Call) -> list[str]:
        """Extract methods from 'methods' keyword argument."""
        for keyword in node.keywords:
            if keyword.arg == "methods":
                return self._extract_list_from_node(keyword.value)
        return []

    def _extract_list_from_node(self, node: ast.expr) -> list[str]:
        """Extract a list of strings from an AST List node."""
        if isinstance(node, ast.List):
            result = []
            for elt in node.elts:
                if isinstance(elt, ast.Constant):
                    result.append(str(elt.value))
                elif isinstance(elt, ast.Str):  # Python < 3.8
                    result.append(elt.s)
            return result
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Flask Routes Extractor
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class FlaskRoutesExtractor:
    """
    Main extractor class for Flask routes.

    Walks a Flask repository, finds all route decorators, and emits
    SuperContext entities and facts.
    """

    repo_path: str = "."
    repo_name: str = "flask"
    tenant_id: str = "default"
    service_name: str = "flask-api"  # Service that hosts the routes

    def extract(self) -> ExtractionResult:
        """
        Extract all Flask routes from the repository.

        Returns:
            ExtractionResult with entities, facts, and evidence
        """
        entities: list[Entity] = []
        facts: list[Fact] = []
        evidence: list[Evidence] = []

        routes = self._find_all_routes()

        # For each route, emit an Endpoint entity and HOSTS fact
        for route in routes:
            # Emit Endpoint entity for each HTTP method
            for method in route.methods:
                endpoint_entity = self._make_endpoint_entity(route, method)
                entities.append(endpoint_entity)

                # Emit HOSTS fact (Service -> Endpoint)
                fact = self._make_hosts_fact(endpoint_entity, route)
                facts.append(fact)

                # Emit Evidence for the decorator
                evid = self._make_evidence(endpoint_entity.entity_id, route)
                evidence.append(evid)

        return ExtractionResult(entities=entities, facts=facts, evidence=evidence)

    def _find_all_routes(self) -> list[RouteInfo]:
        """Walk the repository and find all Flask route decorators."""
        routes: list[RouteInfo] = []

        for root, dirs, files in os.walk(self.repo_path):
            # Skip common non-code directories
            dirs[:] = [d for d in dirs if d not in {".git", "__pycache__", ".venv", "venv", "node_modules"}]

            for filename in files:
                if not filename.endswith(".py"):
                    continue

                filepath = os.path.join(root, filename)
                relpath = os.path.relpath(filepath, self.repo_path)

                try:
                    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                        source_code = f.read()
                    tree = ast.parse(source_code)
                except (SyntaxError, IOError, OSError):
                    continue

                visitor = FlaskRouteVisitor(relpath, self.repo_name, self.tenant_id, source_code)
                visitor.visit(tree)
                routes.extend(visitor.routes)

        return routes

    def _make_endpoint_entity(self, route: RouteInfo, method: str) -> Entity:
        """Create an Endpoint entity for a route and HTTP method."""
        identity = {
            "tenant_id": self.tenant_id,
            "repo": self.repo_name,
            "protocol": "http",
            "method": method,
            "path": route.path,
            "host": None,  # Not specified for Flask
        }

        return Entity(
            kind="Endpoint",
            identity=identity,
            properties={
                "handler": route.handler_name,
                "file": route.filepath,
            },
            canonical_status="canonical",
        )

    def _make_hosts_fact(self, endpoint_entity: Entity, route: RouteInfo) -> Fact:
        """Create a HOSTS fact (Service hosts this Endpoint)."""
        service_identity = {
            "tenant_id": self.tenant_id,
            "repo": self.repo_name,
            "namespace": "default",
            "slug": self.service_name,
        }
        service_urn = f"supercontext://service/{self.service_name}"

        return Fact(
            predicate="HOSTS",
            subject_id=service_urn,
            object_id=endpoint_entity.urn,
            qualifier={
                "handler": route.handler_name,
                "methods": route.methods,
            },
            canonical_status="canonical",
        )

    def _make_evidence(self, entity_id: str, route: RouteInfo) -> Evidence:
        """Create Evidence linking the entity to source code."""
        return Evidence(
            target_type="entity",
            target_id=entity_id,
            derivation_class="deterministic_static",
            source_system="flask-routes-extractor",
            source_ref={
                "method": "ast",
                "pattern": "decorator",
                "decorator_name": "route",
            },
            bytes_ref={
                "repo": self.repo_name,
                "path": route.filepath,
                "line_start": route.line_start,
                "line_end": route.line_end,
            },
        )


# ─────────────────────────────────────────────────────────────────────────────
# Output Formatting
# ─────────────────────────────────────────────────────────────────────────────


def print_extraction_results(result: ExtractionResult) -> None:
    """Print extraction results in a readable format."""
    print("\n" + "=" * 80)
    print("FLASK ROUTES EXTRACTION RESULTS")
    print("=" * 80)

    print(f"\nSummary:")
    print(f"  Endpoints found: {len(result.entities)}")
    print(f"  Relations (HOSTS facts): {len(result.facts)}")
    print(f"  Evidence records: {len(result.evidence)}")

    if not result.entities:
        print("\nNo Flask routes found.")
        return

    print(f"\nEndpoints (first 20):")
    print("-" * 80)

    for entity in sorted(result.entities, key=lambda e: e.identity.get("path", ""))[:20]:
        method = entity.identity.get("method", "?")
        path = entity.identity.get("path", "?")
        handler = entity.properties.get("handler", "?")
        print(f"  {method:6} {path:40} → {handler}")

    if len(result.entities) > 20:
        print(f"  ... and {len(result.entities) - 20} more")

    print(f"\nSample Evidence:")
    print("-" * 80)

    for evid in result.evidence[:5]:
        if evid.bytes_ref:
            print(f"  {evid.bytes_ref.get('path', '?')}:{evid.bytes_ref.get('line_start', '?')}")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────


def main() -> int:
    """Main entry point."""
    repo_path = sys.argv[1] if len(sys.argv) > 1 else "."

    if not os.path.isdir(repo_path):
        print(f"Error: {repo_path} is not a directory")
        return 1

    extractor = FlaskRoutesExtractor(repo_path=repo_path)
    result = extractor.extract()

    print_extraction_results(result)

    # Optionally save to JSONL
    if len(sys.argv) > 2:
        output_dir = sys.argv[2]
        os.makedirs(output_dir, exist_ok=True)

        # Write entities
        with open(os.path.join(output_dir, "entities.jsonl"), "w") as f:
            for entity in result.entities:
                f.write(json.dumps(entity.to_record()) + "\n")

        # Write facts
        with open(os.path.join(output_dir, "facts.jsonl"), "w") as f:
            for fact in result.facts:
                f.write(json.dumps(fact.to_record()) + "\n")

        # Write evidence
        with open(os.path.join(output_dir, "evidence.jsonl"), "w") as f:
            for evid in result.evidence:
                f.write(json.dumps(evid.to_record()) + "\n")

        print(f"\nResults written to {output_dir}/")

    return 0


if __name__ == "__main__":
    sys.exit(main())
