"""Dependency-direction tests for review state modules."""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import review_projection
import review_schema
import review_state


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


class ReviewModuleBoundaryTest(unittest.TestCase):
    def test_dependency_direction_does_not_point_to_cli_facade(self) -> None:
        schema_imports = imported_modules(SCRIPTS / "review_schema.py")
        projection_imports = imported_modules(SCRIPTS / "review_projection.py")

        self.assertNotIn("review_state", schema_imports)
        self.assertNotIn("review_projection", schema_imports)
        self.assertNotIn("review_state", projection_imports)
        self.assertIn("review_schema", projection_imports)

    def test_state_facade_exposes_schema_and_projection_contracts(self) -> None:
        self.assertIs(review_state.validate_event, review_schema.validate_event)
        self.assertIs(
            review_state.validate_document,
            review_projection.validate_document,
        )
        self.assertIs(review_state.project_history, review_projection.project_history)

    def test_new_document_uses_projection_default_state(self) -> None:
        document = review_state.new_document("abcdefgh", "review")

        self.assertEqual(document["state"], review_projection.default_state())
        self.assertEqual(document["format"], review_schema.FORMAT)
        self.assertEqual(document["format_revision"], review_schema.FORMAT_REVISION)
