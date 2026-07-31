# Knowledge Graph: Building and Using SuperContext's Core

**A comprehensive guide to knowledge graphs in SuperContext.** This document explains what knowledge graphs are, how SuperContext builds them, how to query them, and how to extend them with custom extractors.

**Last updated**: 2026-05-25

---

## Part 1: What is a Knowledge Graph?

A **knowledge graph (KG)** is a directed graph of facts extracted from code. Instead of reading source files manually or searching with grep, you query the KG to understand relationships—who calls what, what depends on what, what will break if you change something.

### Nodes and Edges

In SuperContext, a knowledge graph consists of:

**Nodes (Entities)** — The things in your codebase:
- `CodeSymbol`: A function, method, or class (e.g., `payments.charge()`)
- `CodeModule`: A file or package boundary (e.g., `payments/invoice.py`)
- `Service`: A deployed unit (e.g., `pricing-service`)
- `Endpoint`: An HTTP or gRPC route (e.g., `POST /api/v1/charges`)
- `EventChannel`: A message topic or queue (e.g., `payments.order_created`)
- `ExternalPackage`: A library your code depends on (e.g., `requests`)
- `Repo`: The repository itself
- `Domain`: A business domain grouping services

**Edges (Facts/Relations)** — How they connect:
- `CALLS`: Function A calls function B
- `IMPORTS`: Module A imports from module B
- `HOSTS`: Service A hosts endpoint B
- `PUBLISHES`: Service A publishes to event channel B
- `CONSUMES`: Service A consumes from event channel B
- `DEPENDS_ON`: Service A depends on service B
- `DEFINED_IN`: A symbol is defined in a module

Every fact includes **evidence**: the commit hash, file path, and line numbers where the fact was observed. This makes answers verifiable.

### Why Knowledge Graphs Matter

**1. Change Safety** — Before merging a change, query the KG: "If I modify this function, what breaks?" The graph shows you the transitive call chain of everything that would be affected. No more surprises in production.

**2. Dependency Analysis** — Understand your service landscape. Which services would be impacted if you change an API? What internal packages have the most blast radius? The KG makes these questions answerable in seconds.

**3. Coverage Understanding** — The KG tells you what was extracted and what wasn't. Coverage metrics show which services, languages, and patterns are instrumented. Use this to identify where to focus extraction work.

**4. Onboarding** — New engineers need to understand the dependency landscape. Instead of tribal knowledge and manual documentation, they query the KG to see how services interact, what the critical paths are, and where risks lie.

### Example: Flask Application

Consider a simple Flask application:

```python
# app.py
from flask import Flask
from auth import authenticate
from db import get_user

app = Flask(__name__)

@app.route('/api/user/<user_id>')
def get_user_info(user_id):
    token = authenticate()
    user = get_user(user_id)
    return {"id": user_id, "name": user.name}

if __name__ == '__main__':
    app.run()
```

```python
# auth.py
def authenticate():
    """Verify the current request."""
    return "token_12345"
```

```python
# db.py
def get_user(user_id):
    """Fetch user from database."""
    return {"id": user_id, "name": "Alice"}
```

The KG extracts these facts (simplified):

| Entity 1 | Relation | Entity 2 | Evidence |
|----------|----------|----------|----------|
| `app.get_user_info` | CALLS | `auth.authenticate` | app.py:11 |
| `app.get_user_info` | CALLS | `db.get_user` | app.py:12 |
| `app` | IMPORTS | `auth` | app.py:2 |
| `app` | IMPORTS | `db` | app.py:3 |
| Endpoint `GET /api/user/<user_id>` | HOSTS | Service `flask-app` | app.py:8 (decorator) |

Now you can query:

**Query 1**: "Who calls `get_user`?"
```
db.get_user is called by:
  - app.get_user_info (app.py:12)
```

**Query 2**: "What breaks if I change the signature of `authenticate()`?"
```
Blast radius for auth.authenticate:
  ├─ app.get_user_info (calls authenticate)
  │  └─ Endpoint GET /api/user/<user_id> (hosted by flask-app)
```

**Query 3**: "What imports the auth module?"
```
Modules importing auth:
  - app (app.py:2)
```

These queries answer in milliseconds instead of hours of manual investigation.

---

## Part 2: How SuperContext Builds a Knowledge Graph

