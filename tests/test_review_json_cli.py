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

    def acquire(self) -> str:
        output = run(
            "bash",
            str(SCRIPT),
            "lock",
            "acquire",
            str(self.repo),
            self.review_id,
            cwd=self.repo,
        ).stdout
        return json.loads(output)["token"]

    def prepare_review(self, token: str, source: dict[str, Any]) -> str:
        run(
            "bash",
            str(SCRIPT),
            "template",
            str(self.repo),
            self.review_id,
            token,
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

    def publish(
        self, token: str, fingerprint: str, *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        digest = hashlib.sha256(self.review.read_bytes()).hexdigest()
        return run(
            "bash",
            str(SCRIPT),
            "publish",
            str(self.repo),
            self.review_id,
            token,
            digest,
            fingerprint,
            "example.txt",
            cwd=self.repo,
            check=check,
        )

    def test_publish_is_clean_and_inspect_exposes_workflow_and_operation(self) -> None:
        source = self.snapshot()
        token = self.acquire()
        event_id = self.prepare_review(token, source)
        result = json.loads(self.publish(token, source["fingerprint"]).stdout)
        self.assertTrue(result["committed"])
        self.assertEqual(result["event_id"], event_id)
        self.assertFalse(self.event.exists())
        self.assertFalse(self.journal.exists())
        document = json.loads(self.review.read_text())
        self.assertEqual(document["state"]["workflow"]["phase"], "owner_response")
        inspected = run(
            "bash",
            str(SCRIPT),
            "inspect",
            str(self.repo),
            self.review_id,
            "example.txt",
            cwd=self.repo,
        ).stdout
        self.assertIn('"status": "clean"', inspected)

    def test_postcommit_cleanup_failure_is_recoverable_without_duplicate_append(
        self,
    ) -> None:
        source = self.snapshot()
        token = self.acquire()
        event_id = self.prepare_review(token, source)
        # Keep the lock active but make the publisher's token fail only at release.
        # The canonical commit happens before release; recovery uses the true token.
        owner_output = run(
            "python3",
            str(LOCK_SCRIPT),
            "status",
            "--repo",
            str(self.repo),
            "--review-file",
            str(self.review),
            cwd=self.repo,
        ).stdout
        self.assertIn("acquired_at", owner_output)
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
            "--token",
            "wrong-token",
            cwd=self.repo,
            check=False,
        )
        result = json.loads(failed.stdout)
        self.assertEqual(result["status"], "published_cleanup_required")
        self.assertTrue(result["committed"])
        self.assertTrue(self.journal.exists())
        recovered = run(
            "bash",
            str(SCRIPT),
            "recover-publish",
            str(self.repo),
            self.review_id,
            token,
            cwd=self.repo,
        )
        self.assertEqual(json.loads(recovered.stdout)["status"], "recovered")
        history = json.loads(self.review.read_text())["history"]
        self.assertEqual([item["event_id"] for item in history], [event_id])

    def test_wrong_lock_token_does_not_damage_active_lock(self) -> None:
        token = self.acquire()
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
