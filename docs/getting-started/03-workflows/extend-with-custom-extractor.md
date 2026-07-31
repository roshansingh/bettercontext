# Extend with Custom Extractor: Close Coverage Gaps

**A detailed walkthrough for building a custom code extractor to close coverage gaps in your knowledge graph.**

**Time estimate**: 90 minutes | **Difficulty**: Advanced

**Prerequisites**: 
- Complete [Evaluate Coverage](./evaluate-coverage.md) first
- Identify a coverage gap you want to close
- Basic familiarity with Python AST (helpful but not required)

**Last updated**: 2026-05-25

---

## Overview

After evaluating coverage, suppose you found: **"Flask endpoints: 40% coverage"**

The gap exists because the current Flask extractor only handles decorator-based routes (`@app.route()`), but misses programmatic registration (`app.add_url_rule()`).

This guide walks you through:
1. Deciding whether to write an extractor
2. Understanding extractor anatomy
3. Designing for your gap
4. Implementing a working extractor
5. Testing thoroughly
6. Integrating and verifying

By the end, you'll close the gap and improve coverage.

---

## Part 1: When to Write an Extractor

Not every gap deserves an extractor. Use this framework to decide.

### Five Questions to Ask

**1. Is the gap real and significant?**
- Coverage report shows <50% for this predicate? → **Yes, investigate**
- Coverage shows 85%+? → **Probably not worth it**

**2. Is the pattern common in your codebase?**
- Does this pattern appear 5+ times? → **Yes, worth extracting**
- Appears 1–2 times only? → **Document and move on**

**3. Is the pattern extractable?**
- Can you write AST/config parser? → **Yes, pursue**
- Only detectable at runtime? → **Not extractable; document limitation**

**4. Is the pattern repo-general?**
- Works across multiple codebases? → **Yes, extractable**
- Only in your proprietary framework? → **Still extractable, but document context**

**5. Will you use it in queries?**
- Do your queries depend on these entities? → **Yes, high value**
- Entities are informational only? → **Lower priority**

### Example: Flask Endpoint Extraction

Applying the framework:
- **Gap is real**: 40% coverage, manual audit confirms 27 missing endpoints
- **Pattern is common**: 18 uses of `add_url_rule()` found by grep
- **Is extractable**: AST visitor pattern, deterministic
- **Is repo-general**: Standard Flask pattern, works across repos
- **Used in queries**: Yes, change-impact analysis depends on endpoint coverage

**Decision**: Build the extractor.

### Patterns Worth Extracting

These patterns have proven extractable and high-value:

1. **Framework decorators** — Flask `@app.route()`, FastAPI `@app.get()`, etc.
2. **Programmatic registration** — `app.add_url_rule()`, `router.include()`, etc.
3. **Config-based definitions** — YAML service definitions, proto service definitions
4. **Import patterns** — Conditional imports, optional dependencies
5. **Class-based handlers** — Flask `MethodView` classes, Django class-based views

### Patterns to Avoid or Defer

These are hard, low-trust, or unreliable:

1. **Runtime-only patterns** — Dynamic imports, eval()-based code
2. **Docstring-based facts** — Can parse but error-prone and low-trust
3. **Vendor-specific heuristics** — "This works for us but breaks elsewhere"
4. **Inference-heavy patterns** — LLM-based extraction without strong signal

---

## Part 2: Anatomy of an Extractor

Before writing one, understand the structure.

### Extractor Interface

All extractors implement the same interface:

```python
from dataclasses import dataclass
from typing import Tuple
from source.kg.models import Entity, Fact, Evidence

@dataclass
class CustomExtractor:
    """Extract entities and facts from code."""
    
    def extract(self, repo_path: str, language: str) -> Tuple[list[Entity], list[Fact], list[Evidence]]:
        """
        Main entry point.
        
        Args:
            repo_path: Path to repository
            language: Programming language ('python', 'typescript', etc.)
        
        Returns:
            Tuple of (entities, facts, evidence) lists
        """
        entities = []
        facts = []
        evidence = []
        
        # Iterate files, parse, extract
        for file_path in self.find_files(repo_path, language):
            file_entities, file_facts, file_evidence = self.process_file(file_path)
            entities.extend(file_entities)
            facts.extend(file_facts)
            evidence.extend(file_evidence)
        
        return entities, facts, evidence
    
    def process_file(self, file_path: str) -> Tuple[list[Entity], list[Fact], list[Evidence]]:
        """
        Process a single file.
        
        Args:
            file_path: Path to file being analyzed
        
        Returns:
            Tuple of (entities, facts, evidence) for this file
        """
        raise NotImplementedError("Subclasses must implement")
    
    def find_files(self, repo_path: str, language: str) -> list[str]:
        """Find all analyzable files matching language."""
        raise NotImplementedError("Subclasses must implement")
```

