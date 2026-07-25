"""Tests for calendar-revision workflow projection and evidence gates."""

from __future__ import annotations

import importlib.util
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

MODULE = Path(__file__).parents[1] / "scripts" / "review_state.py"
SPEC = importlib.util.spec_from_file_location("review_state", MODULE)
assert SPEC and SPEC.loader
review_state = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(review_state)
BASE = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=3)


def at(seconds: int) -> str:
    return (BASE + timedelta(seconds=seconds)).isoformat()


def snapshot(marker: str) -> dict[str, Any]:
    return {
        "revision": marker * 40,
        "scope": ["example.txt"],
        "fingerprint": marker * 64,
        "exclusions": [],
        "additional_inputs": [],
        "staged_sha256": "0" * 64,
        "unstaged_sha256": "0" * 64,
        "untracked": [],
    }


def evidence(basis: str = "source_inspection") -> dict[str, Any]:
    value = {
        "basis": basis,
        "provenance": "example.txt",
        "observed_at": at(0),
        "sanitized_result": "Observed the declared behavior.",
    }
    if basis == "captured_fixture":
        value["artifact_digest"] = "a" * 64
    return value


def validation(*, material: bool = False) -> dict[str, Any]:
    return {
        "performed": [{"check": "focused test", "result": "passed"}],
        "gaps": (
            [
                {
                    "gap_id": "G1",
                    "check": "live service",
                    "reason": "unavailable",
                    "material": True,
                }
            ]
            if material
            else []
        ),
    }


def thread(
    thread_id: str = "T1",
    *,
    contract: str = "internal",
    priority: str = "P1",
    basis: str = "source_inspection",
) -> dict[str, Any]:
    return {
        "id": thread_id,
        "priority": priority,
        "contract": contract,
        "title": "Behavior differs",
        "risk": "Users receive an incorrect result.",
        "evidence": evidence(basis),
        "required_behavior": "Return the documented result.",
    }


def event(kind: str, sequence: int) -> dict[str, Any]:
    value = review_state.event_template(kind)
    value["event_id"] = f"evt_test_{sequence:04d}"
    value["occurred_at"] = at(sequence)
    return value


def review_event() -> dict[str, Any]:
    value = event("review", 1)
    value.update(
        {
            "source_snapshot": snapshot("1"),
            "threads": [thread()],
            "validation": validation(),
        }
    )
    return value


def owner_event(decision: str = "applied") -> dict[str, Any]:
    value = event("owner_reply", 2)
    value.update(
        {
            "starting_source_snapshot": snapshot("1"),
            "source_drift_assessment": "Only guarded source changed.",
            "completed_source_snapshot": snapshot("2"),
            "replies": [
                {
                    "thread_id": "T1",
                    "decision": decision,
                    "message": "Handled the finding.",
                    "evidence": evidence("test_result"),
                }
            ],
            "files_changed": ["example.txt"],
            "guide_synchronization": "No guide change was needed.",
            "validation": validation(),
            "commits": [],
        }
    )
    return value


