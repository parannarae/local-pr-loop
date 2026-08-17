"""Retirement tests for reviews that have published no events."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import review_state

CLI = ROOT / "scripts" / "review_cli.py"


def run(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*args],
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


class RetireReviewTest(unittest.TestCase):
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
        (self.repo / "example.txt").write_text("after\n")
        output = run(
            sys.executable, str(CLI), "init", str(self.repo), "review", cwd=self.repo
        ).stdout
        self.review_id = next(
            line.split(": ", 1)[1]
            for line in output.splitlines()
            if line.startswith("review_id: ")
        )
        self.reviews = self.repo / ".local" / "reviews"
        self.canonical = self.reviews / f"{self.review_id}.json"
        self.retired = self.reviews / f"{self.review_id}.retired.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def retire(self, check: bool = True) -> subprocess.CompletedProcess[str]:
        return run(
            sys.executable,
            str(CLI),
            "retire",
            str(self.repo),
            self.review_id,
            "--reason",
            "created by a false-positive stale approval",
            cwd=self.repo,
            check=check,
        )

    def test_an_event_free_review_is_retired_and_stops_occupying_the_scope(self) -> None:
        completed = self.retire()

        self.assertEqual(json.loads(completed.stdout)["status"], "retired")
        self.assertFalse(self.canonical.exists())
        self.assertFalse((self.reviews / f"{self.review_id}.latest.md").exists())
        disposal = json.loads(self.retired.read_text())
        self.assertEqual(disposal["review_id"], self.review_id)
        self.assertIn("false-positive", disposal["reason"])

    def test_the_identifier_stays_claimed_after_retirement(self) -> None:
        self.retire()

        # The disposal record is what prevents a later init from reusing the identifier.
        self.assertTrue(self.retired.is_file())

    def test_a_review_with_published_events_is_never_retired(self) -> None:
        created_at = "2026-08-17T10:00:00+00:00"
        document = review_state.new_document(self.review_id, "review")
        document["created_at"] = created_at
        event = review_state.event_template("initial_review_timeout")
        event["reason"] = "the reviewer never appeared"
        event["started_at"] = created_at
        event["deadline"] = "2026-08-17T12:00:00+00:00"
        event["occurred_at"] = "2026-08-17T12:00:01+00:00"
        document = review_state.append_event(document, event)
        self.canonical.write_text(json.dumps(document, indent=2) + "\n")

        completed = self.retire(check=False)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("published events", completed.stderr)
        self.assertTrue(self.canonical.is_file())
        self.assertFalse(self.retired.exists())

    def test_a_competing_lock_cannot_interleave_with_retirement(self) -> None:
        """Retirement must exclude guard creation and publication, not merely check them.

        Both require the cooperative lock, so a competing acquisition attempted at the
        exact moment before canonical state is removed proves the critical section holds.
        Checking lock status and then deleting would leave that window open.
        """

        import review_cli

        attempts: list[int] = []
        real_retire = review_cli.retire_locked_review

        def attempt_competing_lock(paths, document, reason):
            completed = run(
                sys.executable,
                str(CLI),
                "lock",
                "acquire",
                str(self.repo),
                self.review_id,
                cwd=self.repo,
                check=False,
            )
            attempts.append(completed.returncode)
            return real_retire(paths, document, reason)

        review_cli.retire_locked_review = attempt_competing_lock
        try:
            review_cli.command_retire(
                Namespace(
                    repo=str(self.repo), review_id=self.review_id, reason="race probe"
                )
            )
        finally:
            review_cli.retire_locked_review = real_retire

        self.assertEqual(len(attempts), 1)
        self.assertNotEqual(attempts[0], 0, "a competing lock was granted mid-retirement")
        self.assertFalse(self.canonical.exists())
        self.assertTrue(self.retired.is_file())

    def test_a_locked_review_is_never_retired(self) -> None:
        run(
            sys.executable,
            str(CLI),
            "lock",
            "acquire",
            str(self.repo),
            self.review_id,
            cwd=self.repo,
        )

        completed = self.retire(check=False)

        self.assertNotEqual(completed.returncode, 0)
        self.assertTrue(self.canonical.is_file())
        self.assertFalse(self.retired.exists())


if __name__ == "__main__":
    unittest.main()
