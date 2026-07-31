#!/usr/bin/env python3
"""
Test Template for SuperContext Extractors

This module demonstrates how to write comprehensive tests for custom extractors.
It uses Python's unittest framework to validate:

1. Positive tests: Extract expected entities/facts from valid patterns
2. Negative tests: Do NOT extract invalid patterns (false positives)
3. Edge case tests: Handle malformed input, missing fields, nested patterns

To adapt this for your own extractor:
1. Copy this file
2. Replace imports with your extractor class
3. Create test fixtures (temporary directories with sample code)
4. Write test methods following the pattern below

Usage:
    python -m unittest extractor_test_template.py
    python extractor_test_template.py  # Direct execution
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

# Import your extractor here
# from flask_routes_extractor import FlaskRoutesExtractor


# ─────────────────────────────────────────────────────────────────────────────
# Test Fixtures: Sample Code Snippets
# ─────────────────────────────────────────────────────────────────────────────


VALID_FLASK_ROUTE = """
from flask import Flask

app = Flask(__name__)

@app.route('/api/users')
def list_users():
    return []

@app.route('/api/users/<int:id>', methods=['GET', 'DELETE'])
def get_user(id):
    return {"id": id}
"""

MULTIPLE_METHODS_ROUTE = """
from flask import Flask

app = Flask(__name__)

@app.route('/api/items', methods=['GET', 'POST', 'PUT'])
def manage_items():
    return []
"""

BLUEPRINT_ROUTE = """
from flask import Blueprint

bp = Blueprint('items', __name__, url_prefix='/api')

@bp.route('/items')
def list_items():
    return []

@bp.route('/items/<int:id>')
def get_item(id):
    return {"id": id}
"""

INVALID_DECORATOR = """
from flask import Flask

app = Flask(__name__)

@app.cached(timeout=300)  # Not a route
def cached_func():
    return "cached"

@some_other_decorator()  # Not related to Flask
def other_func():
    pass
"""

MALFORMED_ROUTE = """
from flask import Flask

app = Flask(__name__)

@app.route()  # Missing path argument
def no_path():
    return []

@app.route(123)  # Path is not a string
def bad_path():
    return []
"""

NESTED_ROUTE = """
from flask import Flask

app = Flask(__name__)

def outer():
    @app.route('/nested')
    def nested():
        return []
    return nested
"""

EMPTY_METHODS = """
from flask import Flask

app = Flask(__name__)

@app.route('/api/test', methods=[])
def test():
    return []