class ReviewStateTest(unittest.TestCase):
    def test_new_document_uses_calendar_revision_and_workflow(self) -> None:
        document = review_state.new_document("abcdefgh", "review")
        self.assertEqual(document["format"], "local-pr-loop")
        self.assertEqual(document["format_revision"], "2026-07-25.2")
        self.assertEqual(document["created_by"]["version"], "0.3.0")
        self.assertEqual(
            document["state"]["workflow"]["phase"], "awaiting_initial_review"
        )

    def test_multi_actor_routing_is_projected(self) -> None:
        document = review_state.new_document("abcdefgh", "review")
        review_state.append_event(document, review_event())
        workflow = document["state"]["workflow"]
        self.assertEqual(workflow["primary_actor"], "owner")
        self.assertEqual(workflow["allowed_events_by_actor"]["owner"], ["owner_reply"])
        self.assertIn("source_update", workflow["allowed_events_by_actor"]["reviewer"])

    def test_unique_event_ids_and_strict_time_order_are_required(self) -> None:
        document = review_state.new_document("abcdefgh", "review")
        review_state.append_event(document, review_event())
        duplicate = event("source_update", 1)
        duplicate["event_id"] = "evt_test_0001"
        duplicate.update(
            {
                "source_snapshot": snapshot("2"),
                "reason": "Changed source.",
                "thread_impacts": [],
                "new_threads": [],
                "validation": validation(),
            }
        )
        with self.assertRaisesRegex(ValueError, "event_id is duplicated|must increase"):
            review_state.append_event(document, duplicate)

    def test_external_p1_requires_contract_evidence(self) -> None:
        candidate = review_event()
        candidate["threads"] = [thread(contract="external")]
        errors = review_state.validate_event(candidate)
        self.assertTrue(any("external-contract P1/P2" in error for error in errors))
        candidate["threads"][0]["evidence"] = evidence("captured_fixture")
        self.assertFalse(
            any(
                "external-contract P1/P2" in error
                for error in review_state.validate_event(candidate)
            )
        )

    def test_lgtm_rejects_material_validation_gap(self) -> None:
        document = review_state.new_document("abcdefgh", "review")
        final = event("final_review", 1)
        final.update(
            {
                "source_snapshot": snapshot("1"),
                "resolutions": [],
                "decision": "LGTM",
                "validation": validation(material=True),
            }
        )
        with self.assertRaisesRegex(
            ValueError, "LGTM forbids unresolved material gaps"
        ):
            review_state.append_event(document, final)

    def test_historical_material_gap_requires_explicit_resolution(self) -> None:
        document = review_state.new_document("abcdefgh", "review")
        initial = review_event()
        initial["validation"] = validation(material=True)
        review_state.append_event(document, initial)
        review_state.append_event(document, owner_event())
        final = event("final_review", 3)
        final.update(
            {
                "source_snapshot": snapshot("2"),
                "resolutions": [{"thread_id": "T1", "message": "Verified."}],
                "gap_resolutions": [],
                "decision": "LGTM",
                "validation": validation(),
            }
        )
        with self.assertRaisesRegex(ValueError, "unresolved material gaps"):
            review_state.append_event(document, deepcopy(final))
        final["gap_resolutions"] = [
            {
                "gap_id": "G1",
                "message": "The previously unavailable check now passes.",
                "evidence": evidence("test_result"),
            }
        ]
        review_state.append_event(document, final)
        self.assertEqual(document["state"]["validation_gaps"]["resolved"], ["G1"])

    def test_failed_final_check_blocks_lgtm(self) -> None:
        document = review_state.new_document("abcdefgh", "review")
        final = event("final_review", 1)
        final.update(
            {
                "source_snapshot": snapshot("1"),
                "resolutions": [],
                "gap_resolutions": [
                    {
                        "gap_id": "G1",
                        "message": "Attempted disposition.",
                        "evidence": evidence("test_result"),
                    }
                ],
                "decision": "LGTM",
                "validation": {
                    "performed": [{"check": "tests", "result": "failed"}],
                    "gaps": [
                        {
                            "gap_id": "G1",
                            "check": "tests",
                            "reason": "failure",
                            "material": True,
                        }
                    ],
                },
            }
        )
        with self.assertRaisesRegex(ValueError, "LGTM forbids failed checks"):
            review_state.append_event(document, final)

    def test_unknown_secret_field_is_rejected(self) -> None:
        candidate = review_event()
        candidate["cookie"] = "secret"
        self.assertTrue(
            any(
                "unknown fields: cookie" in error
                for error in review_state.validate_event(candidate)
            )
        )

    def test_contextual_templates_prefill_role_obligations(self) -> None:
        document = review_state.new_document("abcdefgh", "review")
        review_state.append_event(document, review_event())
        owner = review_state.contextual_event_template(
            document, "owner_reply", snapshot("2")
        )
        self.assertEqual([item["thread_id"] for item in owner["replies"]], ["T1"])
        self.assertEqual(owner["starting_source_snapshot"], snapshot("1"))
        owner["source_drift_assessment"] = "Guarded source changed."
        owner["replies"][0].update(
            {
                "decision": "declined",
                "message": "Existing behavior is correct.",
                "evidence": evidence("test_result"),
            }
        )
        owner["guide_synchronization"] = "No guide change."
        review_state.append_event(document, owner)
        final = review_state.contextual_event_template(
            document, "final_review", snapshot("2")
        )
        self.assertTrue(final["resolutions"][0]["verification"]["independent"])

    def test_thread_conversation_view_preserves_handoffs(self) -> None:
        document = review_state.new_document("abcdefgh", "review")
        review_state.append_event(document, review_event())
        review_state.append_event(document, owner_event())
        conversations = review_state.thread_conversations(document)
        self.assertEqual(len(conversations), 1)
        self.assertEqual(
            [entry["kind"] for entry in conversations[0]["conversation"]],
            ["owner_reply"],
        )

    def test_follow_up_document_links_prior_review(self) -> None:
        document = review_state.new_document(
            "abcdefgh", "follow-up", prior_review_id="bcdefghj"
        )
        self.assertEqual(document["prior_review_id"], "bcdefghj")
        self.assertEqual(review_state.validate_document(document), [])

    def test_evidence_cannot_postdate_event(self) -> None:
        candidate = review_event()
        candidate["threads"][0]["evidence"]["observed_at"] = at(2)
        self.assertTrue(
            any(
                "must not follow event.occurred_at" in error
                for error in review_state.validate_event(candidate)
            )
        )

    def test_declined_thread_requires_independent_reviewer_verification(self) -> None:
        document = review_state.new_document("abcdefgh", "review")
        review_state.append_event(document, review_event())
        review_state.append_event(document, owner_event("declined"))
        final = event("final_review", 3)
        final.update(
            {
                "source_snapshot": snapshot("2"),
                "resolutions": [
                    {"thread_id": "T1", "message": "Verified independently."}
                ],
                "decision": "LGTM",
                "validation": validation(),
            }
        )
        with self.assertRaisesRegex(ValueError, "independent verification"):
            review_state.append_event(document, deepcopy(final))
        final["resolutions"][0]["verification"] = {
            "independent": True,
            "evidence": evidence("live_probe"),
        }
        review_state.append_event(document, final)
        self.assertEqual(document["state"]["terminal"]["outcome"], "lgtm")

    def test_old_schema_is_preserved_but_rejected_with_new_loop_guidance(self) -> None:
        errors = review_state.validate_document(
            {
                "schema_version": 2,
                "review_id": "abcdefgh",
                "name": "old",
                "state": {},
                "history": [],
            }
        )
        self.assertTrue(any("start a new loop" in error for error in errors))

    def test_state_is_a_pure_history_projection(self) -> None:
        document = review_state.new_document("abcdefgh", "review")
        review_state.append_event(document, review_event())
        document["state"]["workflow"]["phase"] = "terminal"
        self.assertIn(
            "state projection is stale", review_state.validate_document(document)
        )


if __name__ == "__main__":
    unittest.main()
