"""End-to-end tests for publication, recovery, lock, and waiting behavior."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

import review_workflow

SCRIPT = Path(__file__).parents[1] / "scripts" / "review_cli.py"
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
            sys.executable, str(SCRIPT), "init", str(self.repo), "review", cwd=self.repo
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
                sys.executable,
                str(SCRIPT),
                "snapshot",
                str(self.repo),
                "example.txt",
                cwd=self.repo,
            ).stdout
        )

    def acquire(self) -> None:
        output = run(
            sys.executable,
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
            sys.executable,
            str(SCRIPT),
            "inspect",
            str(self.repo),
            self.review_id,
            "--json",
            "example.txt",
            cwd=self.repo,
        )
        run(
            sys.executable,
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
            sys.executable,
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
            sys.executable,
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
        self.assertIn("review_cli.py", dashboard["recommended_next_command"])
        self.assertNotIn("review-json.sh", dashboard["recommended_next_command"])
        conversations = json.loads(
            run(
                sys.executable,
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
            sys.executable,
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
            sys.executable,
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

    def test_guarded_draft_helpers_preserve_cli_artifact_contract(self) -> None:
        initial = json.loads(
            run(
                sys.executable,
                str(SCRIPT),
                "inspect",
                str(self.repo),
                self.review_id,
                "--json",
                "example.txt",
                cwd=self.repo,
            ).stdout
        )
        self.assertEqual(initial["workflow"]["phase"], "awaiting_initial_review")
        self.assertEqual(initial["operation"]["status"], "clean")
        self.assertEqual(initial["operation"]["lock_status"], "unlocked")
        self.assertFalse(initial["operation"]["lease_present"])
        self.assertFalse(initial["source"]["drift"])
        self.assertIn("lock acquire", initial["recommended_next_command"])

        self.acquire()
        guarded = json.loads(
            run(
                sys.executable,
                str(SCRIPT),
                "inspect",
                str(self.repo),
                self.review_id,
                "--json",
                "example.txt",
                cwd=self.repo,
            ).stdout
        )
        self.assertEqual(guarded["operation"]["lock_status"], "locked")
        self.assertTrue(guarded["operation"]["lease_present"])
        self.assertIn("template", guarded["recommended_next_command"])

        template_output = run(
            sys.executable,
            str(SCRIPT),
            "template",
            str(self.repo),
            self.review_id,
            "review",
            cwd=self.repo,
        ).stdout.strip()
        self.assertEqual(Path(template_output), self.event)
        self.assertEqual(self.event.stat().st_mode & 0o777, 0o600)

        check_result = json.loads(
            run(
                sys.executable,
                str(SCRIPT),
                "add-check",
                str(self.repo),
                self.review_id,
                "passed",
                "schema validation",
                cwd=self.repo,
            ).stdout
        )
        self.assertEqual(check_result["status"], "check_added")
        gap_result = json.loads(
            run(
                sys.executable,
                str(SCRIPT),
                "add-gap",
                str(self.repo),
                self.review_id,
                "live probe",
                "service unavailable",
                "--material",
                cwd=self.repo,
            ).stdout
        )
        self.assertEqual(gap_result["status"], "gap_added")
        self.assertEqual(gap_result["gap_id"], "G1")
        draft = json.loads(self.event.read_text())
        self.assertEqual(
            draft["validation"]["performed"],
            [{"check": "schema validation", "result": "passed"}],
        )
        self.assertEqual(
            draft["validation"]["gaps"],
            [
                {
                    "gap_id": "G1",
                    "check": "live probe",
                    "reason": "service unavailable",
                    "material": True,
                }
            ],
        )

        aborted = json.loads(
            run(
                sys.executable,
                str(SCRIPT),
                "abort-draft",
                str(self.repo),
                self.review_id,
                cwd=self.repo,
            ).stdout
        )
        self.assertEqual(aborted["status"], "draft_aborted")
        self.assertFalse(self.event.exists())
        released = json.loads(
            run(
                sys.executable,
                str(SCRIPT),
                "lock",
                "release",
                str(self.repo),
                self.review_id,
                cwd=self.repo,
            ).stdout
        )
        self.assertEqual(released["status"], "released")

    def publish_lgtm(self) -> None:
        self.acquire()
        source = self.snapshot()
        run(
            sys.executable,
            str(SCRIPT),
            "inspect",
            str(self.repo),
            self.review_id,
            "--json",
            "example.txt",
            cwd=self.repo,
        )
        run(
            sys.executable,
            str(SCRIPT),
            "template",
            str(self.repo),
            self.review_id,
            "final_review",
            cwd=self.repo,
        )
        event = json.loads(self.event.read_text())
        event["source_snapshot"] = source
        event["decision"] = "LGTM"
        event["validation"]["performed"] = [
            {"check": "source inspection", "result": "passed"}
        ]
        write_json(self.event, event)
        self.assertTrue(json.loads(self.publish().stdout)["committed"])

    def await_handoff(
        self, round_seconds: int, max_rounds: int
    ) -> subprocess.CompletedProcess[str]:
        return run(
            sys.executable,
            str(SCRIPT),
            "await-handoff",
            str(self.repo),
            self.review_id,
            "--round-seconds",
            str(round_seconds),
            "--max-rounds",
            str(max_rounds),
            cwd=self.repo,
            check=False,
        )

    # --- await-handoff ---

    def test_await_handoff_returns_changed_when_counterpart_publishes(self) -> None:
        waiter = subprocess.Popen(
            [
                sys.executable,
                str(SCRIPT),
                "await-handoff",
                str(self.repo),
                self.review_id,
                "--round-seconds",
                "30",
                "--max-rounds",
                "1",
            ],
            cwd=self.repo,
            stdout=subprocess.PIPE,
            text=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        # Let the waiter record the initial canonical hash before publishing.
        time.sleep(3)
        source = self.snapshot()
        self.acquire()
        self.prepare_review(source)
        self.publish()
        stdout, _ = waiter.communicate(timeout=60)
        lines = stdout.strip().splitlines()
        self.assertEqual(
            lines[0],
            "waiting_for: reviewer to publish_initial_review"
            " (no handoff deadline in this phase)",
        )
        outcome = json.loads(lines[-1])
        self.assertEqual(outcome["status"], "changed")
        self.assertEqual(outcome["rounds_used"], 1)
        self.assertEqual(waiter.returncode, 0)

    def test_await_handoff_reports_terminal_on_entry(self) -> None:
        self.publish_lgtm()
        result = self.await_handoff(round_seconds=5, max_rounds=1)
        self.assertEqual(result.returncode, 0)
        outcome = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(outcome, {"rounds_used": 0, "status": "terminal"})

    def test_await_handoff_maps_passed_deadline_to_timeout_eligible(self) -> None:
        source = self.snapshot()
        self.acquire()
        self.prepare_review(source)
        self.publish()
        # Back-date the only event beyond the two-hour owner deadline. History,
        # derived latest_event, and thread evidence observed_at must stay
        # mutually consistent for the document to remain valid.
        document = json.loads(self.review.read_text())
        stale = (
            datetime.now(timezone.utc) - timedelta(hours=3)
        ).isoformat().replace("+00:00", "Z")
        document["history"][-1]["occurred_at"] = stale
        document["state"]["latest_event"]["occurred_at"] = stale
        for thread in document["history"][-1]["threads"]:
            thread["evidence"]["observed_at"] = stale
        write_json(self.review, document)
        run(
            sys.executable,
            str(SCRIPT),
            "validate",
            str(self.repo),
            self.review_id,
            cwd=self.repo,
        )
        result = self.await_handoff(round_seconds=5, max_rounds=1)
        self.assertEqual(result.returncode, 4)
        lines = result.stdout.strip().splitlines()
        self.assertEqual(
            lines[0],
            "waiting_for: owner to reply_to_open_threads (a handoff deadline applies)",
        )
        outcome = json.loads(lines[-1])
        self.assertEqual(outcome["status"], "timeout_eligible")
        self.assertEqual(outcome["rounds_used"], 1)

    def test_await_handoff_exhausts_at_bound_in_awaiting_initial_review(self) -> None:
        result = self.await_handoff(round_seconds=1, max_rounds=1)
        self.assertEqual(result.returncode, 5)
        lines = result.stdout.strip().splitlines()
        self.assertEqual(
            lines[0],
            "waiting_for: reviewer to publish_initial_review"
            " (no handoff deadline in this phase)",
        )
        outcome = json.loads(lines[-1])
        self.assertEqual(outcome, {"rounds_used": 1, "status": "exhausted"})

    def test_await_handoff_rejects_out_of_range_bounds(self) -> None:
        # Bounds are enforced by the workflow helper's own parser, like
        # wait's timeout range, so both reject with the argparse exit code.
        self.assertEqual(self.await_handoff(1, 0).returncode, 2)
        self.assertEqual(self.await_handoff(0, 1).returncode, 2)

    # --- follow-up routing ---

    def test_initial_final_review_and_source_drift_route_to_follow_up(self) -> None:
        self.publish_lgtm()

        (self.repo / "example.txt").write_text("after\n")
        dashboard = json.loads(
            run(
                sys.executable,
                str(SCRIPT),
                "inspect",
                str(self.repo),
                self.review_id,
                "--json",
                "example.txt",
                cwd=self.repo,
            ).stdout
        )
        self.assertEqual(dashboard["workflow"]["phase"], "terminal")
        self.assertTrue(dashboard["source"]["drift"])
        self.assertTrue(dashboard["source"]["approval_stale"])
        self.assertIn("start-follow-up", dashboard["recommended_next_command"])


def write_waiting_document(path: Path, marker: str) -> str:
    """Write a minimal waiting-phase canonical document; return its SHA-256.

    `marker` only varies the bytes so two writes produce distinct hashes.
    """
    document = {
        "name": marker,
        "state": {
            "workflow": {
                "phase": "awaiting_initial_review",
                "primary_actor": "reviewer",
                "primary_action": {"kind": "publish_initial_review"},
            },
            "latest_event": None,
        },
    }
    path.write_text(json.dumps(document))
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ReviewWorkflowWaitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.review = Path(self.temporary.name) / "review.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    # --- poll_for_change ---

    def test_reports_change_that_landed_before_the_poll_started(self) -> None:
        baseline = write_waiting_document(self.review, "before")
        current = write_waiting_document(self.review, "after")

        result = review_workflow.poll_for_change(self.review, 1, baseline)

        self.assertEqual(result, {"status": "changed", "canonical_sha256": current})

    def test_times_out_on_unchanged_file_without_expected_baseline(self) -> None:
        baseline = write_waiting_document(self.review, "stable")

        result = review_workflow.poll_for_change(self.review, 1)

        self.assertEqual(result, {"status": "timeout", "canonical_sha256": baseline})

    # --- await_handoff ---

    def test_every_round_polls_against_the_entry_baseline(self) -> None:
        baseline = write_waiting_document(self.review, "entry")
        changed = {"status": "changed", "canonical_sha256": "b" * 64}
        timed_out = {"status": "timeout", "canonical_sha256": baseline}
        args = argparse.Namespace(
            review=str(self.review), round_seconds=5, max_rounds=3
        )

        # A change absorbed into a later round's baseline was the original
        # defect, so the contract under test is that every round receives the
        # baseline captured once at entry.
        with mock.patch.object(
            review_workflow, "poll_for_change", side_effect=[timed_out, changed]
        ) as poll:
            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                exit_code = review_workflow.await_handoff(args)

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            poll.call_args_list,
            [
                mock.call(self.review, 5, baseline),
                mock.call(self.review, 5, baseline),
            ],
        )
        outcome = json.loads(stdout.getvalue().strip().splitlines()[-1])
        self.assertEqual(outcome["status"], "changed")
        self.assertEqual(outcome["rounds_used"], 2)


if __name__ == "__main__":
    unittest.main()
