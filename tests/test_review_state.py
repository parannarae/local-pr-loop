"""Tests for the schema-v2 thread lifecycle and state projection."""

from __future__ import annotations

import importlib.util
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

MODULE_PATH = Path(__file__).parents[1] / "scripts" / "review_state.py"
SPEC = importlib.util.spec_from_file_location("review_state", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
review_state = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(review_state)

BASE_TIME = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=3)


def timestamp(offset_seconds: int) -> str:
    return (BASE_TIME + timedelta(seconds=offset_seconds)).isoformat()


def snapshot(marker: str) -> dict[str, Any]:
    return {
        "revision": marker * 40,
        "scope": ["src/example.py", "tests/test_example.py"],
        "fingerprint": marker * 64,
        "exclusions": [],
        "additional_inputs": [],
    }


def validation() -> dict[str, list[str]]:
    return {
        "performed": ["focused tests passed"],
        "unavailable": [],
        "remaining_gaps": [],
    }


def thread(thread_id: str, title: str) -> dict[str, str]:
    return {
        "id": thread_id,
        "priority": "P1",
        "title": title,
        "risk": f"{title} risk",
        "evidence": f"{title} evidence",
        "required_behavior": f"{title} requirement",
    }


def review_event(*threads: dict[str, str]) -> dict[str, Any]:
    return {
        "kind": "review",
        "status": "OWNER ACTION REQUIRED",
        "role": "Reviewer",
        "submitted_at": timestamp(1),
        "source_snapshot": snapshot("1"),
        "threads": list(threads),
        "validation": validation(),
    }


def owner_reply_event(
    open_thread_ids: list[str],
    *,
    starting_snapshot: dict[str, Any],
    completed_snapshot: dict[str, Any],
    offset_seconds: int,
) -> dict[str, Any]:
    return {
        "kind": "owner_reply",
        "status": "REVIEWER ACTION REQUIRED",
        "role": "Owner",
        "completed_at": timestamp(offset_seconds),
        "starting_source_snapshot": deepcopy(starting_snapshot),
        "source_drift_assessment": "Only declared source changed.",
        "completed_source_snapshot": deepcopy(completed_snapshot),
        "replies": [
            {
                "thread_id": thread_id,
                "decision": "applied",
                "message": f"Applied {thread_id}.",
                "evidence": f"Tests cover {thread_id}.",
            }
            for thread_id in open_thread_ids
        ],
        "files_changed": ["src/example.py", "tests/test_example.py"],
        "guide_synchronization": "No guide changes were needed.",
        "validation": validation(),
        "commits": [],
    }


def append(document: dict[str, Any], event: dict[str, Any]) -> None:
    review_state.append_event(document, event)
    errors = review_state.validate_document(document)
    if errors:
        raise AssertionError("\n".join(errors))


