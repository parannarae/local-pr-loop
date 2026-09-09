"""Unit tests for the workflow waiting helpers."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import review_workflow


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


def write_handoff_document(path: Path, marker: str, elapsed: float) -> str:
    """Write a reviewer_verification document whose handoff started `elapsed` ago.

    The reviewer handoff runs 1800s, so `elapsed` above that puts the deadline in
    the past and below it leaves the deadline that many seconds ahead.
    """
    occurred = datetime.now(timezone.utc) - timedelta(seconds=elapsed)
    document = {
        "name": marker,
        "state": {
            "workflow": {
                "phase": "reviewer_verification",
                "primary_actor": "reviewer",
                "primary_action": {"kind": "verify_owner_reply"},
            },
            "latest_event": {"occurred_at": occurred.isoformat()},
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

    def test_a_handoff_deadline_still_ahead_cuts_the_wait_short(self) -> None:
        # The waiting actor should learn a timeout became eligible promptly rather
        # than sitting out a bound many times longer than the deadline.
        baseline = write_handoff_document(self.review, "nearly-due", 1799.5)

        started = time.monotonic()
        result = review_workflow.poll_for_change(self.review, 30, baseline)

        self.assertEqual(result["status"], "deadline_reached")
        self.assertLess(time.monotonic() - started, 5)

    def test_a_passed_handoff_deadline_no_longer_shortens_the_wait(self) -> None:
        # The defect: with the deadline behind it, the loop's own deadline was in the
        # past, so the call returned without polling and every later call did too.
        baseline = write_handoff_document(self.review, "overdue", 3600)

        started = time.monotonic()
        result = review_workflow.poll_for_change(self.review, 1, baseline)

        self.assertEqual(
            result, {"status": "deadline_reached", "canonical_sha256": baseline}
        )
        self.assertGreaterEqual(time.monotonic() - started, 0.9)

    def test_a_change_after_a_passed_deadline_is_reported_rather_than_missed(
        self,
    ) -> None:
        baseline = write_handoff_document(self.review, "overdue", 3600)
        changed: list[str] = []

        def publish_during_the_wait() -> None:
            time.sleep(0.05)
            changed.append(write_handoff_document(self.review, "published", 3600))

        writer = threading.Thread(target=publish_during_the_wait)
        writer.start()
        try:
            result = review_workflow.poll_for_change(self.review, 1, baseline)
        finally:
            writer.join()

        self.assertEqual(
            result, {"status": "changed", "canonical_sha256": changed[0]}
        )

    def test_the_caller_bound_still_wins_over_a_distant_handoff_deadline(self) -> None:
        # A deadline ahead of the bound must not extend the wait past what was asked.
        baseline = write_handoff_document(self.review, "fresh", 0)

        started = time.monotonic()
        result = review_workflow.poll_for_change(self.review, 1, baseline)

        self.assertEqual(result, {"status": "timeout", "canonical_sha256": baseline})
        self.assertLess(time.monotonic() - started, 5)

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
        ) as poll, contextlib.redirect_stdout(io.StringIO()) as stdout:
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
