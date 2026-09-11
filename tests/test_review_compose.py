"""Entry-time refusals and identifier assignment in the operation composer.

The composer's job is to reject an act as it is typed, naming the flag at fault,
rather than write a draft that `validate-event` will reject later. These tests
pin the refusals this skill's own sessions actually needed: evidence of the wrong
basis, evidence observed after the handoff, resolving the last open thread
outside a final_review, and acting on a thread the transaction cannot act on.

Removal is the other half of that job. An act the composer cannot take back
leaves an unpublishable draft with no move but to abort it, so these tests also
pin what `drop` removes beyond the operation it was handed.
"""

from __future__ import annotations

import argparse
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

import review_compose

NOW = datetime.now(timezone.utc)

EVIDENCE = {
    "basis": "source_inspection",
    "provenance": "example.txt",
    "sanitized_result": "Observed the declared behavior.",
}


def arguments(command: str, **overrides: Any) -> argparse.Namespace:
    """Build the namespace one composer subcommand would receive, fully defaulted."""
    values: dict[str, Any] = {
        "command": command,
        "message": "Recorded the act.",
        "message_file": None,
        "basis": None,
        "provenance": None,
        "sanitized_result": None,
        "sanitized_result_file": None,
        "artifact_digest": None,
        "observed_at": None,
        "priority": "P1",
        "contract": "internal",
        "title": "Behavior differs",
        "risk": "Users receive an incorrect result.",
        "required_behavior": "Return the documented result.",
        "paths": ["example.txt"],
        "anchor": [],
        "thread_id": "T1",
        "decision": "applied",
        "blocker": None,
        "completed_work": None,
        "remaining_work": None,
        "validation_gap": None,
        "verified": False,
        "check": "focused test",
        "reason": "The staging endpoint is unavailable.",
        "material": False,
        "result": "passed",
        "gap_reason": None,
        "gap_id": "G1",
        "disposition": "performed",
        "unperformed_check": None,
        "fail_closed_behavior": None,
        "tag": "decision",
        "structure_disposition": None,
        "structure_message": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def draft_for(
    kind: str,
    *,
    operations: list[Any] | None = None,
    open_threads: tuple = (),
    resolved_threads: tuple = (),
    open_gaps: tuple = (),
    history: list[Any] | None = None,
    value: dict[str, Any] | None = None,
) -> review_compose.Draft:
    carried = list(operations or [])
    return review_compose.Draft(
        path=Path("/nonexistent/draft.json"),
        value={"kind": kind, "operations": carried, **(value or {})},
        kind=kind,
        operations=carried,
        document={"history": history or [], "state": {}},
        open_threads=open_threads,
        resolved_threads=resolved_threads,
        open_gaps=open_gaps,
        recorded_gaps=open_gaps,
        stamped=NOW,
    )


def compose(draft: review_compose.Draft, args: argparse.Namespace):
    return review_compose.composed_operations(draft, args)


class EvidenceRefusalTest(unittest.TestCase):
    def test_external_contract_finding_refuses_a_source_inspection_basis(self) -> None:
        draft = draft_for("review")

        with self.assertRaisesRegex(ValueError, "external-contract P1/P2"):
            compose(
                draft,
                arguments("open-thread", contract="external", **EVIDENCE),
            )

        operations, thread_id = compose(
            draft,
            arguments(
                "open-thread",
                contract="external",
                **{**EVIDENCE, "basis": "authoritative_contract"},
            ),
        )
        self.assertEqual(thread_id, "T1")
        self.assertEqual(operations[0]["contract"], "external")

    def test_evidence_observed_after_the_handoff_is_refused(self) -> None:
        later = (NOW + timedelta(minutes=5)).isoformat()

        with self.assertRaisesRegex(ValueError, "postdates this transaction"):
            compose(
                draft_for("review"),
                arguments("open-thread", observed_at=later, **EVIDENCE),
            )

    def test_partial_evidence_flags_are_refused_as_a_set(self) -> None:
        with self.assertRaisesRegex(ValueError, "together"):
            compose(
                draft_for("review"),
                arguments("open-thread", basis="source_inspection"),
            )

    def test_an_act_that_demands_evidence_says_which_flags_it_wants(self) -> None:
        with self.assertRaisesRegex(ValueError, "--sanitized-result"):
            compose(draft_for("review"), arguments("open-thread"))

    def test_captured_fixture_evidence_requires_its_artifact_digest(self) -> None:
        with self.assertRaisesRegex(ValueError, "artifact_digest"):
            compose(
                draft_for("review"),
                arguments(
                    "open-thread",
                    **{**EVIDENCE, "basis": "captured_fixture"},
                ),
            )


class ThreadActRefusalTest(unittest.TestCase):
    def test_a_thread_must_name_the_files_it_concerns(self) -> None:
        with self.assertRaisesRegex(ValueError, "--paths"):
            compose(
                draft_for("review"), arguments("open-thread", paths=[], **EVIDENCE)
            )

    def test_a_reply_refuses_a_thread_that_is_not_open(self) -> None:
        draft = draft_for("owner_reply", open_threads=("T1",))

        with self.assertRaisesRegex(ValueError, "T2 is not an open thread"):
            compose(draft, arguments("reply", thread_id="T2", **EVIDENCE))

    def test_a_reviewer_update_refuses_to_resolve_its_last_open_thread(self) -> None:
        draft = draft_for("reviewer_update", open_threads=("T1",))

        with self.assertRaisesRegex(ValueError, "template final_review"):
            compose(draft, arguments("resolve", thread_id="T1"))

    def test_a_reviewer_update_may_resolve_while_another_thread_stays_open(
        self,
    ) -> None:
        draft = draft_for("reviewer_update", open_threads=("T1", "T2"))

        operations, _ = compose(draft, arguments("resolve", thread_id="T1"))
        self.assertEqual(operations[0]["op"], "thread.resolve")
        self.assertNotIn("verification", operations[0])

    def test_a_final_review_may_resolve_every_open_thread(self) -> None:
        draft = draft_for("final_review", open_threads=("T1",))

        operations, _ = compose(draft, arguments("resolve", thread_id="T1"))
        self.assertEqual(operations[0]["thread_id"], "T1")

    def test_resolving_a_declined_thread_requires_independent_verification(
        self,
    ) -> None:
        declined = [
            {
                "kind": "owner_reply",
                "operations": [
                    {"op": "thread.reply", "thread_id": "T1", "decision": "declined"}
                ],
            }
        ]
        draft = draft_for("final_review", open_threads=("T1",), history=declined)

        with self.assertRaisesRegex(ValueError, "independent verification"):
            compose(draft, arguments("resolve", thread_id="T1"))

        operations, _ = compose(
            draft, arguments("resolve", thread_id="T1", verified=True, **EVIDENCE)
        )
        self.assertTrue(operations[0]["verification"]["independent"])

    def test_evidence_without_verified_is_refused_rather_than_dropped(self) -> None:
        draft = draft_for("final_review", open_threads=("T1",))

        with self.assertRaisesRegex(ValueError, "--verified"):
            compose(draft, arguments("resolve", thread_id="T1", **EVIDENCE))

    def test_a_blocked_reply_must_state_every_blocked_work_field(self) -> None:
        draft = draft_for("owner_reply", open_threads=("T1",))

        with self.assertRaisesRegex(ValueError, "--blocker"):
            compose(
                draft,
                arguments("reply", decision="deferred/blocked", **EVIDENCE),
            )

        operations, _ = compose(
            draft,
            arguments(
                "reply",
                decision="deferred/blocked",
                blocker="The staging endpoint is unavailable.",
                completed_work="Applied the local part.",
                remaining_work="Verify against staging.",
                validation_gap="The live probe never ran.",
                **EVIDENCE,
            ),
        )
        self.assertEqual(operations[0]["decision"], "deferred/blocked")

    def test_blocked_work_fields_are_refused_on_an_applied_reply(self) -> None:
        draft = draft_for("owner_reply", open_threads=("T1",))

        with self.assertRaisesRegex(ValueError, "deferred/blocked"):
            compose(
                draft,
                arguments("reply", blocker="Not applicable.", **EVIDENCE),
            )

    def test_reopening_requires_a_resolved_thread(self) -> None:
        draft = draft_for("source_update", open_threads=("T1",))

        with self.assertRaisesRegex(ValueError, "not a resolved thread"):
            compose(draft, arguments("reopen", thread_id="T1"))


class TransactionKindRefusalTest(unittest.TestCase):
    def test_an_owner_reply_cannot_resolve_a_thread(self) -> None:
        draft = draft_for("owner_reply", open_threads=("T1",))

        with self.assertRaisesRegex(ValueError, "owner_reply does not allow"):
            compose(draft, arguments("resolve", thread_id="T1"))

    def test_a_note_must_target_a_thread_this_transaction_acts_on(self) -> None:
        draft = draft_for("review")

        with self.assertRaisesRegex(ValueError, "no entry for thread T1"):
            compose(draft, arguments("note"))

        opened, _ = compose(draft, arguments("open-thread", **EVIDENCE))
        draft.operations.extend(opened)
        operations, target = compose(draft, arguments("note"))
        self.assertEqual(target, "T1")
        self.assertEqual(operations[0]["target"], {"kind": "thread", "id": "T1"})


class ValidationRecordTest(unittest.TestCase):
    def test_a_failed_check_must_open_its_material_gap_in_the_same_act(self) -> None:
        draft = draft_for("review")

        with self.assertRaisesRegex(ValueError, "--gap-reason"):
            compose(draft, arguments("record-check", result="failed"))

        operations, target = compose(
            draft,
            arguments(
                "record-check",
                result="failed",
                gap_reason="The focused test fails against the guarded tree.",
            ),
        )
        self.assertEqual([item["op"] for item in operations], ["check.record", "gap.open"])
        self.assertEqual(target, "G1")
        self.assertTrue(operations[1]["material"])
        self.assertEqual(operations[1]["check"], operations[0]["check"])

    def test_a_gap_reason_without_a_failure_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "only to a failed check"):
            compose(
                draft_for("review"),
                arguments("record-check", gap_reason="Nothing failed."),
            )

    def test_an_unavailable_gap_resolution_must_carry_its_justification(self) -> None:
        draft = draft_for("final_review", open_gaps=("G1",))

        with self.assertRaisesRegex(ValueError, "--unperformed-check"):
            compose(
                draft,
                arguments(
                    "resolve-gap",
                    disposition="unavailable_non_material",
                    **EVIDENCE,
                ),
            )

        operations, _ = compose(
            draft,
            arguments(
                "resolve-gap",
                disposition="unavailable_non_material",
                unperformed_check="The live probe never ran.",
                fail_closed_behavior="The path denies access when the probe is absent.",
                **EVIDENCE,
            ),
        )
        self.assertEqual(
            operations[0]["justification"]["unperformed_check"],
            "The live probe never ran.",
        )

    def test_a_gap_resolution_refuses_a_gap_that_is_not_open(self) -> None:
        with self.assertRaisesRegex(ValueError, "not an open validation gap"):
            compose(
                draft_for("final_review"),
                arguments("resolve-gap", **EVIDENCE),
            )


