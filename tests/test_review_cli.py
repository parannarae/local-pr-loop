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
import review_compose
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
                "await-handoff",
                "discover",
                "draft",
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

    def test_every_operation_the_format_allows_has_a_typed_constructor(self) -> None:
        # A vocabulary entry with no constructor is one an agent would have to
        # hand-shape, which is the authoring path the composer exists to remove.
        # `timeout.declare` is the sole exception: template stamps it from the
        # handoff clock, so no agent ever composes one.
        constructed = {
            value.OP
            for value in vars(review_compose).values()
            if isinstance(value, type)
            and issubclass(value, review_compose.Constructor)
            and value.OP
        }
        self.assertEqual(
            constructed,
            set(review_schema.OPERATION_FIELDS) - {"timeout.declare"},
        )

    def test_the_composer_exposes_a_subcommand_for_every_composer(self) -> None:
        # Beyond the composers, the draft command model is `drop`, which takes an
        # act back, `reply-context`, which records what an owner reply derives
        # from the repository, and `show`, which reports what the draft owes.
        parser = review_compose.build_parser()
        subcommands = next(
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        self.assertEqual(
            set(subcommands.choices),
            set(review_compose.COMPOSERS) | {"drop", "reply-context", "show"},
        )

    def test_package_version_references_are_consistent(self) -> None:
        skill_text = (ROOT / "SKILL.md").read_text()
        match = re.search(r'^  version: "([^"]+)"$', skill_text, re.MULTILINE)
        if match is None:
            self.fail("SKILL.md must declare a metadata version")
        version = match.group(1)
        self.assertEqual(version, review_schema.CREATOR_VERSION)
        schema_document = (ROOT / "references" / "review-schema.md").read_text()
        self.assertIn(f"The skill version is `{version}`.", schema_document)
        self.assertIn(f'"created_by": {{"version": "{version}"}}', schema_document)