Building a KG happens in three steps: **extraction**, **storage**, and **snapshot**.

### Step 1: Extraction

An **extractor** is a language-specific program that analyzes source code and emits facts. SuperContext includes extractors for Python and TypeScript/JavaScript. Each extractor:

1. **Parses** the code into an Abstract Syntax Tree (AST)
2. **Walks** the tree to find symbols, imports, calls, decorators, and other patterns
3. **Emits** structured facts: entities, facts, evidence, and coverage rows

#### Python Extractor Flow

The Python extractor (`source/kg/languages/python/extractors/ast_extractor.py`) works like this:

```python
import ast
from source.kg.core.models import Entity, Fact, Evidence

# Step 1: Parse Python files into AST
code = "def authenticate(): return 'token'"
tree = ast.parse(code)

# Step 2: Walk the tree and collect symbols
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef):
        # Found a function definition
        symbol_name = node.name
        line_number = node.lineno
        # Emit entity: CodeSymbol
        entity = Entity(
            kind="CodeSymbol",
            identity={
                "repo": "my-repo",
                "module": "auth",
                "qualname": "authenticate",
                "symbol_kind": "function"
            },
            properties={"lineno": line_number}
        )
        
    if isinstance(node, ast.Call):
        # Found a function call
        # Emit fact: CALLS relation
        if isinstance(node.func, ast.Name):
            called_name = node.func.id
            # Fact connecting caller to callee
```

#### TypeScript Extractor Flow

The TypeScript extractor works similarly but parses JavaScript/TypeScript using a language-aware parser, then emits the same entity/fact/evidence structures.

#### Evidence Capture

When extracting, every fact includes **bytes_ref** for verifiable evidence:

```python
evidence = Evidence(
    target_type="fact",
    target_id=fact.fact_id,
    derivation_class="deterministic_static",
    source_system="python_ast_v0",
    source_ref={"pattern": "function_call"},
    bytes_ref={
        "repo": "my-repo",
        "commit_sha": "abc123def456",
        "path": "auth.py",
        "line_start": 11,
        "line_end": 11
    }
)
```

The `bytes_ref` makes every fact auditable: you can always fetch the exact code that proved a relationship exists.

### Step 2: Storage

Once extracted, facts are stored in **JSONL format** (one JSON object per line). A KG snapshot writes five files:

#### entities.jsonl

Each line is a JSON Entity:

```json
{
  "entity_id": "ent_abc123xyz789",
  "kind": "CodeSymbol",
  "identity": {
    "repo": "payment-service",
    "module": "payments.charge",
    "qualname": "charge_customer",
    "symbol_kind": "function"
  },
  "properties": {
    "lineno": 42,
    "is_exported": true
  },
  "canonical_status": "canonical",
  "urn": "supercontext://code-symbol/payment-service/payments.charge/charge_customer/function"
}
```

#### facts.jsonl

Each line is a JSON Fact (a relation between two entities):

```json
{
  "fact_id": "fact_def456abc123",
  "predicate": "CALLS",
  "subject_id": "ent_abc123xyz789",
  "object_id": "ent_def456ghi789",
  "qualifier": {
    "api": "direct_call",
    "line": 45
  },
  "canonical_status": "canonical"
}
```

The `subject_id` and `object_id` refer to entity IDs in entities.jsonl. The `predicate` tells you the relation type (CALLS, IMPORTS, HOSTS, etc.).

#### evidence.jsonl

Each line proves a fact or entity exists:

```json
{
  "evidence_id": "ev_xyz789def456",
  "target_type": "fact",
  "target_id": "fact_def456abc123",
  "derivation_class": "deterministic_static",
  "source_system": "python_ast_v0",
  "source_ref": {
    "pattern": "direct_function_call"
  },
  "bytes_ref": {
    "repo": "payment-service",
    "commit_sha": "9f3e2b1d8c7a6e5f4d3c2b1a0f9e8d7c",
    "path": "payments/charge.py",
    "line_start": 45,
    "line_end": 45
  },
  "confidence": 1.0,
  "ingested_at": "2026-05-25T10:30:00"
}
```

#### coverage.jsonl

Tracks what was extracted and what gaps exist:

