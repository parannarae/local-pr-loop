"""End-to-end tests for publication, recovery, lock, and waiting behavior."""

from __future__ import annotations

import hashlib
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

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

import review_state

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
            "--scope-json",
            json.dumps(
                {"exclude": [], "additional_input": [], "scope": ["example.txt"]}
            ),
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
            " (a handoff deadline applies)",
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
            " (a handoff deadline applies)",
        )
        outcome = json.loads(lines[-1])
        self.assertEqual(outcome, {"rounds_used": 1, "status": "exhausted"})

    def test_await_handoff_rejects_out_of_range_bounds(self) -> None:
        # Bounds are enforced by the workflow helper's own parser, like
        # wait's timeout range, so both reject with the argparse exit code.
        self.assertEqual(self.await_handoff(1, 0).returncode, 2)
        self.assertEqual(self.await_handoff(0, 1).returncode, 2)

    # --- add-note ---

    def test_add_note_lands_on_review_draft_thread_message(self) -> None:
        self.snapshot()
        self.acquire()
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
        # Review threads carry their own optional message, so a note flagged
        # at raise time lands on the thread itself.
        noted = json.loads(
            run(
                sys.executable,
                str(SCRIPT),
                "add-note",
                str(self.repo),
                self.review_id,
                "T1",
                "raised as a design constraint",
                "--tag",
                "decision",
                cwd=self.repo,
            ).stdout
        )
        self.assertEqual(noted["status"], "note_added")
        draft = json.loads(self.event.read_text())
        self.assertEqual(
            draft["threads"][0]["message"],
            "Note to user: [decision] raised as a design constraint",
        )
        failed = run(
            sys.executable,
            str(SCRIPT),
            "add-note",
            str(self.repo),
            self.review_id,
            "T9",
            "no such thread",
            cwd=self.repo,
            check=False,
        )
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("no entry for thread", failed.stderr)

    def test_add_note_round_trips_into_summary_notes_section(self) -> None:
        source = self.snapshot()
        self.acquire()
        self.prepare_review(source)
        self.publish()

        self.acquire()
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
            "owner_reply",
            cwd=self.repo,
        )
        event = json.loads(self.event.read_text())
        event["source_drift_assessment"] = "No drift."
        event["guide_synchronization"] = "No guide impact."
        event["replies"][0].update(
            {"decision": "applied", "message": "Updated the example."}
        )
        event["replies"][0]["evidence"].update(
            {
                "provenance": "example.txt",
                "sanitized_result": "The file now holds the new value.",
            }
        )
        event["validation"]["performed"] = [
            {"check": "source inspection", "result": "passed"}
        ]
        write_json(self.event, event)
        noted = json.loads(
            run(
                sys.executable,
                str(SCRIPT),
                "add-note",
                str(self.repo),
                self.review_id,
                "T1",
                "design shifted to eventual consistency",
                "--tag",
                "decision",
                cwd=self.repo,
            ).stdout
        )
        self.assertEqual(noted["status"], "note_added")
        # No manual occurred_at repair: the helper restamps the draft, so the
        # publish below also proves the timestamp refresh.
        result = json.loads(self.publish().stdout)
        self.assertTrue(result["committed"])
        report = self.report.read_text()
        self.assertIn("- **Attention: 1 note for you**", report)
        self.assertIn(
            "- **[decision]** design shifted to eventual consistency *(T1)*",
            report,
        )
        self.assertLess(
            report.index("## Notes for You"), report.index("## Issue Summary")
        )

    # --- event templates vs schema ---

    def test_event_templates_emit_only_schema_allowed_fields(self) -> None:
        # A generated field the validator rejects forces agents to hand-edit
        # structure, which the workflow forbids; every kind must template
        # clean of unknown-field errors.
        for kind in review_state.ACTOR_BY_KIND:
            template = review_state.event_template(kind)
            unknown = [
                error
                for error in review_state.validate_event(template)
                if "unknown fields" in error
            ]
            self.assertEqual(unknown, [], kind)

    # --- source_update template ---

    def test_source_update_template_validates_after_filling_blanks_only(self) -> None:
        source = self.snapshot()
        self.acquire()
        self.prepare_review(source)
        self.publish()
        self.acquire()
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
            "source_update",
            cwd=self.repo,
        )
        event = json.loads(self.event.read_text())
        event["reason"] = "Replacement basis for continued review."
        write_json(self.event, event)
        # Filling the documented semantic blank alone must validate; no
        # generated field may need deleting.
        result = run(
            sys.executable,
            str(SCRIPT),
            "validate-event",
            str(self.repo),
            self.review_id,
            cwd=self.repo,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    # --- initial_review_timeout ---

    def test_stalled_initial_review_reaches_terminal_via_timeout(self) -> None:
        # Back-date creation past the two-hour deadline before guarding, so
        # the guard hashes the canonical bytes that publish will verify. The
        # Z spelling covers the verbatim started_at anchor copy: projection
        # compares raw strings, so a reserialized +00:00 form would reject.
        document = json.loads(self.review.read_text())
        document["created_at"] = (
            (datetime.now(timezone.utc) - timedelta(hours=3))
            .isoformat()
            .replace("+00:00", "Z")
        )
        write_json(self.review, document)
        self.acquire()
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
        result = run(
            sys.executable,
            str(SCRIPT),
            "publish-timeout",
            "--if-eligible",
            str(self.repo),
            self.review_id,
            cwd=self.repo,
        )
        published = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertTrue(published["committed"])
        state = json.loads(self.review.read_text())["state"]
        self.assertEqual(state["workflow"]["phase"], "terminal")
        self.assertEqual(state["terminal"]["outcome"], "initial_review_timeout")
        report = self.report.read_text()
        self.assertIn(
            "- **Outcome: ended by initial_review_timeout — review incomplete**",
            report,
        )
        self.assertIn("Review ended by initial_review_timeout", report)

    # --- scope-candidates ---

    def test_scope_candidates_groups_changed_paths_and_unions_them(self) -> None:
        (self.repo / "example.txt").write_text("modified\n")
        (self.repo / "new.txt").write_text("untracked\n")
        result = json.loads(
            run(
                sys.executable,
                str(SCRIPT),
                "scope-candidates",
                str(self.repo),
                cwd=self.repo,
            ).stdout
        )
        self.assertIsNone(result["merge_base"])
        self.assertIn("example.txt", result["candidates"]["unstaged"])
        self.assertIn("new.txt", result["candidates"]["untracked"])
        self.assertEqual(result["union"], ["example.txt", "new.txt"])

    def test_scope_candidates_with_base_ref_reports_merge_base_diff(self) -> None:
        with_base = json.loads(
            run(
                sys.executable,
                str(SCRIPT),
                "scope-candidates",
                str(self.repo),
                "HEAD",
                cwd=self.repo,
            ).stdout
        )
        self.assertTrue(with_base["merge_base"])
        self.assertEqual(with_base["candidates"]["merge_base_diff"], [])

    # --- draft helper timestamp refresh ---

    def test_draft_helpers_restamp_occurred_at(self) -> None:
        self.snapshot()
        self.acquire()
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
        templated_at = json.loads(self.event.read_text())["occurred_at"]
        run(
            sys.executable,
            str(SCRIPT),
            "add-check",
            str(self.repo),
            self.review_id,
            "passed",
            "schema validation",
            cwd=self.repo,
        )
        restamped_at = json.loads(self.event.read_text())["occurred_at"]
        self.assertGreater(restamped_at, templated_at)

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

    # --- accretion ledger and structure chaining ---

    def cli(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return run(sys.executable, str(SCRIPT), *args, cwd=self.repo, check=check)

    def test_accretion_flags_chain_a_structure_round(self) -> None:
        base = run("git", "rev-parse", "HEAD", cwd=self.repo).stdout.strip()
        output = self.cli(
            "init", str(self.repo), "accretion-review", "--base-ref", "HEAD"
        ).stdout
        review_id = next(
            line.split(": ", 1)[1]
            for line in output.splitlines()
            if line.startswith("review_id: ")
        )
        reviews = self.repo / ".local" / "reviews"
        canonical = reviews / f"{review_id}.json"
        self.assertEqual(json.loads(canonical.read_text())["comparison_base"], base)

        # Grow the guarded file well past the 20% threshold, then walk the guarded
        # final_review path: the ledger must flag it and the template must prefill
        # the acknowledgment.
        (self.repo / "example.txt").write_text("before\n" * 10)
        self.cli("lock", "acquire", str(self.repo), review_id)
        dashboard = json.loads(
            self.cli("inspect", str(self.repo), review_id, "--json", "example.txt").stdout
        )
        self.assertEqual(dashboard["accretion"]["flagged"], ["example.txt"])
        self.cli("template", str(self.repo), review_id, "final_review")
        draft_path = reviews / f"{review_id}.event.json"
        draft = json.loads(draft_path.read_text())
        self.assertEqual(draft["structure_debt"]["flagged_paths"], ["example.txt"])
        draft["decision"] = "LGTM"
        draft["validation"]["performed"] = [
            {"check": "source inspection", "result": "passed"}
        ]

        # Publishing without the acknowledgment fails before the commit point.
        unacknowledged = {
            key: value for key, value in draft.items() if key != "structure_debt"
        }
        write_json(draft_path, unacknowledged)
        refused = json.loads(
            self.cli("publish", str(self.repo), review_id, check=False).stdout
        )
        self.assertEqual(refused["status"], "precommit_failed")
        self.assertIn("structure_debt", refused["detail"])

        draft["structure_debt"].update(
            {
                "disposition": "structure_deferred",
                "message": "Real accretion; chain a structure round.",
            }
        )
        write_json(draft_path, draft)
        self.assertTrue(
            json.loads(self.cli("publish", str(self.repo), review_id).stdout)[
                "committed"
            ]
        )

        # The deferred terminal recommends chaining the structure round.
        dashboard = json.loads(
            self.cli("inspect", str(self.repo), review_id, "--json", "example.txt").stdout
        )
        self.assertEqual(dashboard["workflow"]["phase"], "terminal")
        recommended = dashboard["recommended_next_command"]
        self.assertIn("start-follow-up", recommended)
        self.assertIn("--kind structure", recommended)

        follow_up = self.cli(
            "start-follow-up",
            str(self.repo),
            review_id,
            "accretion-review-structure",
            "--kind",
            "structure",
        ).stdout
        successor_id = next(
            line.split(": ", 1)[1]
            for line in follow_up.splitlines()
            if line.startswith("review_id: ")
        )
        successor = json.loads((reviews / f"{successor_id}.json").read_text())
        self.assertEqual(successor["review_kind"], "structure")
        self.assertEqual(successor["prior_review_id"], review_id)
        self.assertEqual(successor["comparison_base"], base)

        # One structure round consumes the flag set: the prior terminal stops
        # recommending another.
        dashboard = json.loads(
            self.cli("inspect", str(self.repo), review_id, "--json", "example.txt").stdout
        )
        self.assertEqual(dashboard["recommended_next_command"], "none")


if __name__ == "__main__":
    unittest.main()
