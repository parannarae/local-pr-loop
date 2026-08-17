"""Recovery and publication-result clarity tests.

These cover the cases where a correct outcome was previously reported in a way that read
as a failure, which is what makes an author redo work that already succeeded.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

CLI = ROOT / "scripts" / "review_cli.py"


def import_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


publisher = import_module("review_publish_recovery", ROOT / "scripts" / "review_publish.py")


def run(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*args],
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


# --- unpublishable_draft_reason ---


class UnpublishableDraftTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.draft = Path(self.temporary.name) / "draft.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write(self, value: dict) -> Path:
        self.draft.write_text(json.dumps(value))
        return self.draft

    def test_timeout_stamped_before_its_deadline_can_never_publish(self) -> None:
        draft = self.write(
            {
                "kind": "reviewer_timeout",
                "occurred_at": "2026-08-17T12:20:30+00:00",
                "deadline": "2026-08-17T12:22:18+00:00",
            }
        )

        self.assertIn("never succeed", publisher.unpublishable_draft_reason(draft))

    def test_timeout_stamped_after_its_deadline_may_publish(self) -> None:
        draft = self.write(
            {
                "kind": "reviewer_timeout",
                "occurred_at": "2026-08-17T12:22:39+00:00",
                "deadline": "2026-08-17T12:22:18+00:00",
            }
        )

        self.assertIsNone(publisher.unpublishable_draft_reason(draft))

    def test_a_non_timeout_draft_is_never_reported_as_unpublishable(self) -> None:
        draft = self.write(
            {"kind": "owner_reply", "occurred_at": "2026-08-17T12:20:30+00:00"}
        )

        self.assertIsNone(publisher.unpublishable_draft_reason(draft))


# --- terminal_outcome ---


class TerminalOutcomeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.review = Path(self.temporary.name) / "review.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_reports_the_recorded_outcome(self) -> None:
        self.review.write_text(json.dumps({"state": {"terminal": {"outcome": "lgtm"}}}))

        self.assertEqual(publisher.terminal_outcome(self.review), "lgtm")

    def test_reports_nothing_while_the_review_is_open(self) -> None:
        self.review.write_text(json.dumps({"state": {"terminal": None}}))

        self.assertIsNone(publisher.terminal_outcome(self.review))


# --- recover-publish without a receipt ---


class RecoverWithoutReceiptTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        run("git", "init", "-q", cwd=self.repo)
        run("git", "config", "user.email", "review@example.com", cwd=self.repo)
        run("git", "config", "user.name", "Review Test", cwd=self.repo)
        (self.repo / ".gitignore").write_text(".local/\n")
        (self.repo / "example.txt").write_text("before\n")
        run("git", "add", ".gitignore", "example.txt", cwd=self.repo)
        run("git", "commit", "-qm", "initial", cwd=self.repo)
        output = run(
            sys.executable, str(CLI), "init", str(self.repo), "review", cwd=self.repo
        ).stdout
        self.review_id = next(
            line.split(": ", 1)[1]
            for line in output.splitlines()
            if line.startswith("review_id: ")
        )
        self.event = (
            self.repo / ".local" / "reviews" / f"{self.review_id}.event.json"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def recover(self) -> dict:
        completed = run(
            sys.executable,
            str(CLI),
            "recover-publish",
            str(self.repo),
            self.review_id,
            cwd=self.repo,
            check=False,
        )
        return json.loads(completed.stdout), completed.returncode

    def test_a_clean_review_is_a_successful_no_op(self) -> None:
        """A deleted receipt is the normal result of successful cleanup, not damage."""

        value, returncode = self.recover()

        self.assertEqual(returncode, 0)
        self.assertEqual(value["status"], "already_clean")
        self.assertEqual(value["recovery_action"], "none")
        # Without a receipt this command cannot know which publication was meant.
        self.assertIsNone(value["event_id"])

    def test_a_remaining_draft_is_reported_rather_than_treated_as_damage(self) -> None:
        self.event.write_text(json.dumps({"kind": "owner_reply"}))

        value, returncode = self.recover()

        self.assertEqual(returncode, 0)
        self.assertEqual(value["status"], "nothing_to_recover")
        self.assertIn("draft", value["detail"])


if __name__ == "__main__":
    unittest.main()
