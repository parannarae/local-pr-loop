"""End-to-end test for the guarded schema-v2 command workflow."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

SCRIPT = Path(__file__).parents[1] / "scripts" / "review-json.sh"


def run(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        [*args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


class ReviewJsonCliTest(unittest.TestCase):
    def test_review_owner_reply_and_final_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            run("git", "init", "-q", cwd=repo)
            run("git", "config", "user.email", "review@example.com", cwd=repo)
            run("git", "config", "user.name", "Review Test", cwd=repo)
            (repo / ".gitignore").write_text(".local/\n")
            source = repo / "example.txt"
            source.write_text("before\n")
            run("git", "add", ".gitignore", "example.txt", cwd=repo)
            run("git", "commit", "-qm", "initial", cwd=repo)

            init_output = run(
                "bash",
                str(SCRIPT),
                "init",
                str(repo),
                "thread-review",
                cwd=repo,
            )
            review_id = next(
                line.split(": ", 1)[1]
                for line in init_output.splitlines()
                if line.startswith("review_id: ")
            )
            review_path = repo / ".local" / "reviews" / f"{review_id}.json"
            event_path = repo / ".local" / "reviews" / f"{review_id}.event.json"
            report_path = repo / ".local" / "reviews" / f"{review_id}.latest.md"

            initial_snapshot = json.loads(
                run(
                    "bash",
                    str(SCRIPT),
                    "snapshot",
                    str(repo),
                    "example.txt",
                    cwd=repo,
                )
            )
            token = self._acquire(repo, review_id)
            run(
                "bash",
                str(SCRIPT),
                "template",
                str(repo),
                review_id,
                token,
                "review",
                cwd=repo,
            )
            review_event = json.loads(event_path.read_text())
            review_event["source_snapshot"] = initial_snapshot
            review_event["threads"][0].update(
                {
                    "title": "Update the example",
                    "risk": "The old example remains visible.",
                    "evidence": "example.txt still contains before.",
                    "required_behavior": "Change the example to after.",
                }
            )
            review_event["validation"]["performed"] = ["Initial inspection completed."]
            write_json(event_path, review_event)
            self._publish(
                repo,
                review_id,
                token,
                review_path,
                initial_snapshot["fingerprint"],
            )
            document = json.loads(review_path.read_text())
            self.assertEqual(document["state"]["marker"], "OWNER ACTION REQUIRED")
            self.assertEqual(document["state"]["open_threads"], ["T1"])

            token = self._acquire(repo, review_id)
            source.write_text("after\n")
            completed_snapshot = json.loads(
                run(
                    "bash",
                    str(SCRIPT),
                    "snapshot",
                    str(repo),
                    "example.txt",
                    cwd=repo,
                )
            )
            run(
                "bash",
                str(SCRIPT),
                "template",
                str(repo),
                review_id,
                token,
                "owner_reply",
                cwd=repo,
            )
            owner_event = json.loads(event_path.read_text())
            owner_event.update(
                {
                    "starting_source_snapshot": initial_snapshot,
                    "source_drift_assessment": "Only example.txt changed.",
                    "completed_source_snapshot": completed_snapshot,
                    "replies": [
                        {
                            "thread_id": "T1",
                            "decision": "applied",
                            "message": "Updated the example.",
                            "evidence": "example.txt now contains after.",
                        }
                    ],
                    "files_changed": ["example.txt"],
                    "guide_synchronization": "No guide synchronization was needed.",
                    "commits": [],
                }
            )
            owner_event["validation"]["performed"] = ["Verified the updated content."]
            write_json(event_path, owner_event)
            self._publish(
                repo,
                review_id,
                token,
                review_path,
                completed_snapshot["fingerprint"],
            )
            document = json.loads(review_path.read_text())
            self.assertEqual(document["state"]["marker"], "REVIEWER ACTION REQUIRED")
            self.assertEqual(document["state"]["open_threads"], ["T1"])

            token = self._acquire(repo, review_id)
            run(
                "bash",
                str(SCRIPT),
                "template",
                str(repo),
                review_id,
                token,
                "final_review",
                cwd=repo,
            )
            final_event = json.loads(event_path.read_text())
            final_event.update(
                {
                    "source_snapshot": completed_snapshot,
                    "resolutions": [
                        {
                            "thread_id": "T1",
                            "message": "The updated source satisfies the requirement.",
                        }
                    ],
                    "decision": "LGTM",
                }
            )
            final_event["validation"]["performed"] = ["Final source inspection passed."]
            write_json(event_path, final_event)
            self._publish(
                repo,
                review_id,
                token,
                review_path,
                completed_snapshot["fingerprint"],
            )

            document = json.loads(review_path.read_text())
            self.assertEqual(document["schema_version"], 2)
            self.assertEqual(document["state"]["marker"], "LGTM")
            self.assertEqual(document["state"]["open_threads"], [])
            self.assertEqual(document["state"]["resolved_threads"], ["T1"])
            self.assertFalse(event_path.exists())
            report = report_path.read_text()
            self.assertIn("- Status: LGTM", report)
            self.assertIn("- Resolved threads: T1", report)

    @staticmethod
    def _acquire(repo: Path, review_id: str) -> str:
        output = run(
            "bash",
            str(SCRIPT),
            "lock",
            "acquire",
            str(repo),
            review_id,
            cwd=repo,
        )
        return json.loads(output)["token"]

    @staticmethod
    def _publish(
        repo: Path,
        review_id: str,
        token: str,
        review_path: Path,
        source_fingerprint: str,
    ) -> None:
        review_sha = hashlib.sha256(review_path.read_bytes()).hexdigest()
        run(
            "bash",
            str(SCRIPT),
            "publish",
            str(repo),
            review_id,
            token,
            review_sha,
            source_fingerprint,
            "example.txt",
            cwd=repo,
        )


if __name__ == "__main__":
    unittest.main()