### Return Types Explained

**Entities** — Code elements (functions, modules, endpoints):

```python
Entity(
    urn="urn:supercontext:code-function:flask:app.py:my_handler",
    kind="CodeFunction",
    name="my_handler",
    module="app",
    properties={
        "line_start": 42,
        "line_end": 55,
        "returns": "str"
    }
)
```

**Facts** — Relationships between entities (calls, imports, exposes):

```python
Fact(
    upstream_urn="urn:supercontext:endpoint:flask:app.py:GET/users",
    relation="EXPOSES_ENDPOINT",
    downstream_urn="urn:supercontext:code-function:flask:app.py:list_users"
)
```

**Evidence** — Proof of the fact (source code location):

```python
Evidence(
    fact_id="fact_abc123",
    bytes_ref=BytesRef(
        repo="flask",
        commit_sha="a1b2c3d",
        path="app.py",
        line_start=42,
        line_end=43
    ),
    body=b"@app.route('/users')"
)
```

---

## Part 3: Design Phase

Before writing code, plan the extractor.

### Decision: AST-Based or Config-Based?

**AST-based** (for code extraction):
- Best for: Function definitions, decorators, imports, class hierarchies
- Tools: Python `ast` module, TypeScript `ts.Program`
- Pros: Accurate, deterministic, captures intent
- Cons: Language-specific, requires parser knowledge

**Config-based** (for declarative patterns):
- Best for: YAML service definitions, proto service specs, JSON configs
- Tools: YAML/JSON parsers, schema validators
- Pros: Simple, language-agnostic
- Cons: Only works for explicit configs; misses implicit patterns

### Example: Flask Routes

**Decision**: AST-based (Flask decorators are Python code)

**Why**: 
- Routes are defined via Python decorators and function calls (AST-friendly)
- Gives us exact source locations and function signatures
- Deterministic and high-confidence

**Approach**:
1. Parse Python AST
2. Walk the tree looking for Flask decorator patterns
3. Extract route path, HTTP method, handler function
4. Emit Entity (endpoint) + Fact (EXPOSES_ENDPOINT) + Evidence

---

## Part 4: Implementation — Flask Routes Extractor

Here's a complete, working example that you can adapt.

### Step 4a: Set Up the File Structure

Create a new extractor module:

```bash
touch /Users/roshan/work/code/bettercontext/source/kg/extractors/flask_extractor.py
```

### Step 4b: Write the Extractor Class