"""


# ─────────────────────────────────────────────────────────────────────────────
# Test Suite
# ─────────────────────────────────────────────────────────────────────────────


class TestExtractor(unittest.TestCase):
    """
    Test suite for a custom extractor.

    Example: FlaskRoutesExtractor tests

    Structure:
      - setUp: Create temp directory and fixture files
      - tearDown: Clean up temp directory
      - test_positive_*: Extract expected patterns
      - test_negative_*: Do NOT extract invalid patterns
      - test_edge_*: Handle corner cases
    """

    def setUp(self) -> None:
        """Create a temporary directory and sample files for testing."""
        self.temp_dir = tempfile.mkdtemp()
        self.original_dir = os.getcwd()

    def tearDown(self) -> None:
        """Clean up temporary directory."""
        os.chdir(self.original_dir)
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    # ─────────────────────────────────────────────────────────────────────────
    # POSITIVE TESTS: Should find expected patterns
    # ─────────────────────────────────────────────────────────────────────────

    def test_finds_simple_route(self) -> None:
        """Test that extractor finds a simple @app.route() decorator."""
        # TODO: Adapt this for your extractor
        # 1. Create a fixture file
        fixture_file = os.path.join(self.temp_dir, "simple.py")
        with open(fixture_file, "w") as f:
            f.write(VALID_FLASK_ROUTE)

        # 2. Run the extractor
        # extractor = FlaskRoutesExtractor(repo_path=self.temp_dir)
        # result = extractor.extract()

        # 3. Assert expected entities/facts exist
        # self.assertGreater(len(result.entities), 0)
        # self.assertTrue(
        #     any(
        #         e.identity.get("path") == "/api/users"
        #         for e in result.entities
        #     )
        # )

    def test_extracts_multiple_methods(self) -> None:
        """Test that extractor correctly extracts multiple HTTP methods."""
        # TODO: Implement
        fixture_file = os.path.join(self.temp_dir, "methods.py")
        with open(fixture_file, "w") as f:
            f.write(MULTIPLE_METHODS_ROUTE)

        # extractor = FlaskRoutesExtractor(repo_path=self.temp_dir)
        # result = extractor.extract()

        # Should find 3 entities (GET, POST, PUT for same path)
        # self.assertEqual(len(result.entities), 3)

    def test_extracts_blueprint_routes(self) -> None:
        """Test that extractor finds routes on blueprints."""
        # TODO: Implement
        fixture_file = os.path.join(self.temp_dir, "blueprint.py")
        with open(fixture_file, "w") as f:
            f.write(BLUEPRINT_ROUTE)

        # extractor = FlaskRoutesExtractor(repo_path=self.temp_dir)
        # result = extractor.extract()

        # self.assertGreater(len(result.entities), 0)

    def test_handles_path_parameters(self) -> None:
        """Test that extractor preserves path parameters like <int:id>."""
        # TODO: Implement
        fixture_file = os.path.join(self.temp_dir, "params.py")
        with open(fixture_file, "w") as f:
            f.write(VALID_FLASK_ROUTE)

        # extractor = FlaskRoutesExtractor(repo_path=self.temp_dir)
        # result = extractor.extract()

        # self.assertTrue(
        #     any(
        #         "<int:id>" in e.identity.get("path", "")
        #         for e in result.entities
        #     )
        # )

    # ─────────────────────────────────────────────────────────────────────────
    # NEGATIVE TESTS: Should NOT find invalid patterns
    # ─────────────────────────────────────────────────────────────────────────

    def test_ignores_non_route_decorators(self) -> None:
        """Test that extractor ignores decorators that aren't routes."""
        # TODO: Implement
        fixture_file = os.path.join(self.temp_dir, "invalid.py")
        with open(fixture_file, "w") as f:
            f.write(INVALID_DECORATOR)

        # extractor = FlaskRoutesExtractor(repo_path=self.temp_dir)
        # result = extractor.extract()

        # Should find nothing
        # self.assertEqual(len(result.entities), 0)

    def test_rejects_malformed_routes(self) -> None:
        """Test that extractor gracefully handles malformed routes."""
        # TODO: Implement
        fixture_file = os.path.join(self.temp_dir, "malformed.py")
        with open(fixture_file, "w") as f:
            f.write(MALFORMED_ROUTE)

        # extractor = FlaskRoutesExtractor(repo_path=self.temp_dir)
        # result = extractor.extract()

        # Should skip malformed routes, not crash
        # self.assertEqual(len(result.entities), 0)

    def test_no_false_positives_in_comments(self) -> None:
        """Test that extractor doesn't match routes in comments."""
        # TODO: Implement
        code_with_comment = """
# This file has routes in comments:
# @app.route('/fake/route')

from flask import Flask

app = Flask(__name__)

@app.route('/real/route')
def real():
    return []
"""
        fixture_file = os.path.join(self.temp_dir, "comments.py")
        with open(fixture_file, "w") as f:
            f.write(code_with_comment)

        # extractor = FlaskRoutesExtractor(repo_path=self.temp_dir)
        # result = extractor.extract()

        # Should find only 1 route (the real one)
        # self.assertEqual(len(result.entities), 1)

    # ─────────────────────────────────────────────────────────────────────────
    # EDGE CASE TESTS: Handle unusual but valid patterns
    # ─────────────────────────────────────────────────────────────────────────

    def test_handles_nested_functions(self) -> None:
        """Test that extractor finds routes even in nested functions."""
        # TODO: Implement
        fixture_file = os.path.join(self.temp_dir, "nested.py")
        with open(fixture_file, "w") as f:
            f.write(NESTED_ROUTE)

        # extractor = FlaskRoutesExtractor(repo_path=self.temp_dir)
        # result = extractor.extract()

        # May or may not find nested routes depending on implementation
        # Just ensure it doesn't crash:
        # self.assertIsNotNone(result)

    def test_handles_empty_methods_list(self) -> None:
        """Test that extractor handles methods=[] gracefully."""
        # TODO: Implement
        fixture_file = os.path.join(self.temp_dir, "empty_methods.py")
        with open(fixture_file, "w") as f:
            f.write(EMPTY_METHODS)

        # extractor = FlaskRoutesExtractor(repo_path=self.temp_dir)
        # result = extractor.extract()

        # Should either skip or use default method (GET)
        # self.assertIsNotNone(result)

    def test_handles_syntax_errors(self) -> None:
        """Test that extractor gracefully skips files with syntax errors."""
        # TODO: Implement
        fixture_file = os.path.join(self.temp_dir, "syntax_error.py")
        with open(fixture_file, "w") as f:
            f.write("this is not valid python }{]")

        # extractor = FlaskRoutesExtractor(repo_path=self.temp_dir)
        # Should not crash:
        # result = extractor.extract()
        # self.assertIsNotNone(result)

    def test_handles_large_codebase(self) -> None:
        """Test that extractor handles a codebase with many files."""
        # TODO: Implement
        # Create 10 Python files with routes
        for i in range(10):
            fixture_file = os.path.join(self.temp_dir, f"module_{i}.py")
            with open(fixture_file, "w") as f:
                f.write(f"""
from flask import Flask
app = Flask(__name__)

@app.route('/route{i}')
def handler{i}():
    return []
""")

        # extractor = FlaskRoutesExtractor(repo_path=self.temp_dir)
        # result = extractor.extract()

        # Should find all 10 routes
        # self.assertEqual(len(result.entities), 10)

    def test_produces_valid_evidence(self) -> None:
        """Test that evidence records have correct structure and references."""
        # TODO: Implement
        fixture_file = os.path.join(self.temp_dir, "evidence.py")
        with open(fixture_file, "w") as f:
            f.write(VALID_FLASK_ROUTE)

        # extractor = FlaskRoutesExtractor(repo_path=self.temp_dir)
        # result = extractor.extract()

        # Check evidence structure
        # for evid in result.evidence:
        #     self.assertIn("bytes_ref", evid.source_ref or {})
        #     self.assertEqual(evid.target_type, "entity")
        #     self.assertIsNotNone(evid.derivation_class)

    # ─────────────────────────────────────────────────────────────────────────
    # INTEGRATION TESTS
    # ─────────────────────────────────────────────────────────────────────────

    def test_empty_directory(self) -> None:
        """Test that extractor handles empty repository gracefully."""
        # TODO: Implement
        # extractor = FlaskRoutesExtractor(repo_path=self.temp_dir)
        # result = extractor.extract()

        # Should return empty result, not crash
        # self.assertEqual(len(result.entities), 0)
        # self.assertEqual(len(result.facts), 0)

    def test_facts_reference_entities(self) -> None:
        """Test that facts correctly reference extracted entities."""
        # TODO: Implement
        fixture_file = os.path.join(self.temp_dir, "simple.py")
        with open(fixture_file, "w") as f:
            f.write(VALID_FLASK_ROUTE)

        # extractor = FlaskRoutesExtractor(repo_path=self.temp_dir)
        # result = extractor.extract()

        # All facts should reference entities that exist
        # entity_ids = {e.entity_id for e in result.entities}
        # for fact in result.facts:
        #     # Either fact ID should be in entities or be a service URN
        #     self.assertTrue(
        #         fact.object_id in entity_ids or
        #         "service" in fact.object_id
        #     )


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    # Run all tests
    unittest.main(verbosity=2)