class ApprovalTest(unittest.TestCase):
    def flagged_draft(self) -> review_compose.Draft:
        return draft_for(
            "final_review",
            operations=[
                {
                    "op": "review.approve",
                    "decision": "",
                    "structure_debt": {
                        "disposition": "",
                        "flagged_paths": ["src/app.py"],
                        "message": "",
                    },
                }
            ],
        )

    def test_an_approval_must_dispose_of_the_flagged_files(self) -> None:
        with self.assertRaisesRegex(ValueError, "--structure-disposition"):
            compose(self.flagged_draft(), arguments("approve", decision="LGTM"))

    def test_a_disposed_approval_keeps_the_flagged_set_it_was_given(self) -> None:
        operations, _ = compose(
            self.flagged_draft(),
            arguments(
                "approve",
                decision="LGTM",
                structure_disposition="structure_deferred",
                structure_message="Real accretion; chain a structure round.",
            ),
        )
        self.assertEqual(
            operations[0]["structure_debt"],
            {
                "disposition": "structure_deferred",
                "flagged_paths": ["src/app.py"],
                "message": "Real accretion; chain a structure round.",
            },
        )

    def test_structure_debt_is_refused_where_nothing_is_flagged(self) -> None:
        draft = draft_for(
            "final_review",
            operations=[{"op": "review.approve", "decision": ""}],
        )

        with self.assertRaisesRegex(ValueError, "no guarded file is accretion-flagged"):
            compose(
                draft,
                arguments(
                    "approve",
                    decision="LGTM",
                    structure_disposition="structure_reviewed",
                    structure_message="Nothing grew.",
                ),
            )