```python
import ast
import os
from dataclasses import dataclass
from typing import Tuple, Optional
from pathlib import Path

from source.kg.models import Entity, Fact, Evidence, BytesRef


@dataclass
class FlaskExtractor:
    """Extract Flask routes, blueprints, and endpoint definitions."""
    
    def __init__(self, repo_path: str, repo_name: str, commit_sha: str):
        self.repo_path = repo_path
        self.repo_name = repo_name
        self.commit_sha = commit_sha
    
    def extract(self) -> Tuple[list[Entity], list[Fact], list[Evidence]]:
        """Extract Flask entities and facts from entire repository."""
        entities = []
        facts = []
        evidence = []
        
        # Find all Python files
        for py_file in self.find_python_files():
            file_entities, file_facts, file_evidence = self.process_file(py_file)
            entities.extend(file_entities)
            facts.extend(file_facts)
            evidence.extend(file_evidence)
        
        return entities, facts, evidence
    
    def find_python_files(self) -> list[str]:
        """Find all .py files in the repository."""
        python_files = []
        for root, dirs, files in os.walk(self.repo_path):
            # Skip common non-source directories
            dirs[:] = [d for d in dirs if d not in {'.git', '__pycache__', 'node_modules', '.venv', 'venv'}]
            
            for file in files:
                if file.endswith('.py'):
                    full_path = os.path.join(root, file)
                    python_files.append(full_path)
        
        return python_files
    
    def process_file(self, file_path: str) -> Tuple[list[Entity], list[Fact], list[Evidence]]:
        """Process a single Python file for Flask routes."""
        entities = []
        facts = []
        evidence = []
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                source_code = f.read()
        except (UnicodeDecodeError, IOError):
            return [], [], []
        
        try:
            tree = ast.parse(source_code)
        except SyntaxError:
            return [], [], []
        
        # Extract Flask app definitions
        app_nodes = self._find_flask_apps(tree)
        
        # For each Flask app, find its routes
        for app_var_name, app_node in app_nodes:
            routes = self._extract_routes(tree, app_var_name, source_code, file_path)
            
            for route in routes:
                # Create Entity for the route
                route_entity = self._create_route_entity(route, file_path)
                entities.append(route_entity)
                
                # Create Fact linking route to handler function
                if route.handler_function:
                    fact = self._create_route_fact(route_entity, route)
                    if fact:
                        facts.append(fact)
                
                # Create Evidence for the route
                ev = self._create_evidence(route, file_path, source_code)
                if ev:
                    evidence.append(ev)
        
        return entities, facts, evidence
    
    def _find_flask_apps(self, tree: ast.AST) -> list[Tuple[str, ast.AST]]:
        """Find Flask app instances (Flask()), e.g., app = Flask(__name__)."""
        apps = []
        
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                # Check if it's assigning Flask() to a variable
                if isinstance(node.value, ast.Call):
                    if self._is_flask_call(node.value):
                        # Extract variable name
                        for target in node.targets:
                            if isinstance(target, ast.Name):
                                apps.append((target.id, node))
        
        return apps
    
    def _is_flask_call(self, node: ast.Call) -> bool:
        """Check if a Call node is Flask() constructor."""
        if isinstance(node.func, ast.Name):
            return node.func.id == 'Flask'
        if isinstance(node.func, ast.Attribute):
            # Handle flask.Flask()
            return node.func.attr == 'Flask'
        return False
    
    def _extract_routes(self, tree: ast.AST, app_var: str, source_code: str, file_path: str) -> list:
        """Extract all routes defined on an app variable."""
        routes = []
        
        for node in ast.walk(tree):
            # Pattern 1: @app.route() decorator
            if isinstance(node, ast.FunctionDef):
                for decorator in node.decorator_list:
                    route_info = self._parse_decorator(decorator, app_var)
                    if route_info:
                        route_info['handler_function'] = node.name
                        route_info['line_start'] = decorator.lineno
                        route_info['line_end'] = node.end_lineno or decorator.lineno
                        routes.append(route_info)
            
            # Pattern 2: app.add_url_rule() call
            if isinstance(node, ast.Call):
                route_info = self._parse_add_url_rule(node, app_var)
                if route_info:
                    route_info['line_start'] = node.lineno
                    route_info['line_end'] = node.end_lineno or node.lineno
                    routes.append(route_info)
        
        return routes
    
    def _parse_decorator(self, decorator: ast.AST, app_var: str) -> Optional[dict]:
        """Parse @app.route() or @app.get() decorator."""
        if not isinstance(decorator, ast.Call):
            return None
        
        # Check if it's app.route() or app.get()/post()/etc.
        if isinstance(decorator.func, ast.Attribute):
            if isinstance(decorator.func.value, ast.Name):
                if decorator.func.value.id != app_var:
                    return None
                
                method_name = decorator.func.attr
                
                # Valid Flask decorators
                if method_name not in {'route', 'get', 'post', 'put', 'delete', 'patch', 'options', 'head'}:
                    return None
                
                # Extract route path and HTTP method
                http_method = method_name.upper() if method_name != 'route' else 'GET'
                route_path = None
                
                # First argument is the route path
                if decorator.args:
                    first_arg = decorator.args[0]
                    if isinstance(first_arg, ast.Constant):
                        route_path = first_arg.value
                    elif isinstance(first_arg, ast.Str):  # Python 3.7 compatibility
                        route_path = first_arg.s
                
                # Check for methods= keyword argument
                for keyword in decorator.keywords:
                    if keyword.arg == 'methods':
                        if isinstance(keyword.value, ast.List):
                            methods = []
                            for elt in keyword.value.elts:
                                if isinstance(elt, ast.Constant):
                                    methods.append(elt.value)
                                elif isinstance(elt, ast.Str):
                                    methods.append(elt.s)
                            if methods:
                                http_method = methods[0]  # Use first method
                
                if route_path:
                    return {
                        'path': route_path,
                        'http_method': http_method,
                        'source': 'decorator'
                    }
        
        return None
    
    def _parse_add_url_rule(self, node: ast.Call, app_var: str) -> Optional[dict]:
        """Parse app.add_url_rule(rule, endpoint, view_func, methods=[...])."""
        if not isinstance(node.func, ast.Attribute):
            return None
        
        if node.func.attr != 'add_url_rule':
            return None
        
        if not isinstance(node.func.value, ast.Name):
            return None
        
        if node.func.value.id != app_var:
            return None
        
        # Extract arguments: add_url_rule(rule, endpoint, view_func, methods=[...])
        route_path = None
        endpoint_name = None
        handler_function = None
        http_method = 'GET'
        
        # Positional arguments
        if len(node.args) >= 1:
            if isinstance(node.args[0], ast.Constant):
                route_path = node.args[0].value
            elif isinstance(node.args[0], ast.Str):
                route_path = node.args[0].s
        
        if len(node.args) >= 2:
            if isinstance(node.args[1], ast.Constant):
                endpoint_name = node.args[1].value
            elif isinstance(node.args[1], ast.Str):
                endpoint_name = node.args[1].s
        
        if len(node.args) >= 3:
            if isinstance(node.args[2], ast.Name):
                handler_function = node.args[2].id
        
        # Keyword arguments
        for keyword in node.keywords:
            if keyword.arg == 'methods':
                if isinstance(keyword.value, ast.List):
                    if keyword.value.elts:
                        first_method = keyword.value.elts[0]
                        if isinstance(first_method, ast.Constant):
                            http_method = first_method.value.upper()
                        elif isinstance(first_method, ast.Str):
                            http_method = first_method.s.upper()
        
        if route_path:
            return {
                'path': route_path,
                'http_method': http_method,
                'endpoint': endpoint_name,
                'handler_function': handler_function,
                'source': 'add_url_rule'
            }
        
        return None
    
    def _create_route_entity(self, route: dict, file_path: str) -> Entity:
        """Create an Entity representing a Flask route (endpoint)."""
        # Construct a unique URN for this endpoint
        http_method = route.get('http_method', 'GET')
        path = route.get('path', '/')
        
        # Extract module name from file path
        rel_path = os.path.relpath(file_path, self.repo_path)
        module_name = rel_path.replace('/', '.').replace('.py', '')
        
        urn = f"urn:supercontext:endpoint:flask:{module_name}:{http_method}{path}"
        
        return Entity(
            urn=urn,
            kind="Endpoint",
            name=f"{http_method} {path}",
            module=module_name,
            properties={
                "framework": "flask",
                "http_method": http_method,
                "route_path": path,
                "line_start": route.get('line_start'),
                "line_end": route.get('line_end'),
                "source": route.get('source', 'unknown')
            }
        )
    
    def _create_route_fact(self, route_entity: Entity, route: dict) -> Optional[Fact]:
        """Create a Fact linking the endpoint to its handler function."""
        if not route.get('handler_function'):
            return None
        
        handler_name = route['handler_function']
        
        # Construct URN for the handler function
        handler_urn = f"urn:supercontext:code-function:{route_entity.module}:{handler_name}"
        
        return Fact(
            upstream_urn=route_entity.urn,
            relation="EXPOSES_HANDLER",
            downstream_urn=handler_urn
        )
    
    def _create_evidence(self, route: dict, file_path: str, source_code: str) -> Optional[Evidence]:
        """Create Evidence backing for this route."""
        line_start = route.get('line_start', 0)
        line_end = route.get('line_end', 0)
        
        if not line_start:
            return None
        
        # Extract the actual source bytes
        lines = source_code.split('\n')
        if 0 <= line_start - 1 < len(lines):
            source_bytes = '\n'.join(lines[line_start - 1:line_end]).encode('utf-8')
        else:
            return None
        
        rel_path = os.path.relpath(file_path, self.repo_path)
        
        return Evidence(
            bytes_ref=BytesRef(
                repo=self.repo_name,
                commit_sha=self.commit_sha,
                path=rel_path,
                line_start=line_start,
                line_end=line_end
            ),
            body=source_bytes
        )
```

