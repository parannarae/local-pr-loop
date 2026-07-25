"""End-to-end tests for publication, recovery, and lock behavior."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

SCRIPT = Path(__file__).parents[1] / "scripts" / "review-json.sh"
LOCK_SCRIPT = Path(__file__).parents[1] / "scripts" / "review_lock.py"


def run(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*args],
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


class ReviewJsonCliTest(unittest.TestCase):
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
            "bash", str(SCRIPT), "init", str(self.repo), "review", cwd=self.repo
        ).stdout
        self.review_id = next(
            line.split(": ", 1)[1]
            for line in output.splitlines()
            if line.startswith("review_id: ")
        )
        base = self.repo / ".local" / "reviews" / self.review_id
        self.review = base.with_suffix(".json").resolve()
        self.event = base.with_suffix(".event.json").resolve()
        self.report = base.with_suffix(".latest.md").resolve()
        self.journal = base.with_suffix(".publish.json").resolve()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def snapshot(self) -> dict[str, Any]:
        return json.loads(
            run(
                "bash",
                str(SCRIPT),
                "snapshot",
                str(self.repo),
                "example.txt",
                cwd=self.repo,
            ).stdout
        )

    def acquire(self) -> None:
        output = run(
            "bash",
            str(SCRIPT),
            "lock",
            "acquire",
            str(self.repo),
            self.review_id,
            cwd=self.repo,
        ).stdout
        self.assertNotIn("token", output)
        self.assertEqual(json.loads(output)["status"], "acquired")
        lease = self.repo / ".local" / "reviews" / f"{self.review_id}.lease.json"
        self.assertEqual(lease.stat().st_mode & 0o777, 0o600)

    def prepare_review(self, source: dict[str, Any]) -> str:
        run(
            "bash",
            str(SCRIPT),
            "inspect",
            str(self.repo),
            self.review_id,
            "--json",
            "example.txt",
            cwd=self.repo,
        )
        run(
            "bash",
            str(SCRIPT),
            "template",
            str(self.repo),
            self.review_id,
            "review",
            cwd=self.repo,
        )
        event = json.loads(self.event.read_text())
        event["source_snapshot"] = source
        event["threads"][0].update(
            {
                "title": "Update example",
                "risk": "Old result remains.",
                "required_behavior": "Use the new result.",
            }
        )
        event["threads"][0]["evidence"].update(
            {
                "provenance": "example.txt",
                "sanitized_result": "The file contains the old value.",
            }
        )
        event["validation"]["performed"] = [
            {"check": "source inspection", "result": "passed"}
        ]
        write_json(self.event, event)
        return event["event_id"]

    def publish(self, *, check: bool = True) -> subprocess.CompletedProcess[str]:
        return run(
            "bash",
            str(SCRIPT),
            "publish",
            str(self.repo),
            self.review_id,
            cwd=self.repo,
            check=check,
        )

    def test_publish_is_clean_and_inspect_exposes_workflow_and_operation(self) -> None:
        source = self.snapshot()
        self.acquire()
        event_id = self.prepare_review(source)
        result = json.loads(self.publish().stdout)
        self.assertTrue(result["committed"])
        self.assertEqual(result["event_id"], event_id)
        self.assertFalse(self.event.exists())
        self.assertFalse(self.journal.exists())
        self.assertFalse(
            (self.repo / ".local" / "reviews" / f"{self.review_id}.lease.json").exists()
        )
        document = json.loads(self.review.read_text())
        self.assertEqual(document["state"]["workflow"]["phase"], "owner_response")
        inspected = run(
            "bash",
            str(SCRIPT),
            "inspect",
            str(self.repo),
            self.review_id,
            "--json",
            "example.txt",
            cwd=self.repo,
        ).stdout
        self.assertIn('"status": "clean"', inspected)
        dashboard = json.loads(inspected)
        self.assertIn("lock acquire", dashboard["recommended_next_command"])
        conversations = json.loads(
            run(
                "bash",
                str(SCRIPT),
                "threads",
                str(self.repo),
                self.review_id,
                "--json",
                cwd=self.repo,
            ).stdout
        )
        self.assertEqual(conversations[0]["thread"]["id"], "T1")

    def test_direct_publish_with_wrong_token_is_structured_precommit_failure(
        self,
    ) -> None:
        source = self.snapshot()
        self.acquire()
        event_id = self.prepare_review(source)
        publisher = Path(__file__).parents[1] / "scripts" / "review_publish.py"
        failed = run(
            "python3",
            str(publisher),
            "publish",
            "--repo",
            str(self.repo),
            "--review",
            str(self.review),
            "--event",
            str(self.event),
            "--report",
            str(self.report),
            "--journal",
            str(self.journal),
            "--state-script",
            str(Path(__file__).parents[1] / "scripts" / "review_state.py"),
            "--lock-script",
            str(LOCK_SCRIPT),
            "--snapshot-script",
            str(Path(__file__).parents[1] / "scripts" / "source_snapshot.py"),
            "--token",
            "wrong-token",
            "--expected-review-sha",
            hashlib.sha256(self.review.read_bytes()).hexdigest(),
            "--expected-source-fingerprint",
            source["fingerprint"],
            "--",
            "example.txt",
            cwd=self.repo,
            check=False,
        )
        result = json.loads(failed.stdout)
        self.assertEqual(result["status"], "precommit_failed")
        self.assertFalse(result["committed"])
        self.assertFalse(self.journal.exists())
        history = json.loads(self.review.read_text())["history"]
        self.assertEqual(history, [])
        self.assertEqual(json.loads(self.event.read_text())["event_id"], event_id)
        run(
            "bash",
            str(SCRIPT),
            "lock",
            "release",
            str(self.repo),
            self.review_id,
            cwd=self.repo,
        )

    def test_wrong_lock_token_does_not_damage_active_lock(self) -> None:
        self.acquire()
        lease_path = self.repo / ".local" / "reviews" / f"{self.review_id}.lease.json"
        token = json.loads(lease_path.read_text())["token"]
        failed = run(
            "bash",
            str(SCRIPT),
            "lock",
            "release",
            str(self.repo),
            self.review_id,
            "wrong-token",
            cwd=self.repo,
            check=False,
        )
        self.assertNotEqual(failed.returncode, 0)
        verified = run(
            "python3",
            str(LOCK_SCRIPT),
            "verify",
            "--repo",
            str(self.repo),
            "--review-file",
            str(self.review),
            "--token",
            token,
            cwd=self.repo,
        )
        self.assertIn("verified", verified.stdout)


if __name__ == "__main__":
    unittest.main()
