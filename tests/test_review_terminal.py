"""Terminal-state reporting tests separating approvals from timeouts."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import review_state

CLI = ROOT / "scripts" / "review_cli.py"
PUBLISH = ROOT / "scripts" / "review_publish.py"
STATE = ROOT / "scripts" / "review_state.py"
LOCK = ROOT / "scripts" / "review_lock.py"


def run(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*args],
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


class TerminalReportingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.review = self.root / "review.json"
        self.event = self.root / "review.event.json"
        self.report = self.root / "review.latest.md"
        self.journal = self.root / "review.publish.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def dashboard(self, outcome: str) -> dict:
        document = review_state.new_document("abcdefgh", "review")
        document["state"]["workflow"]["phase"] = "terminal"
        document["state"]["workflow"]["primary_actor"] = None
        document["state"]["terminal"] = {
            "outcome": outcome,
            "occurred_at": "2026-08-17T12:00:00+00:00",
        }
        document["state"]["source_fingerprint"] = "a" * 64
        self.review.write_text(json.dumps(document, indent=2) + "\n")
        self.report.write_text(review_state.render_report(document))
        completed = run(
            sys.executable,
            str(PUBLISH),
            "operation",
            "--review",
            str(self.review),
            "--event",
            str(self.event),
            "--report",
            str(self.report),
            "--journal",
            str(self.journal),
            "--state-script",
            str(STATE),
            "--lock-json",
            "unlocked",
            "--repo",
            str(self.root),
            "--review-id",
            "abcdefgh",
            # A different fingerprint means the source moved after the terminal event.
            "--current-source-fingerprint",
            "b" * 64,
            "--command-prefix",
            "cli",
            "--json",
            cwd=self.root,
        )
        return json.loads(completed.stdout)

    def test_a_drifted_approval_is_stale_and_routes_to_a_successor(self) -> None:
        source = self.dashboard("lgtm")["source"]

        self.assertTrue(source["drift"])
        self.assertTrue(source["approval_stale"])
        self.assertFalse(source["source_moved_since_terminal"])

    def test_a_drifted_timeout_is_not_reported_as_a_stale_approval(self) -> None:
        """A timeout records that nothing was verified, so no approval can be stale."""

        dashboard = self.dashboard("reviewer_timeout")
        source = dashboard["source"]

        self.assertTrue(source["drift"])
        self.assertFalse(source["approval_stale"])
        self.assertEqual(source["terminal_outcome"], "reviewer_timeout")
        self.assertTrue(source["source_moved_since_terminal"])
        # Steering to a successor here would present a timeout as an aged-out approval.
        self.assertEqual(dashboard["recommended_next_command"], "none")


class StartFollowUpTest(unittest.TestCase):
    """`start-follow-up` is the successor path for every terminal outcome."""

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
        self.canonical = (
            self.repo / ".local" / "reviews" / f"{self.review_id}.json"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_a_timeout_terminal_can_start_a_linked_successor(self) -> None:
        # Build the terminal through the real append path so the projected state is
        # genuine; a hand-edited state is correctly rejected as invalid.
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
        self.assertEqual(document["state"]["workflow"]["phase"], "terminal")

        output = run(
            sys.executable,
            str(CLI),
            "start-follow-up",
            str(self.repo),
            self.review_id,
            "successor",
            cwd=self.repo,
        ).stdout
        successor_id = next(
            line.split(": ", 1)[1]
            for line in output.splitlines()
            if line.startswith("review_id: ")
        )
        successor = json.loads(
            (self.repo / ".local" / "reviews" / f"{successor_id}.json").read_text()
        )

        # Provenance must be visible from the successor, not only the predecessor's prose.
        self.assertEqual(successor["prior_review_id"], self.review_id)


if __name__ == "__main__":
    unittest.main()
