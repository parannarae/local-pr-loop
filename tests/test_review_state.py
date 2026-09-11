"""Tests for calendar-revision workflow projection and evidence gates."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

MODULE = Path(__file__).parents[1] / "scripts" / "review_state.py"
sys.path.insert(0, str(MODULE.parent))
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


def validation(*, material: bool = False) -> list[dict[str, Any]]:
    """Return the check and gap operations one transaction records."""
    operations: list[dict[str, Any]] = [
        {"op": "check.record", "check": "focused test", "result": "passed"}
    ]
    if material:
        operations.append(
            {
                "op": "gap.open",
                "gap_id": "G1",
                "check": "live service",
                "reason": "unavailable",
                "material": True,
            }
        )
    return operations


def thread(
    thread_id: str = "T1",
    *,
    contract: str = "internal",
    priority: str = "P1",
    basis: str = "source_inspection",
) -> dict[str, Any]:
    return {
        "op": "thread.open",
        "id": thread_id,
        "priority": priority,
        "contract": contract,
        "title": "Behavior differs",
        "risk": "Users receive an incorrect result.",
        "evidence": evidence(basis),
        "required_behavior": "Return the documented result.",
        "paths": [],
    }


def reply(
    thread_id: str = "T1", decision: str = "applied", **extra: Any
) -> dict[str, Any]:
    return {
        "op": "thread.reply",
        "thread_id": thread_id,
        "decision": decision,
        "message": "Handled the finding.",
        "evidence": evidence("test_result"),
        **extra,
    }


def approval(**extra: Any) -> dict[str, Any]:
    return {"op": "review.approve", "decision": "LGTM", **extra}


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
            "operations": [thread(), *validation()],
        }
    )
    return value


def owner_event(decision: str = "applied") -> dict[str, Any]:
    value = event("owner_reply", 2)
    value.update(
        {
            "starting_source_snapshot": snapshot("1"),
            "completed_source_snapshot": snapshot("2"),
            "source_drift_assessment": "Only guarded source changed.",
            "guide_synchronization": "No guide change was needed.",
            "changed_files": ["example.txt"],
            "revisions": [],
            "operations": [reply(decision=decision), *validation()],
        }
    )
    return value


class ReviewStateTest(unittest.TestCase):
    def test_new_document_uses_format_and_initial_workflow(self) -> None:
        document = review_state.new_document("abcdefgh", "review")
        self.assertEqual(document["format"], "local-pr-loop")
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
                "operations": [
                    {
                        "op": "source.replace",
                        "snapshot": snapshot("2"),
                        "reason": "Changed source.",
                    },
                    {
                        "op": "thread.comment",
                        "thread_id": "T1",
                        "message": "Still reproduces on the new basis.",
                    },
                ],
            }
        )
        with self.assertRaisesRegex(ValueError, "event_id is duplicated|must increase"):
            review_state.append_event(document, duplicate)

    def test_external_p1_requires_contract_evidence(self) -> None:
        candidate = review_event()
        candidate["operations"] = [thread(contract="external"), *validation()]
        errors = review_state.validate_event(candidate)
        self.assertTrue(any("external-contract P1/P2" in error for error in errors))
        candidate["operations"][0]["evidence"] = evidence("captured_fixture")
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
                "operations": [*validation(material=True), approval()],
            }
        )
        with self.assertRaisesRegex(
            ValueError, "LGTM forbids unresolved material gaps"
        ):
            review_state.append_event(document, final)

    def test_historical_material_gap_requires_explicit_resolution(self) -> None:
        document = review_state.new_document("abcdefgh", "review")
        initial = review_event()
        initial["operations"] = [thread(), *validation(material=True)]
        review_state.append_event(document, initial)
        review_state.append_event(document, owner_event())
        final = event("final_review", 3)
        final.update(
            {
                "source_snapshot": snapshot("2"),
                "operations": [
                    {
                        "op": "thread.resolve",
                        "thread_id": "T1",
                        "message": "Verified.",
                    },
                    *validation(),
                    approval(),
                ],
            }
        )
        with self.assertRaisesRegex(ValueError, "unresolved material gaps"):
            review_state.append_event(document, deepcopy(final))
        final["operations"].insert(
            1,
            {
                "op": "gap.resolve",
                "gap_id": "G1",
                "disposition": "performed",
                "message": "The previously unavailable check now passes.",
                "evidence": evidence("test_result"),
            },
        )
        review_state.append_event(document, final)
        self.assertEqual(document["state"]["validation_gaps"]["resolved"], ["G1"])

    def test_failed_final_check_blocks_lgtm(self) -> None:
        document = review_state.new_document("abcdefgh", "review")
        final = event("final_review", 1)
        final.update(
            {
                "source_snapshot": snapshot("1"),
                "operations": [
                    {"op": "check.record", "check": "tests", "result": "failed"},
                    {
                        "op": "gap.open",
                        "gap_id": "G1",
                        "check": "tests",
                        "reason": "failure",
                        "material": True,
                    },
                    {
                        "op": "gap.resolve",
                        "gap_id": "G1",
                        "disposition": "performed",
                        "message": "Attempted disposition.",
                        "evidence": evidence("test_result"),
                    },
                    approval(),
                ],
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

    def test_unknown_field_inside_an_operation_is_rejected(self) -> None:
        candidate = review_event()
        candidate["operations"][0]["authorization"] = "Bearer token"
        self.assertTrue(
            any(
                "unknown fields: authorization" in error
                for error in review_state.validate_event(candidate)
            )
        )

    def test_an_unknown_operation_name_is_rejected(self) -> None:
        candidate = review_event()
        candidate["operations"].append({"op": "thread.escalate", "thread_id": "T1"})
        self.assertTrue(
            any(
                "is not a known operation" in error
                for error in review_state.validate_event(candidate)
            )
        )

    def test_an_operation_outside_its_kind_is_rejected(self) -> None:
        candidate = review_event()
        candidate["operations"].append(
            {"op": "thread.resolve", "thread_id": "T1", "message": "Closing early."}
        )
        self.assertTrue(
            any(
                "review does not allow thread.resolve" in error
                for error in review_state.validate_event(candidate)
            )
        )

    def test_contextual_templates_prefill_role_obligations(self) -> None:
        document = review_state.new_document("abcdefgh", "review")
        review_state.append_event(document, review_event())
        owner = review_state.contextual_event_template(
            document, "owner_reply", snapshot("2")
        )
        self.assertEqual(
            [item["thread_id"] for item in owner["operations"]], ["T1"]
        )
        self.assertEqual(owner["starting_source_snapshot"], snapshot("1"))
        owner["source_drift_assessment"] = "Guarded source changed."
        owner["operations"][0].update(
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
        self.assertTrue(final["operations"][0]["verification"]["independent"])

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
        candidate["operations"][0]["evidence"]["observed_at"] = at(2)
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
                "operations": [
                    {
                        "op": "thread.resolve",
                        "thread_id": "T1",
                        "message": "Verified independently.",
                    },
                    *validation(),
                    approval(),
                ],
            }
        )
        with self.assertRaisesRegex(ValueError, "independent verification"):
            review_state.append_event(document, deepcopy(final))
        final["operations"][0]["verification"] = {
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

    def test_complete_workflow_projection_and_report_are_history_derived(self) -> None:
        document = review_state.new_document("abcdefgh", "review")
        review_state.append_event(document, review_event())
        self.assertEqual(document["state"]["workflow"]["phase"], "owner_response")

        review_state.append_event(document, owner_event())
        self.assertEqual(
            document["state"]["workflow"]["phase"], "reviewer_verification"
        )

        update = event("reviewer_update", 3)
        update.update(
            {
                "source_snapshot": snapshot("2"),
                "operations": [
                    {
                        "op": "thread.comment",
                        "thread_id": "T1",
                        "message": "One more verification is required.",
                    },
                    *validation(),
                ],
            }
        )
        review_state.append_event(document, update)
        self.assertEqual(document["state"]["workflow"]["phase"], "owner_response")
        self.assertEqual(document["state"]["threads"]["open"], ["T1"])

        second_owner = event("owner_reply", 4)
        second_owner.update(
            {
                "starting_source_snapshot": snapshot("2"),
                "completed_source_snapshot": snapshot("3"),
                "source_drift_assessment": "The guarded source basis is unchanged.",
                "guide_synchronization": "No guide change was needed.",
                "changed_files": ["example.txt"],
                "revisions": [],
                "operations": [
                    reply(),
                    *validation(),
                ],
            }
        )
        review_state.append_event(document, second_owner)

        final = event("final_review", 5)
        final.update(
            {
                "source_snapshot": snapshot("3"),
                "operations": [
                    {
                        "op": "thread.resolve",
                        "thread_id": "T1",
                        "message": "Verified.",
                    },
                    *validation(),
                    approval(),
                ],
            }
        )
        review_state.append_event(document, final)
        self.assertEqual(document["state"]["workflow"]["phase"], "terminal")
        self.assertEqual(document["state"]["threads"]["resolved"], ["T1"])
        self.assertEqual(document["state"]["terminal"]["outcome"], "lgtm")

        report = review_state.render_report(document)
        self.assertIn("# Review Summary — review (`abcdefgh`)", report)
        self.assertIn("- **Outcome: LGTM**", report)
        self.assertIn("## Notes for You", report)
        self.assertIn("## Issue Summary", report)
        self.assertIn("## Verification", report)

    def test_contextual_templates_cover_timeout_and_gap_obligations(self) -> None:
        document = review_state.new_document("abcdefgh", "review")
        initial = review_event()
        initial["operations"] = [thread(), *validation(material=True)]
        review_state.append_event(document, initial)

        timeout = review_state.contextual_event_template(
            document, "owner_timeout", snapshot("1")
        )
        declaration = timeout["operations"][0]
        self.assertEqual(declaration["op"], "timeout.declare")
        self.assertEqual(declaration["started_at"], initial["occurred_at"])
        self.assertTrue(declaration["deadline"])
        self.assertIn("deadline elapsed", declaration["reason"])

        review_state.append_event(document, owner_event())
        reviewer = review_state.contextual_event_template(
            document, "reviewer_update", snapshot("2")
        )
        self.assertEqual(
            [
                item["thread_id"]
                for item in reviewer["operations"]
                if item["op"] == "thread.comment"
            ],
            ["T1"],
        )
        self.assertEqual(
            [
                item["gap_id"]
                for item in reviewer["operations"]
                if item["op"] == "gap.resolve"
            ],
            ["G1"],
        )


class RevisionBoundaryTest(unittest.TestCase):
    """The reader must refuse a storage contract it does not implement in full."""

    def compound_document(self) -> dict[str, Any]:
        """A pre-operation artifact: the revision and the field set it carried."""
        return {
            "format": "local-pr-loop",
            "format_revision": "2026-08-21.1",
            "created_by": {"version": "0.9.1"},
            "created_at": at(0),
            "review_id": "abcdefgh",
            "prior_review_id": None,
            "name": "review",
            "review_kind": "correctness",
            "structure_policy": "auto",
            "comparison_base": None,
            "state": review_state.default_state(),
            "history": [
                {
                    "event_id": "evt_test_0001",
                    "kind": "review",
                    "occurred_at": at(1),
                    "source_snapshot": snapshot("1"),
                    "threads": [
                        {
                            "id": "T1",
                            "priority": "P1",
                            "contract": "internal",
                            "title": "Behavior differs",
                            "risk": "Users receive an incorrect result.",
                            "evidence": evidence(),
                            "required_behavior": "Return the documented result.",
                        }
                    ],
                    "validation": {"performed": [], "gaps": []},
                }
            ],
        }

    def test_a_compound_document_is_refused_before_any_field_is_read(self) -> None:
        errors = review_state.validate_document(self.compound_document())

        # One error and only one: interpreting the rest would read the absent
        # operation list as an empty default and project a loop with no threads.
        self.assertEqual(len(errors), 1)
        self.assertIn("2026-08-21.1", errors[0])
        self.assertIn("start a new loop", errors[0])

    def test_an_operation_document_at_a_foreign_revision_is_refused(self) -> None:
        document = review_state.new_document("abcdefgh", "review")
        review_state.append_event(document, review_event())
        document["format_revision"] = "2027-01-01.1"

        errors = review_state.validate_document(document)

        self.assertEqual(len(errors), 1)
        self.assertIn("2027-01-01.1", errors[0])

    def test_a_foreign_format_name_is_refused(self) -> None:
        document = review_state.new_document("abcdefgh", "review")
        document["format"] = "other-loop"

        self.assertTrue(
            any("other-loop" in error for error in review_state.validate_document(document))
        )

    def test_the_current_revision_is_the_only_readable_contract(self) -> None:
        document = review_state.new_document("abcdefgh", "review")

        self.assertIsNone(review_state.unsupported_revision_error(document))
        self.assertEqual(document["format_revision"], "2026-09-11.1")


class OperationVocabularyTest(unittest.TestCase):
    def test_every_documented_operation_is_defined_exactly_once(self) -> None:
        self.assertEqual(
            set(review_state.OPERATION_FIELDS),
            {
                "thread.open",
                "thread.reply",
                "thread.comment",
                "thread.resolve",
                "thread.reopen",
                "gap.open",
                "gap.resolve",
                "check.record",
                "note.attach",
                "source.replace",
                "review.approve",
                "timeout.declare",
            },
        )

    def test_a_timeout_carries_its_declaration_and_nothing_else(self) -> None:
        document = review_state.new_document("abcdefgh", "review")
        initial = review_event()
        review_state.append_event(document, initial)
        timeout = review_state.contextual_event_template(
            document, "owner_timeout", snapshot("1")
        )
        timeout["event_id"] = "evt_test_0009"
        timeout["occurred_at"] = at(7300)
        timeout["operations"].append(
            {"op": "check.record", "check": "extra", "result": "passed"}
        )

        self.assertTrue(
            any(
                "exactly one timeout.declare" in error
                for error in review_state.validate_event(timeout)
            )
        )

    def test_a_source_update_must_replace_the_snapshot_it_records(self) -> None:
        document = review_state.new_document("abcdefgh", "review")
        review_state.append_event(document, review_event())
        update = event("source_update", 2)
        update.update(
            {
                "source_snapshot": snapshot("2"),
                "operations": [
                    {
                        "op": "source.replace",
                        "snapshot": snapshot("3"),
                        "reason": "Rebased onto the release branch.",
                    },
                    {
                        "op": "thread.comment",
                        "thread_id": "T1",
                        "message": "The finding survives the rebase.",
                    },
                ],
            }
        )

        errors = review_state.validate_event(update)
        self.assertTrue(
            any("must equal the transaction's source_snapshot" in e for e in errors)
        )
        update["operations"][0]["snapshot"] = snapshot("2")
        self.assertEqual(review_state.validate_event(update), [])

    def test_a_note_must_target_a_thread_this_transaction_acts_on(self) -> None:
        document = review_state.new_document("abcdefgh", "review")
        candidate = review_event()
        candidate["operations"].append(
            {
                "op": "note.attach",
                "target": {"kind": "thread", "id": "T2"},
                "tag": "decision",
                "message": "Bump deferred to the release commit.",
            }
        )

        with self.assertRaisesRegex(ValueError, "does not act on"):
            review_state.append_event(document, candidate)

    def test_a_note_target_kind_other_than_thread_is_refused(self) -> None:
        candidate = review_event()
        candidate["operations"].append(
            {
                "op": "note.attach",
                "target": {"kind": "review", "id": "T1"},
                "tag": "decision",
                "message": "A review-level note needs a new format revision.",
            }
        )

        self.assertTrue(
            any(
                "target.kind must be thread" in error
                for error in review_state.validate_event(candidate)
            )
        )

    def test_a_note_must_declare_a_tag(self) -> None:
        candidate = review_event()
        candidate["operations"].append(
            {
                "op": "note.attach",
                "target": {"kind": "thread", "id": "T1"},
                "message": "Untagged.",
            }
        )

        self.assertTrue(
            any(
                "missing required fields: tag" in error
                for error in review_state.validate_event(candidate)
            )
        )

    def test_anchors_must_name_a_declared_path_and_a_forward_range(self) -> None:
        candidate = review_event()
        opened = candidate["operations"][0]
        opened["paths"] = ["example.txt"]
        opened["anchors"] = [
            {"path": "other.txt", "start_line": 10, "end_line": 4}
        ]

        errors = " ; ".join(review_state.validate_event(candidate))
        self.assertIn("must also appear in paths", errors)
        self.assertIn("must not precede start_line", errors)

        opened["anchors"] = [{"path": "example.txt", "start_line": 4, "end_line": 10}]
        self.assertEqual(review_state.validate_event(candidate), [])

    def test_one_thread_cannot_be_acted_on_twice_in_one_transaction(self) -> None:
        document = review_state.new_document("abcdefgh", "review")
        review_state.append_event(document, review_event())
        owner = owner_event()
        owner["operations"].insert(1, reply(decision="declined"))

        with self.assertRaisesRegex(ValueError, "twice in one transaction"):
            review_state.append_event(document, owner)

    def test_revisions_must_be_full_git_object_ids(self) -> None:
        candidate = owner_event()
        candidate["revisions"] = ["HEAD~1"]

        self.assertTrue(
            any(
                "revisions[0] must be a full Git object ID" in error
                for error in review_state.validate_event(candidate)
            )
        )
        candidate["revisions"] = ["b" * 40]
        self.assertEqual(review_state.validate_event(candidate), [])

    def test_owner_reply_metadata_is_required_on_the_envelope(self) -> None:
        candidate = owner_event()
        del candidate["guide_synchronization"]

        self.assertTrue(
            any(
                "guide_synchronization must be a non-empty string" in error
                for error in review_state.validate_event(candidate)
            )
        )


def structure_debt(paths: list[str]) -> dict[str, Any]:
    return {
        "disposition": "structure_deferred",
        "flagged_paths": paths,
        "message": "Real accretion; a structure round should follow.",
    }


class StructureSchemaTest(unittest.TestCase):
    # --- document envelope ---

    def test_new_document_records_kind_policy_and_base_defaults(self) -> None:
        document = review_state.new_document("abcdefgh", "review")
        self.assertEqual(document["review_kind"], "correctness")
        self.assertEqual(document["structure_policy"], "auto")
        self.assertIsNone(document["comparison_base"])
        self.assertEqual(review_state.validate_document(document), [])

    def test_structure_kind_with_comparison_base_validates(self) -> None:
        document = review_state.new_document(
            "abcdefgh", "review", None, "structure", "defer", "a" * 40
        )
        self.assertEqual(review_state.validate_document(document), [])

    def test_invalid_kind_policy_and_base_are_rejected(self) -> None:
        document = review_state.new_document("abcdefgh", "review")
        document["review_kind"] = "cosmetic"
        document["structure_policy"] = "sometimes"
        document["comparison_base"] = "main"
        errors = "; ".join(review_state.validate_document(document))
        self.assertIn("review_kind", errors)
        self.assertIn("structure_policy", errors)
        self.assertIn("comparison_base", errors)

    def test_structure_round_history_rejects_structure_debt(self) -> None:
        document = review_state.new_document(
            "abcdefgh", "review", None, "structure"
        )
        final = event("final_review", 1)
        final.update(
            {
                "source_snapshot": snapshot("1"),
                "operations": [
                    *validation(),
                    approval(structure_debt=structure_debt(["a.py"])),
                ],
            }
        )
        review_state.append_event(document, final)
        errors = "; ".join(review_state.validate_document(document))
        self.assertIn("structure round records no structure_debt", errors)

    # --- thread paths ---

    def test_thread_paths_must_be_unique_strings(self) -> None:
        candidate = review_event()
        candidate["operations"] = [{**thread(), "paths": "a.py"}, *validation()]
        errors = review_state.validate_event(candidate)
        self.assertTrue(any(".paths" in error for error in errors))
        candidate["operations"][0] = {**thread(), "paths": ["a.py", "a.py"]}
        errors = review_state.validate_event(candidate)
        self.assertTrue(any(".paths" in error for error in errors))
        candidate["operations"][0] = {**thread(), "paths": ["a.py", "b.py"]}
        self.assertEqual(review_state.validate_event(candidate), [])

    # --- structure_debt shape ---

    def final_with_debt(self, debt: dict[str, Any]) -> dict[str, Any]:
        value = event("final_review", 1)
        value.update(
            {
                "source_snapshot": snapshot("1"),
                "operations": [*validation(), approval(structure_debt=debt)],
            }
        )
        return value

    def test_valid_structure_debt_passes_event_validation(self) -> None:
        candidate = self.final_with_debt(structure_debt(["a.py"]))
        self.assertEqual(review_state.validate_event(candidate), [])

    def test_structure_debt_rejects_bad_disposition_and_empty_paths(self) -> None:
        candidate = self.final_with_debt(
            {"disposition": "ignored", "flagged_paths": [], "message": ""}
        )
        errors = "; ".join(review_state.validate_event(candidate))
        self.assertIn("structure_debt.disposition", errors)
        self.assertIn("structure_debt.flagged_paths must not be empty", errors)
        self.assertIn("structure_debt.message", errors)

    def test_structure_debt_rejects_unknown_fields(self) -> None:
        candidate = self.final_with_debt({**structure_debt(["a.py"]), "extra": 1})
        errors = "; ".join(review_state.validate_event(candidate))
        self.assertIn("unknown fields", errors)

    # --- template prefill ---

    def test_review_template_prefills_no_thread_for_the_composer_to_fill(self) -> None:
        # A review's findings are not a known set, so there is no obligation
        # skeleton to prefill; each thread arrives through draft open-thread.
        self.assertEqual(review_state.event_template("review")["operations"], [])

    def test_final_review_template_prefills_structure_debt_when_flagged(self) -> None:
        document = review_state.new_document("abcdefgh", "review")
        template = review_state.contextual_event_template(
            document, "final_review", snapshot("1"), ["b.py", "a.py"]
        )
        approve = template["operations"][-1]
        self.assertEqual(approve["op"], "review.approve")
        self.assertEqual(
            approve["structure_debt"],
            {"disposition": "", "flagged_paths": ["a.py", "b.py"], "message": ""},
        )

    def test_final_review_template_omits_structure_debt_when_clean(self) -> None:
        document = review_state.new_document("abcdefgh", "review")
        template = review_state.contextual_event_template(
            document, "final_review", snapshot("1"), []
        )
        self.assertNotIn("structure_debt", template["operations"][-1])


if __name__ == "__main__":
    unittest.main()
