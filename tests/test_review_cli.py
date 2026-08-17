"""Public command-model tests for the Python review CLI."""

from __future__ import annotations

import argparse
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import review_cli
import review_schema


class ReviewCliTest(unittest.TestCase):
    def test_python_cli_is_the_only_public_entry_point(self) -> None:
        self.assertTrue((SCRIPTS / "review_cli.py").is_file())
        self.assertFalse((SCRIPTS / "review-json.sh").exists())

    def test_command_model_exposes_every_documented_operation(self) -> None:
        parser = review_cli.build_parser()
        command_action = next(
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )

        self.assertEqual(
            set(command_action.choices),
            {
                "abort-draft",
                "add-check",
                "add-gap",
                "add-note",
                "await-handoff",
                "evidence-template",
                "init",
                "inspect",
                "lock",
                "publish",
                "publish-timeout",
                "recover-publish",
                "regenerate-report",
                "retire",
                "scope-candidates",
                "snapshot",
                "start-follow-up",
                "template",
                "threads",
                "validate",
                "validate-event",
                "wait",
            },
        )

    def test_package_version_references_are_consistent(self) -> None:
        skill_text = (ROOT / "SKILL.md").read_text()
        match = re.search(r'^  version: "([^"]+)"$', skill_text, re.MULTILINE)
        self.assertIsNotNone(match)
        version = match.group(1)
        self.assertEqual(version, review_schema.CREATOR_VERSION)
        schema_document = (ROOT / "references" / "review-schema.md").read_text()
        self.assertIn(f"The skill version is `{version}`.", schema_document)
        self.assertIn(f'"created_by": {{"version": "{version}"}}', schema_document)