class ReviewStateV2Test(unittest.TestCase):
    def test_new_document_uses_thread_projection_state(self) -> None:
        document = review_state.new_document("abcdefgh", "thread-review")

        self.assertEqual(document["schema_version"], 2)
        self.assertEqual(document["state"], review_state.default_state())
        self.assertEqual(review_state.validate_document(document), [])

    def test_thread_lifecycle_resolves_and_reuses_stable_ids(self) -> None:
        document = review_state.new_document("abcdefgh", "thread-review")
        append(document, review_event(thread("T1", "First"), thread("T2", "Second")))
        self.assertEqual(document["state"]["open_threads"], ["T1", "T2"])

        append(
            document,
            owner_reply_event(
                ["T1", "T2"],
                starting_snapshot=snapshot("1"),
                completed_snapshot=snapshot("2"),
                offset_seconds=2,
            ),
        )
        append(
            document,
            {
                "kind": "reviewer_update",
                "status": "OWNER ACTION REQUIRED",
                "role": "Reviewer",
                "submitted_at": timestamp(3),
                "source_snapshot": snapshot("2"),
                "decisions": [
                    {
                        "thread_id": "T1",
                        "action": "resolve",
                        "message": "The first issue is verified.",
                    },
                    {
                        "thread_id": "T2",
                        "action": "comment",
                        "message": "The second issue still needs work.",
                    },
                ],
                "new_threads": [thread("T3", "Regression")],
                "validation": validation(),
            },
        )
        self.assertEqual(document["state"]["open_threads"], ["T2", "T3"])
        self.assertEqual(document["state"]["resolved_threads"], ["T1"])

        append(
            document,
            owner_reply_event(
                ["T2", "T3"],
                starting_snapshot=snapshot("2"),
                completed_snapshot=snapshot("3"),
                offset_seconds=4,
            ),
        )
        append(
            document,
            {
                "kind": "final_review",
                "status": "LGTM",
                "role": "Reviewer",
                "completed_at": timestamp(5),
                "source_snapshot": snapshot("3"),
                "resolutions": [
                    {"thread_id": "T2", "message": "The second issue is verified."},
                    {"thread_id": "T3", "message": "The regression is fixed."},
                ],
                "decision": "All threads are resolved against the current source.",
                "validation": validation(),
            },
        )

        self.assertEqual(document["state"]["marker"], "LGTM")
        self.assertEqual(document["state"]["open_threads"], [])
        self.assertEqual(document["state"]["resolved_threads"], ["T1", "T2", "T3"])

    def test_review_rejects_unknown_thread_priority(self) -> None:
        event = review_event(thread("T1", "First"))
        event["threads"][0]["priority"] = "urgent"

        self.assertIn(
            "threads[0].priority must be one of P0, P1, P2, P3",
            review_state.validate_event(event),
        )

    def test_rejected_event_does_not_mutate_document(self) -> None:
        document = review_state.new_document("abcdefgh", "thread-review")
        original = deepcopy(document)
        event = review_event(thread("T1", "First"))
        event["threads"][0]["priority"] = "urgent"

        with self.assertRaisesRegex(ValueError, "priority"):
            review_state.append_event(document, event)

        self.assertEqual(document, original)

    def test_source_update_can_change_scope_without_thread_impacts(self) -> None:
        document = review_state.new_document("abcdefgh", "thread-review")
        append(document, review_event(thread("T1", "First")))
        append(
            document,
            {
                "kind": "source_update",
                "status": "OWNER ACTION REQUIRED",
                "role": "Reviewer",
                "submitted_at": timestamp(2),
                "source_snapshot": snapshot("2"),
                "reason": "Added an omitted changed configuration file.",
                "thread_impacts": [],
                "new_threads": [],
                "validation": validation(),
            },
        )

        self.assertEqual(document["state"]["open_threads"], ["T1"])
        self.assertEqual(document["state"]["current_source_fingerprint"], "2" * 64)
        self.assertIn(
            "## Thread Impacts\n\n- None", review_state.render_report(document)
        )
        append(
            document,
            owner_reply_event(
                ["T1"],
                starting_snapshot=snapshot("2"),
                completed_snapshot=snapshot("3"),
                offset_seconds=3,
            ),
        )

    def test_source_update_can_open_threads_discovered_in_expanded_scope(self) -> None:
        document = review_state.new_document("abcdefgh", "thread-review")
        append(document, review_event(thread("T1", "First")))
        append(
            document,
            {
                "kind": "source_update",
                "status": "OWNER ACTION REQUIRED",
                "role": "Reviewer",
                "submitted_at": timestamp(2),
                "source_snapshot": snapshot("2"),
                "reason": "The expanded scope revealed another issue.",
                "thread_impacts": [],
                "new_threads": [thread("T2", "New scope regression")],
                "validation": validation(),
            },
        )

        self.assertEqual(document["state"]["open_threads"], ["T1", "T2"])
        self.assertIn(
            "## Newly Opened Threads\n\n### T2",
            review_state.render_report(document),
        )

    def test_source_update_can_reopen_a_resolved_thread(self) -> None:
        document = review_state.new_document("abcdefgh", "thread-review")
        append(document, review_event(thread("T1", "First"), thread("T2", "Second")))
        append(
            document,
            owner_reply_event(
                ["T1", "T2"],
                starting_snapshot=snapshot("1"),
                completed_snapshot=snapshot("2"),
                offset_seconds=2,
            ),
        )
        append(
            document,
            {
                "kind": "reviewer_update",
                "status": "OWNER ACTION REQUIRED",
                "role": "Reviewer",
                "submitted_at": timestamp(3),
                "source_snapshot": snapshot("2"),
                "decisions": [
                    {"thread_id": "T1", "action": "resolve", "message": "Verified."},
                    {"thread_id": "T2", "action": "comment", "message": "Still open."},
                ],
                "new_threads": [],
                "validation": validation(),
            },
        )
        append(
            document,
            {
                "kind": "source_update",
                "status": "OWNER ACTION REQUIRED",
                "role": "Reviewer",
                "submitted_at": timestamp(4),
                "source_snapshot": snapshot("3"),
                "reason": "New source invalidates the prior resolution.",
                "thread_impacts": [
                    {
                        "thread_id": "T1",
                        "action": "reopen",
                        "message": "The resolved behavior regressed.",
                    }
                ],
                "new_threads": [],
                "validation": validation(),
            },
        )

        self.assertEqual(document["state"]["open_threads"], ["T1", "T2"])
        self.assertEqual(document["state"]["resolved_threads"], [])

    def test_source_update_can_return_reviewer_work_to_owner(self) -> None:
        document = review_state.new_document("abcdefgh", "thread-review")
        append(document, review_event(thread("T1", "First")))
        append(
            document,
            owner_reply_event(
                ["T1"],
                starting_snapshot=snapshot("1"),
                completed_snapshot=snapshot("2"),
                offset_seconds=2,
            ),
        )
        append(
            document,
            {
                "kind": "source_update",
                "status": "OWNER ACTION REQUIRED",
                "role": "Reviewer",
                "submitted_at": timestamp(3),
                "source_snapshot": snapshot("3"),
                "reason": "Reviewer verification found an omitted changed input.",
                "thread_impacts": [],
                "new_threads": [],
                "validation": validation(),
            },
        )

        self.assertEqual(document["state"]["marker"], "OWNER ACTION REQUIRED")
        self.assertEqual(document["state"]["open_threads"], ["T1"])

    def test_final_review_rejects_missing_open_thread_resolution(self) -> None:
        document = review_state.new_document("abcdefgh", "thread-review")
        append(document, review_event(thread("T1", "First"), thread("T2", "Second")))
        append(
            document,
            owner_reply_event(
                ["T1", "T2"],
                starting_snapshot=snapshot("1"),
                completed_snapshot=snapshot("2"),
                offset_seconds=2,
            ),
        )
        event = {
            "kind": "final_review",
            "status": "LGTM",
            "role": "Reviewer",
            "completed_at": timestamp(3),
            "source_snapshot": snapshot("2"),
            "resolutions": [{"thread_id": "T1", "message": "Verified."}],
            "decision": "Approve.",
            "validation": validation(),
        }

        with self.assertRaisesRegex(ValueError, "resolve every open thread"):
            review_state.append_event(document, event)

    def test_initial_review_can_approve_without_threads(self) -> None:
        document = review_state.new_document("abcdefgh", "thread-review")
        append(
            document,
            {
                "kind": "final_review",
                "status": "LGTM",
                "role": "Reviewer",
                "completed_at": timestamp(1),
                "source_snapshot": snapshot("1"),
                "resolutions": [],
                "decision": "No findings remain.",
                "validation": validation(),
            },
        )

        self.assertEqual(document["state"]["marker"], "LGTM")
        self.assertEqual(document["state"]["open_threads"], [])

    def test_reviewer_update_rejects_unaddressed_open_thread(self) -> None:
        document = review_state.new_document("abcdefgh", "thread-review")
        append(document, review_event(thread("T1", "First"), thread("T2", "Second")))
        append(
            document,
            owner_reply_event(
                ["T1", "T2"],
                starting_snapshot=snapshot("1"),
                completed_snapshot=snapshot("2"),
                offset_seconds=2,
            ),
        )
        event = {
            "kind": "reviewer_update",
            "status": "OWNER ACTION REQUIRED",
            "role": "Reviewer",
            "submitted_at": timestamp(3),
            "source_snapshot": snapshot("2"),
            "decisions": [
                {"thread_id": "T1", "action": "resolve", "message": "Verified."}
            ],
            "new_threads": [],
            "validation": validation(),
        }

        with self.assertRaisesRegex(ValueError, "every open thread"):
            review_state.append_event(document, event)

    def test_owner_reply_rejects_guarded_scope_change(self) -> None:
        document = review_state.new_document("abcdefgh", "thread-review")
        append(document, review_event(thread("T1", "First")))
        completed_snapshot = snapshot("2")
        completed_snapshot["scope"] = ["src/other.py"]
        event = owner_reply_event(
            ["T1"],
            starting_snapshot=snapshot("1"),
            completed_snapshot=completed_snapshot,
            offset_seconds=2,
        )

        with self.assertRaisesRegex(ValueError, "guarded source basis"):
            review_state.append_event(document, event)

    def test_owner_timeout_uses_latest_owner_handoff(self) -> None:
        document = review_state.new_document("abcdefgh", "thread-review")
        append(document, review_event(thread("T1", "First")))
        started_at = timestamp(1)
        deadline = (
            datetime.fromisoformat(started_at)
            + review_state.TIMEOUT_DURATION_BY_KIND["owner_timeout"]
        ).isoformat()
        append(
            document,
            {
                "kind": "owner_timeout",
                "status": "OWNER TIMED OUT",
                "role": "Reviewer",
                "timed_out_at": deadline,
                "reason": "The owner did not reply.",
                "started_at": started_at,
                "deadline": deadline,
            },
        )

        self.assertEqual(document["state"]["marker"], "OWNER TIMED OUT")
        self.assertIn(
            f"- Source fingerprint: {'1' * 64}",
            review_state.render_report(document),
        )

    def test_unsupported_schema_version_is_rejected(self) -> None:
        document = review_state.new_document("abcdefgh", "thread-review")
        document["schema_version"] = 99

        self.assertIn(
            "schema_version must be integer 2", review_state.validate_document(document)
        )

    def test_malformed_thread_id_returns_validation_error(self) -> None:
        document = review_state.new_document("abcdefgh", "thread-review")
        append(document, review_event(thread("T1", "First")))
        document["history"].append(
            {
                "kind": "source_update",
                "status": "OWNER ACTION REQUIRED",
                "role": "Reviewer",
                "submitted_at": timestamp(2),
                "source_snapshot": snapshot("2"),
                "reason": "Malformed impact.",
                "thread_impacts": [
                    {"thread_id": [], "action": "comment", "message": "Invalid ID."}
                ],
                "new_threads": [],
                "validation": validation(),
            }
        )

        errors = review_state.validate_document(document)

        self.assertTrue(
            any(
                "thread_impacts[0].thread_id must match T<N>" in error
                for error in errors
            )
        )
        self.assertTrue(
            any(
                "thread_impacts[0] references unknown thread" in error
                for error in errors
            )
        )

    def test_malformed_reviewer_thread_id_returns_validation_error(self) -> None:
        document = review_state.new_document("abcdefgh", "thread-review")
        append(document, review_event(thread("T1", "First")))
        append(
            document,
            owner_reply_event(
                ["T1"],
                starting_snapshot=snapshot("1"),
                completed_snapshot=snapshot("2"),
                offset_seconds=2,
            ),
        )
        document["history"].append(
            {
                "kind": "reviewer_update",
                "status": "OWNER ACTION REQUIRED",
                "role": "Reviewer",
                "submitted_at": timestamp(3),
                "source_snapshot": snapshot("2"),
                "decisions": [
                    {"thread_id": [], "action": "comment", "message": "Invalid ID."}
                ],
                "new_threads": [],
                "validation": validation(),
            }
        )

        errors = review_state.validate_document(document)

        self.assertTrue(
            any("decisions[0].thread_id must match T<N>" in error for error in errors)
        )
        self.assertTrue(
            any("decisions[0] references unknown thread" in error for error in errors)
        )

    def test_templates_use_stable_thread_contract(self) -> None:
        review = review_state.event_template("review")
        source_update = review_state.event_template("source_update")
        owner_reply = review_state.event_template("owner_reply")

        self.assertEqual(review["threads"][0]["id"], "T1")
        self.assertEqual(source_update["thread_impacts"], [])
        self.assertEqual(source_update["new_threads"], [])
        self.assertEqual(owner_reply["replies"], [])


if __name__ == "__main__":
    unittest.main()