class IdentifierAndSlotTest(unittest.TestCase):
    def test_thread_ids_continue_past_canonical_and_drafted_threads(self) -> None:
        draft = draft_for(
            "source_update", open_threads=("T2",), resolved_threads=("T1",)
        )

        _, first = compose(draft, arguments("open-thread", **EVIDENCE))
        self.assertEqual(first, "T3")
        draft.operations.append({"op": "thread.open", "id": "T3"})
        _, second = compose(draft, arguments("open-thread", **EVIDENCE))
        self.assertEqual(second, "T4")

    def test_gap_ids_continue_past_every_gap_the_review_recorded(self) -> None:
        draft = draft_for("review", open_gaps=("G1", "G2"))

        _, gap_id = compose(draft, arguments("open-gap", material=True))
        self.assertEqual(gap_id, "G3")

    def test_reopening_a_gap_for_the_same_check_keeps_its_identifier(self) -> None:
        # A second identifier would leave a hole in the G<N> sequence projection
        # assigns, so the transaction would be rejected for a typing correction.
        draft = draft_for("review")

        first, _ = compose(draft, arguments("open-gap", material=True))
        draft.operations.extend(first)
        second, gap_id = compose(
            draft, arguments("open-gap", reason="Corrected reason.", material=True)
        )

        self.assertEqual(gap_id, "G1")
        review_compose.place(draft.operations, second[0])
        self.assertEqual(len(draft.operations), 1)
        self.assertEqual(draft.operations[0]["reason"], "Corrected reason.")

    def test_one_act_per_thread_replaces_rather_than_accumulates(self) -> None:
        operations = [
            {"op": "thread.comment", "thread_id": "T1", "message": "Looking."},
            {"op": "thread.comment", "thread_id": "T2", "message": "Looking."},
        ]

        replaced = review_compose.place(
            operations,
            {"op": "thread.resolve", "thread_id": "T1", "message": "Confirmed."},
        )

        self.assertTrue(replaced)
        self.assertEqual(
            [(item["op"], item["thread_id"]) for item in operations],
            [("thread.resolve", "T1"), ("thread.comment", "T2")],
        )

    def test_a_note_accumulates_because_a_thread_may_carry_several(self) -> None:
        operations: list[Any] = []
        for tag in ("decision", "follow-up"):
            review_compose.place(
                operations,
                {
                    "op": "note.attach",
                    "target": {"kind": "thread", "id": "T1"},
                    "tag": tag,
                    "message": "Flagged.",
                },
            )
        self.assertEqual(len(operations), 2)


