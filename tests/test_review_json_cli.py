"""End-to-end tests for composition, publication, recovery, lock, and waiting."""

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

# The evidence every composed act in this suite carries. Each command takes it as
# typed flags, so there is no evidence JSON for a test to shape by hand.
EVIDENCE = (
    "--basis",
    "source_inspection",
    "--provenance",
    "example.txt",
    "--sanitized-result",
    "The guarded path carries the observed value.",
)


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
        self.review_id = self.review_id_of(output)
        base = self.repo / ".local" / "reviews" / self.review_id
        self.review = base.with_suffix(".json").resolve()
        self.event = base.with_suffix(".event.json").resolve()
        self.report = base.with_suffix(".latest.md").resolve()
        self.journal = base.with_suffix(".publish.json").resolve()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    # --- driving the CLI ---

    def cli(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return run(sys.executable, str(SCRIPT), *args, cwd=self.repo, check=check)

    def draft(
        self, *args: str, review_id: str | None = None, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        return self.cli(
            "draft", str(self.repo), review_id or self.review_id, *args, check=check
        )

    def draft_path(self, review_id: str | None = None) -> Path:
        return (
            self.repo
            / ".local"
            / "reviews"
            / f"{review_id or self.review_id}.event.json"
        )

    @staticmethod
    def review_id_of(output: str) -> str:
        return next(
            line.split(": ", 1)[1]
            for line in output.splitlines()
            if line.startswith("review_id: ")
        )

    def snapshot(self) -> dict[str, Any]:
        return json.loads(
            self.cli("snapshot", str(self.repo), "example.txt").stdout
        )

    def acquire(self, review_id: str | None = None) -> None:
        output = self.cli(
            "lock", "acquire", str(self.repo), review_id or self.review_id
        ).stdout
        self.assertNotIn("token", output)
        self.assertEqual(json.loads(output)["status"], "acquired")
        lease = (
            self.repo
            / ".local"
            / "reviews"
            / f"{review_id or self.review_id}.lease.json"
        )
        self.assertEqual(lease.stat().st_mode & 0o777, 0o600)

    def inspect(self, *flags: str, review_id: str | None = None) -> str:
        return self.cli(
            "inspect",
            str(self.repo),
            review_id or self.review_id,
            *flags,
            "example.txt",
        ).stdout

    def template(self, kind: str, review_id: str | None = None) -> None:
        self.cli("template", str(self.repo), review_id or self.review_id, kind)

    def guarded_template(self, kind: str, review_id: str | None = None) -> None:
        """Lock, guard, and open a draft of one kind."""
        self.acquire(review_id)
        self.inspect("--json", review_id=review_id)
        self.template(kind, review_id)

    def publish(self, *, check: bool = True) -> subprocess.CompletedProcess[str]:
        return self.cli("publish", str(self.repo), self.review_id, check=check)

    def publish_committed(self, review_id: str | None = None) -> None:
        published = self.cli(
            "publish", str(self.repo), review_id or self.review_id, check=False
        )
        self.assertTrue(json.loads(published.stdout)["committed"], published.stdout)

    # --- composing whole transactions ---

    def compose_threads(self, count: int, review_id: str | None = None) -> None:
        for index in range(1, count + 1):
            self.draft(
                "open-thread",
                "--title",
                "Update example" if index == 1 else f"Finding {index}",
                "--risk",
                "Old result remains.",
                "--required-behavior",
                "Use the new result.",
                "--paths",
                "example.txt",
                *EVIDENCE,
                review_id=review_id,
            )
        self.draft(
            "record-check",
            "--check",
            "source inspection",
            "--result",
            "passed",
            review_id=review_id,
        )

    def prepare_review(self, thread_count: int = 1) -> str:
        """Guard, template, and compose a publishable initial review."""
        self.guarded_template("review")
        self.compose_threads(thread_count)
        return json.loads(self.event.read_text())["event_id"]

    def publish_initial_review(self, review_id: str, thread_count: int = 1) -> None:
        self.guarded_template("review", review_id)
        self.compose_threads(thread_count, review_id=review_id)
        self.publish_committed(review_id)

    def compose_owner_reply(self, threads: list[str], review_id: str | None = None) -> None:
        self.draft(
            "reply-context",
            "--drift",
            "Only the guarded source changed.",
            "--guide",
            "No behavior guide needed a change.",
            review_id=review_id,
        )
        for thread_id in threads:
            self.draft(
                "reply",
                thread_id,
                "--decision",
                "applied",
                "--message",
                "Applied the new result.",
                *EVIDENCE,
                review_id=review_id,
            )
        self.draft(
            "record-check",
            "--check",
            "source inspection",
            "--result",
            "passed",
            review_id=review_id,
        )

    def publish_owner_reply(self, review_id: str, threads: list[str]) -> None:
        self.guarded_template("owner_reply", review_id)
        self.compose_owner_reply(threads, review_id=review_id)
        self.publish_committed(review_id)

    def publish_reviewer_update(
        self, review_id: str, resolve: str, comment: list[str]
    ) -> None:
        """Resolve one thread and comment on the rest.

        A reviewer_update may not resolve every open thread, which is what
        final_review is for, so the caller must leave at least one open.
        """
        self.guarded_template("reviewer_update", review_id)
        for thread_id in comment:
            self.draft(
                "comment",
                thread_id,
                "--message",
                "Still verifying this one.",
                review_id=review_id,
            )
        self.draft(
            "resolve",
            resolve,
            "--message",
            "Confirmed the new result independently.",
            "--verified",
            *EVIDENCE,
            review_id=review_id,
        )
        self.draft(
            "record-check",
            "--check",
            "source inspection",
            "--result",
            "passed",
            review_id=review_id,
        )
        self.publish_committed(review_id)

    def publish_lgtm(self) -> None:
        self.guarded_template("final_review")
        self.draft("approve", "--decision", "LGTM")
        self.draft(
            "record-check", "--check", "source inspection", "--result", "passed"
        )
        self.assertTrue(json.loads(self.publish().stdout)["committed"])

    # --- publication ---

    def test_publish_is_clean_and_inspect_exposes_workflow_and_operation(self) -> None:
        event_id = self.prepare_review()
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
        inspected = self.inspect("--json")
        self.assertIn('"status": "clean"', inspected)
        dashboard = json.loads(inspected)
        self.assertIn("lock acquire", dashboard["recommended_next_command"])
        self.assertIn("review_cli.py", dashboard["recommended_next_command"])
        self.assertNotIn("review-json.sh", dashboard["recommended_next_command"])
        conversations = json.loads(
            self.cli("threads", str(self.repo), self.review_id, "--json").stdout
        )
        self.assertEqual(conversations[0]["thread"]["id"], "T1")

    def test_direct_publish_with_wrong_token_is_structured_precommit_failure(
        self,
    ) -> None:
        source = self.snapshot()
        event_id = self.prepare_review()
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
        self.cli("lock", "release", str(self.repo), self.review_id)

    def test_wrong_lock_token_does_not_damage_active_lock(self) -> None:
        self.acquire()
        lease_path = self.repo / ".local" / "reviews" / f"{self.review_id}.lease.json"
        token = json.loads(lease_path.read_text())["token"]
        failed = self.cli(
            "lock", "release", str(self.repo), self.review_id, "wrong-token", check=False
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

    # --- composer artifacts ---

    def test_composer_writes_typed_operations_into_the_guarded_draft(self) -> None:
        initial = json.loads(self.inspect("--json"))
        self.assertEqual(initial["workflow"]["phase"], "awaiting_initial_review")
        self.assertEqual(initial["operation"]["status"], "clean")
        self.assertEqual(initial["operation"]["lock_status"], "unlocked")
        self.assertFalse(initial["operation"]["lease_present"])
        self.assertFalse(initial["source"]["drift"])
        self.assertIn("lock acquire", initial["recommended_next_command"])

        self.acquire()
        guarded = json.loads(self.inspect("--json"))
        self.assertEqual(guarded["operation"]["lock_status"], "locked")
        self.assertTrue(guarded["operation"]["lease_present"])
        self.assertIn("template", guarded["recommended_next_command"])

        templated = self.cli(
            "template", str(self.repo), self.review_id, "review"
        ).stdout.strip()
        self.assertEqual(Path(templated), self.event)
        self.assertEqual(self.event.stat().st_mode & 0o777, 0o600)
        # A review prefills no thread: each one arrives through the composer.
        self.assertEqual(json.loads(self.event.read_text())["operations"], [])

        opened = json.loads(
            self.draft(
                "open-thread",
                "--title",
                "Update example",
                "--risk",
                "Old result remains.",
                "--required-behavior",
                "Use the new result.",
                "--paths",
                "example.txt",
                *EVIDENCE,
            ).stdout
        )
        self.assertEqual(opened["op"], "thread.open")
        self.assertEqual(opened["target"], "T1")
        self.assertEqual(opened["status"], "recorded")

        checked = json.loads(
            self.draft(
                "record-check",
                "--check",
                "schema validation",
                "--result",
                "passed",
            ).stdout
        )
        self.assertEqual(checked["op"], "check.record")
        gapped = json.loads(
            self.draft(
                "open-gap",
                "--check",
                "live probe",
                "--reason",
                "service unavailable",
                "--material",
            ).stdout
        )
        self.assertEqual(gapped["target"], "G1")

        operations = json.loads(self.event.read_text())["operations"]
        self.assertEqual(
            [item["op"] for item in operations],
            ["thread.open", "check.record", "gap.open"],
        )
        self.assertEqual(
            operations[1], {"op": "check.record", "check": "schema validation", "result": "passed"}
        )
        self.assertEqual(
            operations[2],
            {
                "op": "gap.open",
                "gap_id": "G1",
                "check": "live probe",
                "reason": "service unavailable",
                "material": True,
            },
        )

        aborted = json.loads(
            self.cli("abort-draft", str(self.repo), self.review_id).stdout
        )
        self.assertEqual(aborted["status"], "draft_aborted")
        self.assertFalse(self.event.exists())
        released = json.loads(
            self.cli("lock", "release", str(self.repo), self.review_id).stdout
        )
        self.assertEqual(released["status"], "released")

    def test_draft_show_lists_what_the_draft_carries_and_still_owes(self) -> None:
        self.publish_initial_review(self.review_id)
        self.guarded_template("owner_reply")

        outstanding = json.loads(self.draft("show").stdout)
        self.assertEqual(outstanding["kind"], "owner_reply")
        self.assertIn("thread.reply T1", outstanding["operations"])
        self.assertFalse(outstanding["schema_valid"])
        self.assertTrue(outstanding["outstanding"])

        self.compose_owner_reply(["T1"])
        complete = json.loads(self.draft("show").stdout)
        self.assertEqual(complete["outstanding"], [])
        self.assertTrue(complete["schema_valid"])

    def test_composing_the_same_thread_twice_corrects_rather_than_duplicates(
        self,
    ) -> None:
        self.publish_initial_review(self.review_id, thread_count=2)
        self.publish_owner_reply(self.review_id, ["T1", "T2"])
        self.guarded_template("reviewer_update")

        self.draft("comment", "T1", "--message", "Looking at this one.")
        replaced = json.loads(
            self.draft(
                "resolve",
                "T1",
                "--message",
                "Confirmed independently.",
                "--verified",
                *EVIDENCE,
            ).stdout
        )
        self.assertEqual(replaced["status"], "replaced")
        acts = [
            item
            for item in json.loads(self.event.read_text())["operations"]
            if item.get("thread_id") == "T1"
        ]
        self.assertEqual([item["op"] for item in acts], ["thread.resolve"])

    def test_dropping_a_prefilled_resolution_leaves_its_gap_open(self) -> None:
        """A gap that stays open must not hold the draft that templating prefilled.

        `template` writes one resolution skeleton per open gap, but a gap is
        resolved only where the check was finally performed or shown to be
        non-material. Without removal the skeleton could be neither filled
        honestly nor taken out, and the whole draft would have to be aborted.
        """
        self.guarded_template("review")
        self.compose_threads(1)
        self.draft(
            "open-gap", "--check", "live probe", "--reason", "service unavailable"
        )
        self.publish_committed()
        self.publish_owner_reply(self.review_id, ["T1"])

        self.guarded_template("final_review")
        self.assertIn(
            "gap.resolve G1", json.loads(self.draft("show").stdout)["operations"]
        )
        dropped = json.loads(self.draft("drop", "gap.resolve", "G1").stdout)
        self.assertEqual(dropped["status"], "dropped")
        self.assertEqual(dropped["dropped"], 1)

        self.draft("resolve", "T1", "--message", "Verified against the guarded tree.")
        self.draft("approve", "--decision", "LGTM")
        self.draft(
            "record-check", "--check", "source inspection", "--result", "passed"
        )
        self.publish_committed()
        document = json.loads(self.review.read_text())
        self.assertEqual(document["state"]["validation_gaps"]["open"], ["G1"])

    def test_correcting_a_failed_check_drops_the_gap_it_opened(self) -> None:
        self.guarded_template("review")
        self.compose_threads(1)
        failed = json.loads(
            self.draft(
                "record-check",
                "--check",
                "focused tests",
                "--result",
                "failed",
                "--gap-reason",
                "The focused test fails against the guarded tree.",
            ).stdout
        )
        self.assertEqual(failed["target"], "focused tests")
        self.assertEqual(failed["gap_id"], "G1")
        self.assertEqual(failed["dropped"], 0)

        corrected = json.loads(
            self.draft(
                "record-check", "--check", "focused tests", "--result", "passed"
            ).stdout
        )
        self.assertEqual(corrected["status"], "replaced")
        self.assertEqual(corrected["dropped"], 1)
        self.assertEqual(
            [item["op"] for item in json.loads(self.event.read_text())["operations"]],
            ["thread.open", "check.record", "check.record"],
        )
        self.publish_committed()
        document = json.loads(self.review.read_text())
        self.assertEqual(document["state"]["validation_gaps"]["open"], [])

    def test_dropping_refuses_an_operation_the_draft_does_not_carry(self) -> None:
        self.guarded_template("review")
        self.compose_threads(1)

        refused = self.draft("drop", "thread.open", "T2", check=False)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("thread.open T1", refused.stderr)

    def test_a_record_checks_acknowledged_target_is_what_drop_accepts(self) -> None:
        """The one two-operation composer must still name itself, not the gap it opened.

        Reporting the gap ID here printed an acknowledgment no `drop` could act on, so
        the only way to take the check back was to abort the whole draft.
        """
        self.guarded_template("review")
        # The fixture already records one passing check, so the draft carries two
        # `check.record` operations and the drop below has to select by the target it
        # was handed rather than by the operation name alone.
        self.compose_threads(1)
        recorded = json.loads(
            self.draft(
                "record-check",
                "--check",
                "focused tests",
                "--result",
                "failed",
                "--gap-reason",
                "The focused test fails against the guarded tree.",
            ).stdout
        )
        self.assertEqual(recorded["target"], "focused tests")
        self.assertEqual(recorded["gap_id"], "G1")

        dropped = json.loads(
            self.draft("drop", recorded["op"], recorded["target"]).stdout
        )

        self.assertEqual(dropped["status"], "dropped")
        # Exactly the named check, and nothing else: its gap is not stranded by the
        # removal, and the unrelated passing check is untouched.
        self.assertEqual(dropped["dropped"], 1)
        operations = json.loads(self.event.read_text())["operations"]
        self.assertEqual(
            [item["check"] for item in operations if item["op"] == "check.record"],
            ["source inspection"],
        )
        # A validation gap outlives the check that revealed it until that check is
        # recorded as passing.
        self.assertEqual(
            [item["gap_id"] for item in operations if item["op"] == "gap.open"], ["G1"]
        )

    # --- await-handoff ---

    def await_handoff(
        self, round_seconds: int, max_rounds: int
    ) -> subprocess.CompletedProcess[str]:
        return self.cli(
            "await-handoff",
            str(self.repo),
            self.review_id,
            "--round-seconds",
            str(round_seconds),
            "--max-rounds",
            str(max_rounds),
            check=False,
        )

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
        self.prepare_review()
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
        self.prepare_review()
        self.publish()
        # Back-date the only event beyond the two-hour owner deadline. History,
        # derived latest_event, and operation evidence observed_at must stay
        # mutually consistent for the document to remain valid.
        document = json.loads(self.review.read_text())
        stale = (
            datetime.now(timezone.utc) - timedelta(hours=3)
        ).isoformat().replace("+00:00", "Z")
        document["history"][-1]["occurred_at"] = stale
        document["state"]["latest_event"]["occurred_at"] = stale
        for operation in document["history"][-1]["operations"]:
            if "evidence" in operation:
                operation["evidence"]["observed_at"] = stale
        write_json(self.review, document)
        self.cli("validate", str(self.repo), self.review_id)
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

    # --- notes ---

    def test_a_note_attaches_to_a_thread_the_draft_acts_on(self) -> None:
        self.guarded_template("review")
        self.compose_threads(1)
        noted = json.loads(
            self.draft(
                "note",
                "T1",
                "--tag",
                "decision",
                "--message",
                "raised as a design constraint",
            ).stdout
        )
        self.assertEqual(noted["op"], "note.attach")
        self.assertEqual(noted["target"], "T1")
        operations = json.loads(self.event.read_text())["operations"]
        self.assertEqual(
            operations[-1],
            {
                "op": "note.attach",
                "target": {"kind": "thread", "id": "T1"},
                "tag": "decision",
                "message": "raised as a design constraint",
            },
        )
        failed = self.draft(
            "note", "T9", "--tag", "decision", "--message", "no such thread", check=False
        )
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("no entry for thread", failed.stderr)

    def test_a_note_round_trips_into_the_report_notes_section(self) -> None:
        self.prepare_review()
        self.publish()

        self.guarded_template("owner_reply")
        self.compose_owner_reply(["T1"])
        self.draft(
            "note",
            "T1",
            "--tag",
            "decision",
            "--message",
            "design shifted to eventual consistency",
        )
        # No manual occurred_at repair: every composer call restamps the draft,
        # so the publish below also proves the timestamp refresh.
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

    # --- source_update ---

    def test_source_update_validates_once_its_reason_is_composed(self) -> None:
        self.prepare_review()
        self.publish()
        self.guarded_template("source_update")
        self.draft(
            "replace-source",
            "--reason",
            "Replacement basis for continued review.",
        )
        # Composing the one semantic blank must validate; no generated field may
        # need deleting and no snapshot needs retyping.
        result = self.cli(
            "validate-event", str(self.repo), self.review_id, check=False
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
        self.inspect("--json")
        result = self.cli(
            "publish-timeout", "--if-eligible", str(self.repo), self.review_id
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
            self.cli("scope-candidates", str(self.repo)).stdout
        )
        self.assertIsNone(result["merge_base"])
        self.assertIn("example.txt", result["candidates"]["unstaged"])
        self.assertIn("new.txt", result["candidates"]["untracked"])
        self.assertEqual(result["union"], ["example.txt", "new.txt"])

    def test_scope_candidates_with_base_ref_reports_merge_base_diff(self) -> None:
        with_base = json.loads(
            self.cli("scope-candidates", str(self.repo), "HEAD").stdout
        )
        self.assertTrue(with_base["merge_base"])
        self.assertEqual(with_base["candidates"]["merge_base_diff"], [])

    # --- composer timestamp refresh ---

    def test_composing_restamps_occurred_at(self) -> None:
        self.guarded_template("review")
        templated_at = json.loads(self.event.read_text())["occurred_at"]
        self.draft(
            "record-check", "--check", "schema validation", "--result", "passed"
        )
        restamped_at = json.loads(self.event.read_text())["occurred_at"]
        self.assertGreater(restamped_at, templated_at)

    # --- owner-reply handoff metadata ---

    def test_reply_context_derives_changed_files_and_revisions(self) -> None:
        self.prepare_review()
        self.publish()
        (self.repo / "example.txt").write_text("after\n")
        run("git", "add", "example.txt", cwd=self.repo)
        run("git", "commit", "-qm", "apply the finding", cwd=self.repo)
        head = run("git", "rev-parse", "HEAD", cwd=self.repo).stdout.strip()

        self.guarded_template("owner_reply")
        recorded = json.loads(
            self.draft(
                "reply-context",
                "--drift",
                "Only the guarded source changed.",
                "--guide",
                "No behavior guide needed a change.",
            ).stdout
        )
        self.assertEqual(recorded["revisions"], 1)

        draft = json.loads(self.event.read_text())
        self.assertEqual(draft["changed_files"], ["example.txt"])
        self.assertEqual(draft["revisions"], [head])
        self.assertEqual(
            draft["source_drift_assessment"], "Only the guarded source changed."
        )

    # --- follow-up routing ---

    def test_initial_final_review_and_source_drift_route_to_follow_up(self) -> None:
        self.publish_lgtm()

        (self.repo / "example.txt").write_text("after\n")
        dashboard = json.loads(self.inspect("--json"))
        self.assertEqual(dashboard["workflow"]["phase"], "terminal")
        self.assertTrue(dashboard["source"]["drift"])
        self.assertTrue(dashboard["source"]["approval_stale"])
        self.assertIn("start-follow-up", dashboard["recommended_next_command"])

    # --- accretion ledger and structure chaining ---

    def start_grown_review(self, name: str) -> str:
        """Open a loop over example.txt and grow the file past the growth threshold."""
        output = self.cli("init", str(self.repo), name, "--base-ref", "HEAD").stdout
        review_id = self.review_id_of(output)
        (self.repo / "example.txt").write_text("before\n" * 10)
        return review_id

    def test_ledger_is_emitted_only_where_a_final_review_may_carry_it(self) -> None:
        review_id = self.start_grown_review("ledger-review")

        # awaiting_initial_review allows final_review, so the acknowledgment the
        # ledger feeds is reachable and the dashboard carries it.
        eligible = json.loads(self.inspect("--json", review_id=review_id))
        self.assertEqual(eligible["accretion"]["flagged"], ["example.txt"])

        self.publish_initial_review(review_id)

        # owner_response allows only owner_reply, which has no structure_debt.
        routine = json.loads(self.inspect("--json", review_id=review_id))
        self.assertEqual(routine["workflow"]["phase"], "owner_response")
        self.assertIsNone(routine["accretion"])

        requested = json.loads(
            self.inspect("--json", "--accretion", review_id=review_id)
        )
        self.assertEqual(requested["accretion"]["flagged"], ["example.txt"])

        # The human form keeps showing the flagged set while the loop is active.
        self.assertIn(
            "accretion_flagged: example.txt", self.inspect(review_id=review_id)
        )

    def test_agent_view_is_the_card_alone_and_names_the_phase_obligations(
        self,
    ) -> None:
        review_id = self.start_grown_review("card-review")
        self.publish_initial_review(review_id)

        card = json.loads(self.inspect("--agent", review_id=review_id))

        self.assertEqual(card["phase"], "owner_response")
        self.assertEqual(card["action"], "publish_owner_reply")
        self.assertEqual(card["open_threads"], ["T1"])
        self.assertIn("reply_to_every_open_thread", card["must"])
        self.assertIn("resolve_thread", card["must_not"])
        self.assertIn("hand_edit_draft_json", card["must_not"])
        self.assertTrue(card["common_path_applies"])
        # The card replaces the dashboard rather than decorating it.
        self.assertNotIn("accretion", card)
        self.assertNotIn("source", card)

    def test_thread_summary_drops_bodies_and_open_filters_resolved_threads(
        self,
    ) -> None:
        review_id = self.start_grown_review("threads-review")
        self.publish_initial_review(review_id, thread_count=2)

        def threads(*flags: str) -> str:
            return self.cli("threads", str(self.repo), review_id, *flags).stdout

        summary = json.loads(threads("--summary", "--open", "--json"))
        self.assertEqual(
            summary,
            [
                {
                    "id": "T1",
                    "priority": "P1",
                    "status": "open",
                    "title": "Update example",
                    "paths": ["example.txt"],
                },
                {
                    "id": "T2",
                    "priority": "P1",
                    "status": "open",
                    "title": "Finding 2",
                    "paths": ["example.txt"],
                },
            ],
        )
        self.assertIn(
            "- T1 [P1] open: Update example (example.txt)",
            threads("--summary", "--open"),
        )

        # Resolve T1 and leave T2 open, which is the state routing has to tell apart.
        self.publish_owner_reply(review_id, ["T1", "T2"])
        self.publish_reviewer_update(review_id, resolve="T1", comment=["T2"])

        self.assertEqual(
            [item["id"] for item in json.loads(threads("--summary", "--open", "--json"))],
            ["T2"],
        )
        # An all-thread summary still reports T1 as historical context.
        every = json.loads(threads("--summary", "--json"))
        self.assertEqual(
            [(item["id"], item["status"]) for item in every],
            [("T1", "resolved"), ("T2", "open")],
        )

        # The full view keeps every body the summary drops, on both threads.
        conversations = json.loads(threads("--json"))
        self.assertEqual(conversations[0]["thread"]["risk"], "Old result remains.")
        self.assertEqual(
            conversations[0]["thread"]["required_behavior"], "Use the new result."
        )
        self.assertIn(
            "Applied the new result.",
            [entry["entry"]["message"] for entry in conversations[0]["conversation"]],
        )
        self.assertIn(
            "Confirmed the new result independently.",
            [entry["entry"]["message"] for entry in conversations[0]["conversation"]],
        )

    def test_accretion_flags_chain_a_structure_round(self) -> None:
        base = run("git", "rev-parse", "HEAD", cwd=self.repo).stdout.strip()
        review_id = self.start_grown_review("accretion-review")
        reviews = self.repo / ".local" / "reviews"
        canonical = reviews / f"{review_id}.json"
        self.assertEqual(json.loads(canonical.read_text())["comparison_base"], base)

        # Walk the guarded final_review path: the ledger must flag the grown file
        # and the template must prefill the acknowledgment the composer disposes of.
        dashboard = json.loads(self.inspect("--json", review_id=review_id))
        self.assertEqual(dashboard["accretion"]["flagged"], ["example.txt"])
        self.guarded_template("final_review", review_id)
        draft_path = self.draft_path(review_id)
        self.assertEqual(
            json.loads(draft_path.read_text())["operations"][-1]["structure_debt"][
                "flagged_paths"
            ],
            ["example.txt"],
        )

        # The composer refuses an approval that leaves the flagged files undisposed.
        refused = self.draft(
            "approve", "--decision", "LGTM", review_id=review_id, check=False
        )
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("structure-disposition", refused.stderr)

        self.draft(
            "approve",
            "--decision",
            "LGTM",
            "--structure-disposition",
            "structure_deferred",
            "--structure-message",
            "Real accretion; chain a structure round.",
            review_id=review_id,
        )
        self.draft(
            "record-check",
            "--check",
            "source inspection",
            "--result",
            "passed",
            review_id=review_id,
        )

        # The publish gate stays the belt: a hand-stripped acknowledgment is
        # refused before the commit point even though the composer wrote one.
        composed = json.loads(draft_path.read_text())
        stripped = json.loads(json.dumps(composed))
        for operation in stripped["operations"]:
            operation.pop("structure_debt", None)
        write_json(draft_path, stripped)
        refused_publish = json.loads(
            self.cli("publish", str(self.repo), review_id, check=False).stdout
        )
        self.assertEqual(refused_publish["status"], "precommit_failed")
        self.assertIn("structure_debt", refused_publish["detail"])

        write_json(draft_path, composed)
        self.publish_committed(review_id)

        # The deferred terminal recommends chaining the structure round.
        dashboard = json.loads(self.inspect("--json", review_id=review_id))
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
        successor_id = self.review_id_of(follow_up)
        successor = json.loads((reviews / f"{successor_id}.json").read_text())
        self.assertEqual(successor["review_kind"], "structure")
        self.assertEqual(successor["prior_review_id"], review_id)
        self.assertEqual(successor["comparison_base"], base)

        # One structure round consumes the flag set: the prior terminal stops
        # recommending another.
        dashboard = json.loads(self.inspect("--json", review_id=review_id))
        self.assertEqual(dashboard["recommended_next_command"], "none")


if __name__ == "__main__":
    unittest.main()
