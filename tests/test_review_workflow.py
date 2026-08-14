"""Unit tests for the workflow waiting helpers."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
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