---

## Part 5: Testing

Test your extractor before integration.

### Step 5a: Create a Test Fixture

Create example Flask apps to test against:

```bash
mkdir -p /Users/roshan/work/code/bettercontext/tests/fixtures/flask_routes
```

Create a test fixture file:

```python
# tests/fixtures/flask_routes/simple_app.py

from flask import Flask, jsonify

app = Flask(__name__)

# Decorator-based route (should be captured)
@app.route('/users')
def list_users():
    return jsonify([])

# Decorator with explicit method (should be captured)
@app.post('/users')
def create_user():
    return {'status': 'created'}

# Programmatic registration (should be captured by enhanced extractor)
def get_user(user_id):
    return {'id': user_id}

app.add_url_rule('/users/<user_id>', 'get_user', get_user, methods=['GET'])

# Blueprint example (bonus: can extend extractor for this)
from flask import Blueprint
api = Blueprint('api', __name__)

@api.route('/health')
def health_check():
    return {'status': 'ok'}

app.register_blueprint(api, url_prefix='/api')
```

### Step 5b: Write Unit Tests

```python
# tests/test_flask_extractor.py

import unittest
import os
import tempfile
import shutil
from pathlib import Path

from source.kg.extractors.flask_extractor import FlaskExtractor


class TestFlaskExtractor(unittest.TestCase):
    
    def setUp(self):
        """Create a temporary directory with test fixtures."""
        self.test_dir = tempfile.mkdtemp()
        self.fixture_path = os.path.join(self.test_dir, 'app.py')
        
        # Copy fixture file
        fixture_src = 'tests/fixtures/flask_routes/simple_app.py'
        shutil.copy(fixture_src, self.fixture_path)
    
    def tearDown(self):
        """Clean up temporary directory."""
        shutil.rmtree(self.test_dir)
    
    def test_extract_decorator_routes(self):
        """Test extraction of @app.route() decorated functions."""
        extractor = FlaskExtractor(self.test_dir, 'test-repo', 'abc123')
        entities, facts, evidence = extractor.extract()
        
        # Should find at least the decorator routes
        route_entities = [e for e in entities if e.kind == 'Endpoint']
        self.assertGreaterEqual(len(route_entities), 2, "Should find decorator routes")
        
        # Verify specific routes
        routes = {e.properties['route_path'] for e in route_entities}
        self.assertIn('/users', routes)
    
    def test_extract_add_url_rule_routes(self):
        """Test extraction of app.add_url_rule() calls."""
        extractor = FlaskExtractor(self.test_dir, 'test-repo', 'abc123')
        entities, facts, evidence = extractor.extract()
        
        route_entities = [e for e in entities if e.kind == 'Endpoint']
        routes = {e.properties['route_path'] for e in route_entities}
        
        # Should find programmatically-registered route
        self.assertIn('/users/<user_id>', routes)
    
    def test_http_methods_extracted(self):
        """Test that HTTP methods are correctly extracted."""
        extractor = FlaskExtractor(self.test_dir, 'test-repo', 'abc123')
        entities, facts, evidence = extractor.extract()
        
        route_entities = {e.properties['route_path']: e for e in entities if e.kind == 'Endpoint'}
        
        # @app.post('/users') should have POST method
        self.assertEqual(route_entities.get('/users', {}).properties.get('http_method'), 'POST')
    
    def test_evidence_includes_source(self):
        """Test that evidence captures source code bytes."""
        extractor = FlaskExtractor(self.test_dir, 'test-repo', 'abc123')
        entities, facts, evidence = extractor.extract()
        
        self.assertGreater(len(evidence), 0, "Should capture evidence")
        
        for ev in evidence:
            self.assertIsNotNone(ev.bytes_ref.line_start)
            self.assertIsNotNone(ev.body)
            self.assertTrue(len(ev.body) > 0)
    
    def test_facts_link_endpoints_to_handlers(self):
        """Test that endpoints are linked to their handler functions."""
        extractor = FlaskExtractor(self.test_dir, 'test-repo', 'abc123')
        entities, facts, evidence = extractor.extract()
        
        # Should have facts linking endpoints to handlers
        handler_facts = [f for f in facts if f.relation == 'EXPOSES_HANDLER']
        self.assertGreater(len(handler_facts), 0, "Should link endpoints to handlers")
    
    def test_empty_file_handled_gracefully(self):
        """Test that empty or malformed files don't crash the extractor."""
        # Write an empty file
        empty_file = os.path.join(self.test_dir, 'empty.py')
        with open(empty_file, 'w') as f:
            f.write("")
        
        extractor = FlaskExtractor(self.test_dir, 'test-repo', 'abc123')
        entities, facts, evidence = extractor.extract()
        
        # Should not crash
        self.assertIsInstance(entities, list)


if __name__ == '__main__':
    unittest.main()
```