def note_on(thread_id: str) -> dict[str, Any]:
    return {
        "op": "note.attach",
        "target": {"kind": "thread", "id": thread_id},
        "tag": "decision",
        "message": "Flagged for the release commit.",
    }


def opened_gap(gap_id: str, check: str) -> dict[str, Any]:
    return {
        "op": "gap.open",
        "gap_id": gap_id,
        "check": check,
        "reason": "The service is unavailable.",
        "material": True,
    }


class RemovalTest(unittest.TestCase):
    """Taking an act back, and what the draft can no longer carry once it is gone."""

    def drop(self, draft: review_compose.Draft, *selector: str) -> list[str]:
        return review_compose.remove(
            draft, review_compose.selection(draft, *selector)
        )

    def test_dropping_a_prefilled_resolution_frees_a_gap_that_stays_open(self) -> None:
        skeleton = {
            "op": "gap.resolve",
            "gap_id": "G1",
            "disposition": "",
            "message": "",
        }
        draft = draft_for("final_review", operations=[skeleton], open_gaps=("G1",))

        removed = self.drop(draft, "gap.resolve", "G1")

        self.assertEqual(removed, ["gap.resolve G1"])
        self.assertEqual(draft.operations, [])
        # The draft's value and its operation list are one object, so a removal
        # reaches what `secure_json` writes back.
        self.assertEqual(draft.value["operations"], [])

    def test_dropping_a_thread_act_takes_the_notes_it_would_strand(self) -> None:
        # A transaction may not carry a note for a thread it does not act on, so
        # the note cannot outlive the act it annotates.
        draft = draft_for(
            "review", operations=[{"op": "thread.open", "id": "T1"}, note_on("T1")]
        )

        removed = self.drop(draft, "thread.open", "T1")

        self.assertEqual(removed, ["thread.open T1", "note.attach T1"])
        self.assertEqual(draft.operations, [])

    def test_removal_closes_the_hole_it_leaves_in_the_minted_sequence(self) -> None:
        # Projection requires each opened thread to continue the canonical
        # sequence, so the survivor takes the identifier the removal freed and
        # its note follows it there.
        draft = draft_for(
            "review",
            operations=[
                {"op": "thread.open", "id": "T1"},
                {"op": "thread.open", "id": "T2"},
                note_on("T2"),
            ],
        )

        self.drop(draft, "thread.open", "T1")

        self.assertEqual(
            [review_compose.operation_summary(item) for item in draft.operations],
            ["thread.open T1", "note.attach T1"],
        )

    def test_a_corrected_check_drops_the_gap_its_failure_opened(self) -> None:
        draft = draft_for("review")
        failed, gap_id = compose(
            draft,
            arguments(
                "record-check",
                result="failed",
                gap_reason="The focused test fails against the guarded tree.",
            ),
        )
        for operation in failed:
            review_compose.place(draft.operations, operation)
        self.assertEqual(gap_id, "G1")

        corrected, _ = compose(draft, arguments("record-check", result="passed"))
        for operation in corrected:
            review_compose.place(draft.operations, operation)
        removed = review_compose.remove(
            draft, review_compose.unsupported(draft.operations)
        )

        self.assertEqual(removed, ["gap.open G1"])
        self.assertEqual(
            draft.operations,
            [{"op": "check.record", "check": "focused test", "result": "passed"}],
        )

    def test_a_dropped_gap_renumbers_the_gaps_recorded_after_it(self) -> None:
        draft = draft_for(
            "review",
            operations=[
                {"op": "check.record", "check": "unit tests", "result": "failed"},
                opened_gap("G1", "unit tests"),
                {"op": "check.record", "check": "live probe", "result": "failed"},
                opened_gap("G2", "live probe"),
            ],
        )
        review_compose.place(
            draft.operations,
            {"op": "check.record", "check": "unit tests", "result": "passed"},
        )

        removed = review_compose.remove(
            draft, review_compose.unsupported(draft.operations)
        )

        self.assertEqual(removed, ["gap.open G1"])
        self.assertEqual(
            [review_compose.operation_summary(item) for item in draft.operations],
            [
                "check.record unit tests",
                "check.record live probe",
                "gap.open G1",
            ],
        )

    def test_a_gap_is_refused_for_a_check_the_draft_records_as_passed(self) -> None:
        draft = draft_for(
            "review",
            operations=[
                {"op": "check.record", "check": "focused test", "result": "passed"}
            ],
        )

        with self.assertRaisesRegex(ValueError, "records focused test as passed"):
            compose(draft, arguments("open-gap", material=True))

    def test_dropping_names_what_the_draft_carries_when_nothing_matches(self) -> None:
        draft = draft_for("review", operations=[{"op": "thread.open", "id": "T1"}])

        with self.assertRaisesRegex(ValueError, "it carries thread.open T1"):
            review_compose.selection(draft, "thread.open", "T2")

    def test_an_approval_is_corrected_by_composing_it_rather_than_dropped(self) -> None:
        # Its flagged set came from the accretion ledger at templating, and
        # composing the approval again is what preserves it.
        draft = draft_for(
            "final_review",
            operations=[{"op": "review.approve", "decision": "LGTM"}],
        )

        with self.assertRaisesRegex(ValueError, "derived from the guard"):
            review_compose.selection(draft, "review.approve", "LGTM")


class AnchorTest(unittest.TestCase):
    def test_an_anchor_must_name_a_path_the_thread_declares(self) -> None:
        with self.assertRaisesRegex(ValueError, "not in --paths"):
            compose(
                draft_for("review"),
                arguments("open-thread", anchor=["other.txt:1-2"], **EVIDENCE),
            )

    def test_a_malformed_anchor_is_refused_with_its_expected_shape(self) -> None:
        with self.assertRaisesRegex(ValueError, "PATH:START-END"):
            compose(
                draft_for("review"),
                arguments("open-thread", anchor=["example.txt:1"], **EVIDENCE),
            )

    def test_a_valid_anchor_is_recorded_against_the_declared_path(self) -> None:
        operations, _ = compose(
            draft_for("review"),
            arguments("open-thread", anchor=["example.txt:10-20"], **EVIDENCE),
        )
        self.assertEqual(
            operations[0]["anchors"],
            [{"path": "example.txt", "start_line": 10, "end_line": 20}],
        )


if __name__ == "__main__":
    unittest.main()
