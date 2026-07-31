#!/usr/bin/env python3
"""
Find Code Impact: Programmatic Blast Radius Analysis

This script demonstrates how to work directly with SuperContext JSONL snapshots
using Python. Instead of using the command-line tools, it:

1. Loads the snapshot JSONL files directly
2. Finds a target symbol in the entities
3. Walks the call graph to find affected code
4. Reports the blast radius in a human-readable format

Usage:
    python find-impact.py [symbol] [snapshot]

Examples:
    python find-impact.py                                    # Default: Flask in default snapshot
    python find-impact.py "Flask.request"                    # Analyze Flask.request
    python find-impact.py "authenticate" ~/snapshots/myrepo  # Analyze 'authenticate' in custom snapshot
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple


# Configuration
PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
DEFAULT_SNAPSHOT = PROJECT_ROOT / "data" / "kg_runs" / "flask"
DEFAULT_SYMBOL = "Flask.request"


# ─────────────────────────────────────────────────────────────────────────────
# Data Models
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Entity:
    """Represents a code entity (function, class, module)."""

    urn: str
    entity_type: str
    name: str
    repository: str
    file_path: str
    line_start: int | None = None
    line_end: int | None = None

    @classmethod
    def from_jsonl(cls, data: dict) -> Entity:
        return cls(
            urn=data["urn"],
            entity_type=data["entity_type"],
            name=data["name"],
            repository=data["repository"],
            file_path=data["file_path"],
            line_start=data.get("line_start"),
            line_end=data.get("line_end"),
        )


class CallEdge(NamedTuple):
    """Represents a function call from source to target."""

    source_urn: str
    target_urn: str
    source_symbol: str
    target_symbol: str
    file_path: str
    line: int | None = None


# ─────────────────────────────────────────────────────────────────────────────
# Snapshot Loading
# ─────────────────────────────────────────────────────────────────────────────


def load_entities(snapshot_path: Path) -> dict[str, Entity]:
    """Load all entities from entities.jsonl."""
    entities = {}
    entities_file = snapshot_path / "entities.jsonl"

    if not entities_file.exists():
        print(f"Warning: No entities.jsonl found in {snapshot_path}")
        return entities

    with open(entities_file) as f:
        for line in f:
            data = json.loads(line)
            entity = Entity.from_jsonl(data)
            entities[entity.urn] = entity

    return entities


def load_call_edges(snapshot_path: Path) -> list[CallEdge]:
    """Load all CALLS edges from facts.jsonl."""
    edges = []
    facts_file = snapshot_path / "facts.jsonl"

    if not facts_file.exists():
        print(f"Warning: No facts.jsonl found in {snapshot_path}")
        return edges

    with open(facts_file) as f:
        for line in f:
            data = json.loads(line)
            if data.get("fact_type") != "CALLS":
                continue

            edge = CallEdge(
                source_urn=data["source_urn"],
                target_urn=data["target_urn"],
                source_symbol=data.get("source_symbol", "unknown"),
                target_symbol=data.get("target_symbol", "unknown"),
                file_path=data.get("file_path", "unknown"),
                line=data.get("line"),
            )
            edges.append(edge)

    return edges


# ─────────────────────────────────────────────────────────────────────────────
# Blast Radius Computation
# ─────────────────────────────────────────────────────────────────────────────


def find_entity_by_symbol(entities: dict[str, Entity], symbol: str) -> Entity | None:
    """Find an entity by its symbol (name)."""
    matches = [e for e in entities.values() if e.name == symbol]

    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]

    # Multiple matches: prefer the most specific one
    print(f"Found {len(matches)} entities named '{symbol}':")
    for i, entity in enumerate(matches[:5], 1):
        print(f"  [{i}] {entity.name} ({entity.entity_type}) in {entity.file_path}")
    if len(matches) > 5:
        print(f"  ... and {len(matches) - 5} more")

    return matches[0]


def compute_blast_radius(
    start_urn: str, edges: list[CallEdge], depth: int = 2, max_results: int = 50
) -> set[str]:
    """Compute all symbols reachable from start_urn via call edges."""
    visited = set()
    to_visit = [(start_urn, 0)]
    affected = set()

    while to_visit and len(affected) < max_results:
        current_urn, current_depth = to_visit.pop(0)

        if current_urn in visited or current_depth > depth:
            continue

        visited.add(current_urn)
        affected.add(current_urn)

        # Find all edges where current_urn is the source (calls TO other functions)
        for edge in edges:
            if edge.source_urn == current_urn and edge.target_urn not in visited:
                to_visit.append((edge.target_urn, current_depth + 1))

    return affected


def build_call_graph_map(edges: list[CallEdge]) -> dict[str, list[CallEdge]]:
    """Build a map from source_urn to all outgoing call edges."""
    graph = defaultdict(list)
    for edge in edges:
        graph[edge.source_urn].append(edge)
    return graph


# ─────────────────────────────────────────────────────────────────────────────
# Output Formatting
# ─────────────────────────────────────────────────────────────────────────────


def print_blast_radius_report(
    start_entity: Entity,
    affected_urns: set[str],
    entities: dict[str, Entity],
    edges: list[CallEdge],
) -> None:
    """Print a formatted blast radius report."""
    print("\n" + "=" * 80)
    print(f"BLAST RADIUS ANALYSIS: {start_entity.name}")
    print("=" * 80)

    print(f"\nTarget:")
    print(f"  Symbol: {start_entity.name}")
    print(f"  Type: {start_entity.entity_type}")
    print(f"  File: {start_entity.file_path}")
    if start_entity.line_start:
        print(f"  Line: {start_entity.line_start}")
    print(f"  Repository: {start_entity.repository}")

    print(f"\nImpact Summary:")
    print(f"  Affected symbols: {len(affected_urns)}")

    if not affected_urns or len(affected_urns) == 1:
        print("\n  No downstream code is directly affected by this symbol.")
        return

    print(f"\nAffected Code (first 20):")
    print("-" * 80)

    affected_entities = []
    for urn in affected_urns:
        if urn in entities:
            affected_entities.append(entities[urn])
        else:
            affected_entities.append(Entity(
                urn=urn,
                entity_type="unknown",
                name="unknown",
                repository="unknown",
                file_path="unknown",
            ))

    for entity in sorted(affected_entities, key=lambda e: e.file_path)[:20]:
        if entity.entity_type == "unknown":
            continue

        location = f"{entity.file_path}"
        if entity.line_start:
            location += f":{entity.line_start}"

        print(f"  • {entity.name:40} {location}")

    if len(affected_entities) > 20:
        print(f"  ... and {len(affected_entities) - 20} more")

    # Show call chains (sample)
    print(f"\nSample Call Chains:")
    print("-" * 80)

    call_graph = build_call_graph_map(edges)
    chains_shown = 0

    for urn in list(affected_urns)[:5]:
        if urn not in call_graph:
            continue

        entity = entities.get(urn)
        if not entity:
            continue

        for edge in call_graph[urn][:3]:
            target = entities.get(edge.target_urn)
            if not target:
                continue

            print(f"  {entity.name:30} → {target.name:30} ({edge.file_path})")
            chains_shown += 1
            if chains_shown >= 10:
                break

        if chains_shown >= 10:
            break


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────


def main() -> int:
    """Main entry point."""
    # Parse arguments
    symbol = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SYMBOL
    snapshot_path_str = sys.argv[2] if len(sys.argv) > 2 else str(DEFAULT_SNAPSHOT)
    snapshot_path = Path(snapshot_path_str)

    # Validate snapshot
    if not snapshot_path.exists():
        print(f"Error: Snapshot not found at {snapshot_path}")
        print(f"\nBuild one first:")
        print(f"  bash examples/01-build/build-kg-single-repo.sh")
        return 1

    print(f"Loading snapshot from {snapshot_path}...")

    # Load data
    entities = load_entities(snapshot_path)
    edges = load_call_edges(snapshot_path)

    print(f"Loaded {len(entities)} entities and {len(edges)} call edges")

    # Find target entity
    print(f"\nSearching for symbol: {symbol}")
    target = find_entity_by_symbol(entities, symbol)

    if not target:
        print(f"Error: Symbol '{symbol}' not found in snapshot")
        print(f"\nHint: Try one of these commands to find a valid symbol:")
        print(f"  supercontext-query-kg --snapshot {snapshot_path} summary | head -20")
        return 1

    print(f"Found: {target.name} ({target.entity_type})")

    # Compute blast radius
    print(f"\nComputing blast radius (depth=2)...")
    affected = compute_blast_radius(target.urn, edges, depth=2, max_results=100)

    # Print report
    print_blast_radius_report(target, affected, entities, edges)

    return 0


if __name__ == "__main__":
    sys.exit(main())