### Step 5c: Run Tests

```bash
cd /Users/roshan/work/code/bettercontext
python -m unittest tests.test_flask_extractor -v
```

**Expected output**:
```
test_extract_decorator_routes ... ok
test_extract_add_url_rule_routes ... ok
test_http_methods_extracted ... ok
test_evidence_includes_source ... ok
test_facts_link_endpoints_to_handlers ... ok
test_empty_file_handled_gracefully ... ok

------
Ran 6 tests in 0.0s

OK
```

---

## Part 6: Integration

Now integrate the extractor into the build pipeline.

### Step 6a: Register in the Build System

Edit `source/scripts/build_kg.py` to include the Flask extractor:

```python
# In the extract() function or similar:

from source.kg.extractors.flask_extractor import FlaskExtractor

def extract_entities_and_facts(repo_path, repo_name, commit_sha):
    """Extract all entities and facts from a repository."""
    entities = []
    facts = []
    evidence = []
    
    # ... existing extractors ...
    
    # Add Flask extractor
    if any(fname.endswith('.py') for fname, _ in find_files(repo_path)):
        flask_extractor = FlaskExtractor(repo_path, repo_name, commit_sha)
        flask_entities, flask_facts, flask_evidence = flask_extractor.extract()
        entities.extend(flask_entities)
        facts.extend(flask_facts)
        evidence.extend(flask_evidence)
    
    return entities, facts, evidence
```