```json
{
  "coverage_id": "cov_coverage123",
  "tenant_id": "default",
  "predicate": "CALLS",
  "scope_ref": {
    "repo": "payment-service",
    "language": "python"
  },
  "state": "instrumented",
  "source_system": "python_ast_v0",
  "checked_at": "2026-05-25T10:30:00"
}
```

States include:
- `instrumented`: This pattern is extracted (e.g., "Python function calls are extracted")
- `partially_instrumented`: Some cases extracted, some gaps (e.g., "Flask routes partially supported")
- `uninstrumented`: No extraction (e.g., "Ruby code not supported")
- `stale`: Extraction was supported but is outdated

#### manifest.json

Metadata about the snapshot:

```json
{
  "snapshot_id": "my-repo-2026-05-25",
  "created_at": "2026-05-25T10:30:00Z",
  "repo": "payment-service",
  "commit_sha": "9f3e2b1d8c7a6e5f4d3c2b1a0f9e8d7c",
  "extractors": [
    {
      "name": "python_ast_v0",
      "version": "1.0.0"
    }
  ],
  "tenant_id": "default",
  "entity_count": 342,
  "fact_count": 1847,
  "evidence_count": 1847
}
```

### Step 3: Immutable Snapshot

Once built, a KG snapshot **never changes**. It's pinned to a specific git commit and stored on disk. This immutability makes snapshots:

- **Reproducible**: Build the same repo and commit twice, get identical KGs
- **Auditable**: Every fact has bytes_ref so you can verify it
- **Queryable**: Load a snapshot and query it without worrying about data changing underneath

You can store multiple snapshots (e.g., one per week, or one per release) and compare them to see how the dependency landscape evolves.

---

## Part 3: Using the Knowledge Graph

### Building a Knowledge Graph

To extract a KG from your code, use the `build_kg` command:

```bash
# Single repository
python -m source.scripts.build_kg \
  --repo /path/to/my-repo \
  --out data/kg_runs/my-repo

# Multiple repositories (fleet view)
python -m source.scripts.build_multi_kg \
  --repo /path/to/repo-1 \
  --repo /path/to/repo-2 \
  --out data/kg_runs/fleet
```

The command:
1. Scans the repo for source files (Python, TypeScript, etc.)
2. Runs the appropriate extractors (Python AST, TS/JS parser, etc.)
3. Writes the five snapshot files to `--out`

After the build completes, the snapshot directory contains `entities.jsonl`, `facts.jsonl`, `evidence.jsonl`, `coverage.jsonl`, and `manifest.json`.

### Querying the Knowledge Graph

Query the snapshot using the `query_kg` command. Common queries:

**View a summary of the snapshot:**
```bash
python -m source.scripts.query_kg --snapshot data/kg_runs/my-repo summary
```

Output:
```
Snapshot Summary
================
Created at: 2026-05-25T10:30:00Z
Commit: 9f3e2b1d8c7a6e5f4d3c2b1a0f9e8d7c
Entities: 342 (12 CodeSymbol, 45 CodeModule, 3 Service, ...)
Facts: 1847 (1203 CALLS, 401 IMPORTS, 89 HOSTS, ...)
Evidence: 1847 (all deterministic_static)
```

**Find all callers of a function:**
```bash
python -m source.scripts.query_kg --snapshot data/kg_runs/my-repo \
  find-callers payments.charge.charge_customer --limit 5
```

Output:
```
Callers of payments.charge.charge_customer:
  1. order_handler (in order_processor.py:78)
  2. webhook_handler (in webhooks.py:105)
  3. retry_job (in jobs/retry.py:34)
Evidence: 3 deterministic_static
```

**Find the blast radius (transitive impact):**
```bash
python -m source.scripts.query_kg --snapshot data/kg_runs/my-repo \
  blast-radius payments.charge.charge_customer --depth 2
```

Output:
```
Blast radius for payments.charge.charge_customer (depth 2):
├─ order_handler
│  ├─ order_created_handler
│  │  └─ Service: order-service
├─ webhook_handler
│  └─ Service: webhook-service
└─ retry_job
   ├─ failed_charge_handler
   └─ Service: async-worker
```

**Find modules importing a package:**
```bash
python -m source.scripts.query_kg --snapshot data/kg_runs/my-repo \
  modules-importing requests --limit 5
```

**Get top-level dependencies:**
```bash
python -m source.scripts.query_kg --snapshot data/kg_runs/my-repo \
  top-dependencies --limit 10
```

