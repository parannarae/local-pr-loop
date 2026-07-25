"""Public command-model tests for the Python review CLI."""

from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import review_cli


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
                "evidence-template",
                "init",
                "inspect",
                "lock",
                "publish",
                "publish-timeout",
                "recover-publish",
                "regenerate-report",
                "snapshot",
                "start-follow-up",
                "template",
                "threads",
                "validate",
                "validate-event",
                "wait",
            },
        )