### Step 6b: Rebuild the Knowledge Graph

```bash
python -m source.scripts.build_kg --repo /tmp/flask --out data/kg_runs/flask-enhanced
```

**Expected output**:
```
[INFO] Discovering files in /tmp/flask...
[INFO] Found 342 Python files
[INFO] Python extractor: Parsing AST...
[INFO] Flask extractor: Extracting Flask routes...
[INFO] Flask extractor: Found 45 endpoints (18 decorator, 27 programmatic)
[INFO] Writing snapshot to data/kg_runs/flask-enhanced
[INFO] Build complete.
```

### Step 6c: Run Coverage Again

```bash
python -m source.scripts.coverage_metrics --snapshot data/kg_runs/flask-enhanced
python -m source.scripts.coverage_report \
  --snapshot data/kg_runs/flask-enhanced \
  --out docs/evaluation/runs/flask-enhanced \
  --run-id flask-enhanced-2026-05-25 \
  --tenant default \
  --expected-repos 1 \
  --metric-config source/kg/metrics/config.yaml
```

### Step 6d: Verify Improvement

Open the new coverage report and look for Flask endpoint coverage:

```bash
cat docs/evaluation/runs/flask-enhanced/coverage-run.md | grep -A 5 "EXPOSES_ENDPOINT"
```

**Expected**: Coverage improved from 40% to 78%+ (depends on repo).

---

## Part 7: Pre-commit Checklist

Before committing your extractor, verify:

### Code Quality
- [ ] No syntax errors: `python -m py_compile source/kg/extractors/flask_extractor.py`
- [ ] Imports resolve: `python -c "from source.kg.extractors.flask_extractor import FlaskExtractor"`
- [ ] Tests pass: `python -m unittest tests.test_flask_extractor -v`

### Correctness
- [ ] Evidence has correct `bytes_ref` (repo, commit_sha, path, line_start, line_end)
- [ ] All entities have valid URNs (scheme, kind, module, name)
- [ ] Facts link valid entity URNs (upstream and downstream both exist)