For complete query reference, see [querying.md](./querying.md).

### Understanding Coverage

Coverage metrics tell you what was extracted and where gaps exist. Coverage is tracked per:
- **Predicate** (e.g., "Python CALLS relations")
- **Scope** (e.g., "Python files in payment-service")
- **State** (instrumented, partially_instrumented, uninstrumented)

Run coverage metrics:

```bash
python -m source.scripts.coverage_metrics \
  --snapshot data/kg_runs/my-repo \
  --expected-repos 1
```

This generates `metrics.jsonl` in the snapshot directory. Each row is a coverage finding:

```json
{
  "tenant_id": "default",
  "predicate": "CALLS",
  "scope_ref": {"repo": "payment-service", "language": "python"},
  "state": "instrumented",
  "metrics": {
    "entities_found": 342,
    "relations_found": 1203,
    "coverage_pct": 87.5
  }
}
```

A report of 87.5% coverage means: "Of the functions/modules we expected to find based on language analysis, we successfully extracted 87.5% of the call relationships."

Gaps (uninstrumented regions) might indicate:
- Language not yet supported (e.g., Ruby, Go)
- Framework not yet recognized (e.g., custom decorators, DSLs)
- Dynamic code paths (inherently hard to extract statically)

For complete coverage interpretation, see [coverage-metrics.md](./coverage-metrics.md).

---

## Part 4: Writing a Custom Extractor

If coverage analysis shows a gap—a pattern that's common and extractable but not yet supported—you can write a custom extractor.

### When to Write One

Write a custom extractor when:

1. **Coverage analysis identified a gap** — You ran `coverage_metrics` and saw `state="uninstrumented"` for a pattern you need
2. **The pattern is common** — It appears in many files or many repos (not a one-off)
3. **The pattern is extractable** — You can recognize it deterministically via AST, regex, or config parsing
4. **The pattern has clear semantics** — You know what entity/fact/evidence to emit

**Examples of extractable patterns:**
- Flask route decorators (`@app.route(...)` → Endpoint entity, HOSTS fact)
- Celery tasks (`@celery.task()` → CodeSymbol with property, PUBLISHES to queue)
- Config file service definitions (YAML/TOML defining services → Service entities)
- Dataclass imports (determining which libraries provide distributed tracing)

**Examples of non-extractable patterns:**
- Comments describing dependencies (unreliable, not semantic)
- Dynamic code (`exec()`, reflection) that's impossible to analyze statically
- Implicit dependencies from framework magic that requires runtime introspection

### Anatomy of an Extractor

An extractor is a Python class with a standard structure:

```python
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from source.kg.core.models import Entity, Fact, Evidence, Coverage, JsonObject
from source.kg.core.repo_source import RepoSnapshot

@dataclass
class KgBuild:
    """Accumulator for extraction results."""
    entities: list[Entity] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    coverage: list[Coverage] = field(default_factory=list)


class MyCustomExtractor:
    """Extract facts from your framework."""
    
    source_system = "my_extractor_v0"  # Unique ID for this extractor
    
    def extract(self, repo: RepoSnapshot) -> KgBuild:
        """
        Main entry point.
        
        Args:
            repo: Snapshot of the repository to analyze
            
        Returns:
            KgBuild with extracted entities, facts, evidence, coverage
        """
        build = KgBuild()
        
        # Scan files and extract facts
        for file_path in repo.source_files():
            # Parse the file
            # Emit entities and facts
            pass
        
        # Emit a coverage row (required)
        build.coverage.append(Coverage(
            tenant_id="default",
            predicate="HOSTS",  # What predicate you extract
            scope_ref={"repo": repo.name, "framework": "my-framework"},
            state="instrumented",
            source_system=self.source_system
        ))
        
        return build
```

### Step-by-Step Example: Flask Routes

Here's a complete example extracting Flask route decorators into Endpoint entities and HOSTS facts.

**Step 1: Find Flask route decorators**

```python
import ast
from pathlib import Path

class FlaskRouteExtractor:
    source_system = "flask_routes_v0"
    
    def _find_flask_decorators(self, file_path: Path) -> list[tuple[str, str, int]]:
        """
        Find @app.route(...) and @blueprint.route(...) decorators.
        
        Returns list of (route_path, http_method, line_number).
        """
        with open(file_path) as f:
            tree = ast.parse(f.read())
        
        routes = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            
            for decorator in node.decorator_list:
                # Match: @app.route('/path', methods=['POST'])
                if isinstance(decorator, ast.Call):
                    if isinstance(decorator.func, ast.Attribute):
                        if decorator.func.attr == 'route':
                            # Extract path from first arg
                            if decorator.args:
                                path_node = decorator.args[0]
                                if isinstance(path_node, ast.Constant):
                                    path = path_node.value
                                    
                                    # Extract methods from keyword args
                                    method = 'GET'  # default
                                    for keyword in decorator.keywords:
                                        if keyword.arg == 'methods':
                                            # Handle methods=['POST', 'PUT']
                                            if isinstance(keyword.value, ast.List):
                                                methods = [
                                                    elt.value for elt in keyword.value.elts
                                                    if isinstance(elt, ast.Constant)
                                                ]
                                                if methods:
                                                    method = methods[0]
                                    
                                    routes.append((path, method, decorator.lineno))
        
        return routes
```

**Step 2: Emit entities and facts**

```python
def extract(self, repo: RepoSnapshot) -> KgBuild:
    build = KgBuild()
    tenant_id = "default"
    
    # Get or create Service entity for this repo
    service_entity = Entity(
        kind="Service",
        identity={
            "tenant_id": tenant_id,
            "namespace": "default",
            "repo": repo.name,
            "slug": repo.name.replace("-", "_")
        },
        properties={"framework": "flask"}
    )
    build.entities.append(service_entity)
    
    # Find all Flask files
    for file_path in repo.source_files(extensions=[".py"]):
        routes = self._find_flask_decorators(file_path)
        
        for route_path, method, lineno in routes:
            # Create Endpoint entity
            endpoint_entity = Entity(
                kind="Endpoint",
                identity={
                    "tenant_id": tenant_id,
                    "repo": repo.name,
                    "protocol": "http",
                    "method": method,
                    "path": route_path,
                    "host": None
                },
                properties={
                    "framework": "flask",
                    "file": str(file_path),
                    "lineno": lineno
                }
            )
            build.entities.append(endpoint_entity)
            
            # Create HOSTS fact
            fact = Fact(
                predicate="HOSTS",
                subject_id=service_entity.entity_id,
                object_id=endpoint_entity.entity_id,
                qualifier={
                    "framework": "flask",
                    "decorator": "@app.route"
                }
            )
            build.facts.append(fact)
            
            # Create evidence
            evidence = Evidence(
                target_type="fact",
                target_id=fact.fact_id,
                derivation_class="deterministic_static",
                source_system=self.source_system,
                source_ref={"pattern": "flask_route_decorator"},
                bytes_ref={
                    "repo": repo.name,
                    "commit_sha": repo.commit_sha,
                    "path": str(file_path),
                    "line_start": lineno,
                    "line_end": lineno
                }
            )
            build.evidence.append(evidence)
    
    # Coverage row (required)
    build.coverage.append(Coverage(
        tenant_id=tenant_id,
        predicate="HOSTS",
        scope_ref={
            "repo": repo.name,
            "framework": "flask"
        },
        state="instrumented",
        source_system=self.source_system
    ))
    
    return build
```

### Testing Your Extractor

Write unit tests to validate extraction correctness:

```python
import unittest
from pathlib import Path
import tempfile
from source.kg.core.repo_source import RepoSnapshot
from my_extractor import FlaskRouteExtractor

class FlaskRouteExtractorTest(unittest.TestCase):
    
    def test_basic_flask_route(self) -> None:
        """Test that @app.route('/path') emits Endpoint + HOSTS."""
        source = (
            "from flask import Flask\n"
            "app = Flask(__name__)\n\n"
            "@app.route('/users/<id>')\n"
            "def get_user(id):\n"
            "    return {'id': id}\n"
        )
        
        build = self._extract_source(source)
        
        # Check entities
        endpoints = [e for e in build.entities if e.kind == "Endpoint"]
        self.assertEqual(len(endpoints), 1)
        self.assertEqual(endpoints[0].identity["path"], "/users/<id>")
        self.assertEqual(endpoints[0].identity["method"], "GET")
        
        # Check facts
        hosts_facts = [f for f in build.facts if f.predicate == "HOSTS"]
        self.assertEqual(len(hosts_facts), 1)
        self.assertEqual(hosts_facts[0].object_id, endpoints[0].entity_id)
        
        # Check evidence
        evidence_list = [e for e in build.evidence if e.target_id == hosts_facts[0].fact_id]
        self.assertEqual(len(evidence_list), 1)
        self.assertEqual(evidence_list[0].derivation_class, "deterministic_static")
        self.assertIsNotNone(evidence_list[0].bytes_ref)
    
    def test_post_route_with_methods_kwarg(self) -> None:
        """Test methods=['POST', 'PUT'] keyword argument."""
        source = (
            "app = Flask(__name__)\n\n"
            "@app.route('/orders', methods=['POST', 'PUT'])\n"
            "def create_or_update_order():\n"
            "    return {}\n"
        )
        
        build = self._extract_source(source)
        
        endpoints = [e for e in build.entities if e.kind == "Endpoint"]
        # Extracts first method in the list
        self.assertEqual(endpoints[0].identity["method"], "POST")
        self.assertEqual(endpoints[0].identity["path"], "/orders")
    
    def test_blueprint_route(self) -> None:
        """Test blueprint routes: @bp.route(...)"""
        source = (
            "from flask import Blueprint\n"
            "bp = Blueprint('items', __name__)\n\n"
            "@bp.route('/items')\n"
            "def list_items():\n"
            "    return []\n"
        )
        
        build = self._extract_source(source)
        
        endpoints = [e for e in build.entities if e.kind == "Endpoint"]
        self.assertEqual(len(endpoints), 1)
        self.assertEqual(endpoints[0].identity["path"], "/items")
    
    def test_coverage_row_emitted(self) -> None:
        """Test that coverage row is always emitted."""
        build = self._extract_source("# empty")
        
        coverage_rows = [c for c in build.coverage if c.predicate == "HOSTS"]
        self.assertEqual(len(coverage_rows), 1)
        self.assertEqual(coverage_rows[0].state, "instrumented")
    
    def _extract_source(self, source: str) -> KgBuild:
        """Helper: Create a temp repo and extract."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "app.py"
            file_path.write_text(source)
            
            repo = RepoSnapshot(
                root=Path(tmpdir),
                name="test-repo",
                commit_sha="abc123"
            )
            
            extractor = FlaskRouteExtractor()
            return extractor.extract(repo)


if __name__ == '__main__':
    unittest.main()
```

### Integration Checklist

Before committing your extractor, verify:

- [ ] **Extraction logic is correct** — Positive test cases pass, negative cases are handled
- [ ] **Edge cases covered** — Decorators with kwargs, missing args, nested structures, etc.
- [ ] **Evidence is complete** — Every fact has `bytes_ref` with repo, commit, path, and line numbers
- [ ] **Coverage row emitted** — Coverage row is always emitted (even if zero facts extracted)
- [ ] **Tenant ID respected** — Uses `resolve_tenant_id()` or context parameter, not hardcoded
- [ ] **Entity URNs are stable** — Same code always produces same entity/fact IDs
- [ ] **No hardcoded repo/service names** — Extractors must be repo-general
- [ ] **Tests include positive cases** — Prove the pattern is found and extracted correctly
- [ ] **Tests include negative cases** — Prove incorrect patterns are skipped
- [ ] **Documentation in source code** — Docstrings explain what the extractor does
- [ ] **Registered in extractor adapter** — Added to the list of active extractors
- [ ] **Performance acceptable** — Extraction completes in reasonable time for large repos

---

## Cross-References & Further Reading

**How to query**: [querying.md](./querying.md) — Complete query tool reference and syntax

**Understand coverage**: [coverage-metrics.md](./coverage-metrics.md) — Coverage metric definitions, interpretation, and improvement strategies

**Full architecture**: 
- [ADR-0006: Canonical Ontology and Fact Metadata Envelope](../../../adr/0006-canonical-ontology-and-fact-metadata-envelope.md)
- [ADR-0005: Evidence Retrieval](../../../adr/0005-modular-evidence-retrieval-with-coordinate-fetch-and-selective-ladder.md)

**Build your first KG**: [setup-and-first-kg.md](../03-workflows/setup-and-first-kg.md) — Step-by-step guide to installing and building a KG

---

**Last updated**: 2026-05-25