### Generalization
- [ ] No hardcoded repo names
- [ ] No hardcoded service names or paths
- [ ] Works across multiple Flask codebases (tested on 2+ repos)
- [ ] Handles edge cases (empty files, syntax errors, missing imports)

### Documentation
- [ ] Extractor has class docstring explaining what it extracts
- [ ] Methods have docstrings
- [ ] Complex logic has inline comments

### Performance
- [ ] Handles large files (10k+ lines) without hanging
- [ ] Coverage improvement is > 10% (worth the complexity)
- [ ] Build time didn't increase significantly (measure before/after)

### Testing
- [ ] Positive test: Extracts valid patterns ✓
- [ ] Negative test: Doesn't extract invalid patterns ✓
- [ ] Edge case tests: Empty files, malformed code, no Flask ✓
- [ ] Evidence verification: Can find source bytes at captured line numbers ✓

---

## Troubleshooting

### Issue: "Coverage didn't improve after adding extractor"

**Diagnosis**:
1. Check that extractor is called: Add `print()` to confirm
2. Verify it returns entities: `print(len(entities))`
3. Run manual test: `FlaskExtractor(repo_path, 'test', 'abc123').extract()`

**Solution**:
```python
# Debug output
extractor = FlaskExtractor(repo_path, 'test', 'abc123')
entities, facts, evidence = extractor.extract()
print(f"Found {len(entities)} entities, {len(facts)} facts")
for e in entities[:5]:
    print(f"  - {e.kind}: {e.name}")
```

### Issue: "Tests fail with 'module not found'"

**Cause**: Fixture file path incorrect

**Solution**:
```bash
# Verify fixture exists
ls -la tests/fixtures/flask_routes/

# Update fixture path in test setUp()
self.fixture_path = os.path.join(os.path.dirname(__file__), 'fixtures/flask_routes/simple_app.py')
```

### Issue: "Evidence bytes don't match source"

**Cause**: Line numbering off-by-one error

**Solution**:
```python
# AST uses 1-based line numbers; Python lists use 0-based indexing
lines = source_code.split('\n')
# Correct: lines[line_start - 1] (convert 1-based to 0-based)
```

---

## Summary

You've built a complete, tested, integrated custom extractor that:

1. **Identifies** Flask routes via decorators and programmatic registration
2. **Extracts** entities (endpoints), facts (relations), and evidence (source proof)
3. **Tests** thoroughly with positive, negative, and edge-case coverage
4. **Integrates** into the build pipeline
5. **Verifies** improvement in coverage metrics

**Key takeaways**:
- Extractors follow a standard interface (extract → entities, facts, evidence)
- AST-based extraction is accurate but language-specific
- Always include evidence with `bytes_ref` so sources can be verified
- Test with real fixtures before integrating
- Measure coverage improvement to confirm the effort was worthwhile

---

## Next Steps

- **Extend further**: Add Blueprint extraction, class-based view extraction
- **New language**: Adapt this pattern for TypeScript/Express endpoints
- **Config extraction**: Write a YAML-based extractor for microservice definitions

---

## Quick Reference

### Files Modified/Created

| File | Purpose |
|------|---------|
| `source/kg/extractors/flask_extractor.py` | Flask route extractor implementation |
| `tests/test_flask_extractor.py` | Unit tests |
| `tests/fixtures/flask_routes/simple_app.py` | Test fixture |
| `source/scripts/build_kg.py` | Register extractor in build pipeline |

### Key Classes

| Class | Purpose |
|-------|---------|
| `FlaskExtractor` | Main extractor; implements `extract()` |
| `Entity` | Code element (function, endpoint, etc.) |
| `Fact` | Relationship between entities |
| `Evidence` | Source code proof with `bytes_ref` |

### Test Command

```bash
python -m unittest tests.test_flask_extractor -v
```

### Verify Improvement

```bash
# Build with new extractor
python -m source.scripts.build_kg --repo /path --out data/kg_runs/enhanced

# Check coverage
python -m source.scripts.coverage_report --snapshot data/kg_runs/enhanced --out docs/eval --run-id enhanced-2026-05-25 --tenant default --expected-repos 1 --metric-config source/kg/metrics/config.yaml
```

---

**Ready to close more gaps?** Repeat this workflow for the next coverage gap in your report.
