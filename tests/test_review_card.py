"""Tests for the phase-scoped operating card `inspect` hands the acting agent."""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import review_card


def import_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


publisher = import_module("review_publish_card", ROOT / "scripts" / "review_publish.py")
review_state = import_module("review_state_card", ROOT / "scripts" / "review_state.py")

STATE_SCRIPT = ROOT / "scripts" / "review_state.py"
# Recent enough that no phase's handoff deadline has passed, so a routine card is
# not disturbed by an eligible timeout.
BASE = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=30)


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


def evidence() -> dict[str, Any]:
    return {
        "basis": "source_inspection",
        "provenance": "example.txt",
        "observed_at": at(0),
        "sanitized_result": "Observed the declared behavior.",
    }


def validation(*, gap: bool = False) -> list[dict[str, Any]]:
    """Return the check and gap operations one transaction records."""
    operations: list[dict[str, Any]] = [
        {"op": "check.record", "check": "focused test", "result": "passed"}
    ]
    if gap:
        operations.append(
            {
                "op": "gap.open",
                "gap_id": "G1",
                "check": "live service",
                "reason": "The staging endpoint is unavailable.",
                "material": True,
            }
        )
    return operations


def event(kind: str, sequence: int) -> dict[str, Any]:
    value = review_state.event_template(kind)
    value["event_id"] = f"evt_card_{sequence:016d}"
    value["occurred_at"] = at(sequence)
    return value


def review_event(*, gap: bool = False) -> dict[str, Any]:
    value = event("review", 1)
    value.update(
        {
            "source_snapshot": snapshot("1"),
            "operations": [
                {
                    "op": "thread.open",
                    "id": "T1",
                    "priority": "P1",
                    "contract": "internal",
                    "title": "Behavior differs",
                    "risk": "Users receive an incorrect result.",
                    "evidence": evidence(),
                    "required_behavior": "Return the documented result.",
                    "paths": ["example.txt"],
                },
                *validation(gap=gap),
            ],
        }
    )
    return value


def owner_event() -> dict[str, Any]:
    value = event("owner_reply", 2)
    value.update(
        {
            "starting_source_snapshot": snapshot("1"),
            "completed_source_snapshot": snapshot("1"),
            "source_drift_assessment": "Only guarded source changed.",
            "guide_synchronization": "No guide change was needed.",
            "changed_files": ["example.txt"],
            "revisions": [],
            "operations": [
                {
                    "op": "thread.reply",
                    "thread_id": "T1",
                    "decision": "applied",
                    "message": "Handled the finding.",
                    "evidence": evidence(),
                },
                *validation(),
            ],
        }
    )
    return value


def final_event() -> dict[str, Any]:
    value = event("final_review", 3)
    value.update(
        {
            "source_snapshot": snapshot("1"),
            "operations": [
                {
                    "op": "thread.resolve",
                    "thread_id": "T1",
                    "message": "Verified the fix.",
                    "verification": {"independent": True, "evidence": evidence()},
                },
                *validation(),
                {"op": "review.approve", "decision": "LGTM"},
            ],
        }
    )
    return value


def document_for(
    *events: dict[str, Any],
    review_kind: str = "correctness",
    structure_policy: str = "off",
) -> dict[str, Any]:
    """Build canonical state at the phase the given events lead to.

    Structure policy defaults to `off` so the accretion ledger stays out of these
    cards; the ledger's own conditionality is covered end to end against a real
    working tree in `test_review_json_cli.py`.
    """
    document = review_state.new_document(
        "cardtest",
        "card",
        review_kind=review_kind,
        structure_policy=structure_policy,
    )
    for value in events:
        review_state.append_event(document, value)
    return document


def card_for(
    document: dict[str, Any],
    *,
    current_fingerprint: str | None = None,
    current_snapshot: dict[str, Any] | None = None,
    lease_present: bool = True,
    lock_json: str = "unlocked",
    journal: dict[str, Any] | None = None,
    draft: dict[str, Any] | None = None,
    stale_report: bool = False,
) -> dict[str, Any]:
    """Run `operation --agent` over one canonical document and return its card."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        review = root / "review.json"
        review.write_text(json.dumps(document, indent=2))
        report = root / "review.latest.md"
        report.write_text(
            "stale\n" if stale_report else review_state.render_report(document)
        )
        event_path = root / "review.event.json"
        if draft is not None:
            event_path.write_text(json.dumps(draft, indent=2))
        journal_path = root / "review.publish.json"
        if journal is not None:
            journal_path.write_text(json.dumps(journal, indent=2))
        recorded = document["state"]["source_fingerprint"] or "0" * 64
        arguments = argparse.Namespace(
            review=str(review),
            event=str(event_path),
            report=str(report),
            journal=str(journal_path),
            state_script=str(STATE_SCRIPT),
            lock_json=lock_json,
            repo=str(root),
            review_id="cardtest",
            current_source_fingerprint=current_fingerprint or recorded,
            current_source_json=(
                json.dumps(current_snapshot) if current_snapshot else ""
            ),
            lease_present=lease_present,
            json=False,
            agent=True,
            accretion=False,
            command_prefix="cli",
        )
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = publisher.operation(arguments)
    assert code == 0
    return json.loads(stream.getvalue())


PUBLISH_TAIL = [
    "template_event",
    "compose_operations",
    "record_validation_evidence",
    "review_draft",
    "inspect_before_publish",
    "publish",
    "read_publication_result",
    "await_handoff",
]


# --- review_card.operating_card ---


class OperatingCardTest(unittest.TestCase):
    def card(self, action: str, **overrides: Any) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "workflow": {
                "phase": "owner_response",
                "primary_actor": "owner",
                "allowed_events_by_actor": {"owner": ["owner_reply"]},
            },
            "action": action,
            "next_command": "cli template repo id owner_reply",
            "lock_first": False,
            "open_threads": ["T1"],
            "open_validation_gaps": [],
            "flags": [],
            "flagged_paths": [],
        }
        arguments.update(overrides)
        return review_card.operating_card(**arguments)

    def test_routine_card_is_the_complete_sequence_from_where_the_agent_stands(
        self,
    ) -> None:
        card = self.card("publish_owner_reply")

        self.assertEqual(card["sequence"], PUBLISH_TAIL)
        self.assertTrue(card["common_path_applies"])
        self.assertEqual(card["required_reading"], [])

    def test_an_unheld_lease_puts_the_lock_and_guard_ahead_of_the_sequence(
        self,
    ) -> None:
        card = self.card("publish_owner_reply", lock_first=True)

        self.assertEqual(
            card["sequence"],
            ["acquire_lock", "inspect_under_lease_with_scope", *PUBLISH_TAIL],
        )

    def test_a_named_reference_suspends_the_routine_sequence(self) -> None:
        card = self.card("publish_owner_reply", flags=["source_drift"])

        self.assertEqual(card["required_reading"], ["references/source-state.md"])
        self.assertFalse(card["common_path_applies"])

    def test_the_structure_debt_obligation_is_added_only_where_it_is_flagged(
        self,
    ) -> None:
        without = self.card("publish_reviewer_update")
        with_flag = self.card(
            "publish_reviewer_update",
            flags=["accretion_flagged"],
            flagged_paths=["src/app.py"],
        )

        self.assertNotIn("acknowledge_structure_debt", without["must"])
        self.assertIn("acknowledge_structure_debt", with_flag["must"])
        self.assertEqual(with_flag["accretion_flagged_paths"], ["src/app.py"])

    def test_every_action_the_ladder_can_choose_states_its_own_obligations(
        self,
    ) -> None:
        # Only the inert "none" action falls back, so no card borrows another
        # action's rules to fill a gap.
        self.assertEqual(
            set(review_card.STEPS_BY_ACTION) - set(review_card.OBLIGATIONS_BY_ACTION),
            {"none"},
        )
        self.assertEqual(
            set(review_card.ACTION_BY_OPERATION_STATUS.values())
            | set(review_card.ACTION_BY_PHASE.values()),
            {
                "recover_publication",
                "regenerate_report",
                "abort_draft",
                "publish_draft",
                "publish_initial_review",
                "publish_owner_reply",
                "publish_reviewer_update",
                "none",
            },
        )

    def test_every_identifier_the_card_emits_has_a_human_sentence(self) -> None:
        # The card is the control channel and the sentences are the human one; an
        # identifier without a sentence would make the human renderer raise.
        obligation_groups = [
            *review_card.OBLIGATIONS_BY_ACTION.values(),
            review_card.TERMINAL_OBLIGATIONS,
            review_card.IDLE_OBLIGATIONS,
        ]
        for obligations in obligation_groups:
            for item in obligations["must"]:
                self.assertIn(item, review_card.SENTENCE_BY_MUST)
            for item in obligations["must_not"]:
                self.assertIn(item, review_card.SENTENCE_BY_MUST_NOT)
        self.assertIn("acknowledge_structure_debt", review_card.SENTENCE_BY_MUST)
        for steps in review_card.STEPS_BY_ACTION.values():
            for step in steps:
                self.assertIn(step, review_card.SENTENCE_BY_STEP)
        for step in review_card.LOCK_STEPS:
            self.assertIn(step, review_card.SENTENCE_BY_STEP)
        for flag in review_card.REFERENCE_BY_FLAG:
            self.assertIn(flag, review_card.SENTENCE_BY_FLAG)


# --- review_publish.operation --agent, routine phases ---


class RoutineCardTest(unittest.TestCase):
    def test_initial_review_card_carries_the_reviewer_obligations(self) -> None:
        card = card_for(document_for(), current_snapshot=snapshot("1"))

        self.assertEqual(card["phase"], "awaiting_initial_review")
        self.assertEqual(card["primary_actor"], "reviewer")
        self.assertEqual(card["action"], "publish_initial_review")
        self.assertEqual(card["sequence"], PUBLISH_TAIL)
        self.assertEqual(
            card["must"],
            [
                "declare_complete_guarded_scope",
                "name_paths_on_every_thread",
                "record_evidence_for_external_contract_findings",
                "await_handoff_if_not_primary_actor",
            ],
        )
        self.assertEqual(
            card["must_not"],
            [
                "transcribe_another_agents_findings",
                "publish_lgtm_with_material_validation_gap",
                "hand_edit_draft_json",
            ],
        )

    def test_owner_response_card_forbids_the_reviewer_only_thread_actions(self) -> None:
        card = card_for(document_for(review_event()), current_snapshot=snapshot("1"))

        self.assertEqual(card["phase"], "owner_response")
        self.assertEqual(card["action"], "publish_owner_reply")
        self.assertEqual(card["open_threads"], ["T1"])
        self.assertEqual(card["sequence"], PUBLISH_TAIL)
        self.assertEqual(
            card["must"],
            [
                "reply_to_every_open_thread",
                "state_decision_on_every_reply",
                "record_evidence_for_declined_work",
                "flag_design_shift_with_a_note",
                "await_handoff_if_not_primary_actor",
            ],
        )
        self.assertEqual(
            card["must_not"],
            [
                "resolve_thread",
                "reopen_thread",
                "open_new_thread",
                "hand_edit_canonical_json",
                "hand_edit_draft_json",
            ],
        )
        self.assertTrue(card["common_path_applies"])

    def test_reviewer_verification_card_carries_the_resolution_obligations(
        self,
    ) -> None:
        card = card_for(
            document_for(review_event(), owner_event()), current_snapshot=snapshot("1")
        )

        self.assertEqual(card["phase"], "reviewer_verification")
        self.assertEqual(card["action"], "publish_reviewer_update")
        self.assertEqual(card["sequence"], PUBLISH_TAIL)
        self.assertEqual(
            card["must"],
            [
                "decide_every_open_thread",
                "verify_declined_thread_independently_before_resolving",
                "resolve_every_open_thread_in_final_review",
                "flag_design_shift_with_a_note",
                "await_handoff_if_not_primary_actor",
            ],
        )
        self.assertIn("publish_lgtm_with_open_thread", card["must_not"])
        self.assertTrue(card["common_path_applies"])

    def test_terminal_card_recommends_nothing_and_still_states_its_obligations(
        self,
    ) -> None:
        card = card_for(
            document_for(review_event(), owner_event(), final_event()),
            current_snapshot=snapshot("1"),
        )

        self.assertEqual(card["phase"], "terminal")
        self.assertEqual(card["action"], "none")
        self.assertEqual(card["sequence"], [])
        self.assertEqual(card["next_command"], "none")
        self.assertEqual(
            card["must"],
            ["confirm_approval_stale_is_false_before_reporting_complete"],
        )
        self.assertIn("publish_after_terminal", card["must_not"])

    def test_an_unlocked_loop_keeps_its_goal_while_the_lock_comes_first(self) -> None:
        card = card_for(
            document_for(review_event()),
            current_snapshot=snapshot("1"),
            lease_present=False,
        )

        self.assertEqual(card["action"], "publish_owner_reply")
        self.assertEqual(card["sequence"][0], "acquire_lock")
        self.assertIn("lock acquire", card["next_command"])

    def test_another_agents_lock_routes_to_waiting_rather_than_the_phase_action(
        self,
    ) -> None:
        card = card_for(
            document_for(review_event()),
            current_snapshot=snapshot("1"),
            lease_present=False,
            lock_json=json.dumps({"holder": "other"}),
        )

        self.assertEqual(card["action"], "wait_for_lock")
        self.assertEqual(card["sequence"], ["wait", "inspect"])
        self.assertEqual(card["must"], ["re_arm_the_wait_until_the_lock_clears"])
        self.assertIn("break_another_agents_lock", card["must_not"])


# --- review_publish.operation --agent, exceptional paths ---


class ExceptionalCardTest(unittest.TestCase):
    def assert_routes_to(self, card: dict[str, Any], reference: str, flag: str) -> None:
        self.assertIn(flag, card["flags"])
        self.assertIn(f"references/{reference}", card["required_reading"])
        self.assertFalse(card["common_path_applies"])

    def test_an_undeclared_scope_requires_the_source_reference(self) -> None:
        card = card_for(document_for(), current_snapshot=snapshot("1"))

        self.assert_routes_to(card, "source-state.md", "scope_undeclared")

    def test_a_changed_scope_requires_the_source_reference(self) -> None:
        widened = snapshot("1")
        widened["scope"] = ["example.txt", "other.txt"]

        card = card_for(document_for(review_event()), current_snapshot=widened)

        self.assert_routes_to(card, "source-state.md", "scope_changed")

    def test_source_drift_requires_the_source_reference(self) -> None:
        card = card_for(
            document_for(review_event()),
            current_fingerprint="9" * 64,
            current_snapshot=snapshot("1"),
        )

        self.assert_routes_to(card, "source-state.md", "source_drift")

    def test_an_interrupted_publication_requires_the_source_reference(self) -> None:
        document = document_for(review_event())
        receipt = {
            "format": review_state.FORMAT,
            "format_revision": review_state.FORMAT_REVISION,
            "event_id": "evt_card_000000000000009",
            "draft_digest": "a" * 64,
            "base_canonical_sha256": "b" * 64,
            "intended_canonical_sha256": "c" * 64,
            "source_fingerprint": "1" * 64,
            "commit_phase": "prepared",
        }

        card = card_for(document, current_snapshot=snapshot("1"), journal=receipt)

        self.assertEqual(card["action"], "recover_publication")
        self.assertEqual(card["sequence"], ["recover_publish", "inspect"])
        self.assert_routes_to(card, "source-state.md", "publication_recovery")

    def test_an_unreadable_artifact_requires_the_source_reference(self) -> None:
        card = card_for(
            document_for(review_event()),
            current_snapshot=snapshot("1"),
            journal={"unexpected": "shape"},
        )

        self.assertEqual(card["action"], "abort_draft")
        self.assertEqual(
            card["must"], ["template_again_rather_than_repairing_the_rejected_draft"]
        )
        self.assert_routes_to(card, "source-state.md", "corrupt_artifact")

    def test_an_eligible_timeout_requires_the_source_reference(self) -> None:
        # An owner_response handoff older than its two-hour deadline.
        elapsed = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        stale = review_event()
        stale["occurred_at"] = elapsed
        stale["operations"][0]["evidence"]["observed_at"] = elapsed

        card = card_for(document_for(stale), current_snapshot=snapshot("1"))

        self.assert_routes_to(card, "source-state.md", "timeout_eligible")

    def test_an_open_validation_gap_requires_the_schema_reference(self) -> None:
        card = card_for(
            document_for(review_event(gap=True)), current_snapshot=snapshot("1")
        )

        self.assertEqual(card["open_validation_gaps"], ["G1"])
        self.assert_routes_to(card, "review-schema.md", "open_validation_gaps")

    def test_a_structure_round_requires_the_structure_reference(self) -> None:
        card = card_for(
            document_for(review_event(), review_kind="structure"),
            current_snapshot=snapshot("1"),
        )

        self.assert_routes_to(card, "structure-review.md", "structure_round")

    def test_a_stale_report_is_repaired_before_the_phase_action(self) -> None:
        card = card_for(
            document_for(review_event()),
            current_snapshot=snapshot("1"),
            stale_report=True,
        )

        self.assertEqual(card["action"], "regenerate_report")
        self.assertEqual(card["sequence"], ["regenerate_report", "inspect"])


# --- review_card.render_card ---


class RenderCardTest(unittest.TestCase):
    def test_the_human_form_explains_the_identifiers_the_agent_form_carries(
        self,
    ) -> None:
        card = review_card.operating_card(
            workflow={
                "phase": "owner_response",
                "primary_actor": "owner",
                "allowed_events_by_actor": {"owner": ["owner_reply"]},
            },
            action="publish_owner_reply",
            next_command="cli template repo id owner_reply",
            lock_first=False,
            open_threads=["T1"],
            open_validation_gaps=[],
            flags=["source_drift"],
            flagged_paths=[],
        )

        rendered = "\n".join(review_card.render_card(card))

        self.assertIn("action: publish_owner_reply", rendered)
        self.assertIn("reply to every open thread", rendered)
        self.assertIn("compose each act with the draft subcommands", rendered)
        self.assertIn("never: resolve a thread", rendered)
        self.assertIn("hand-edit the draft JSON instead of composing it", rendered)
        self.assertIn("the guarded source moved since it was recorded", rendered)
        self.assertIn("references/source-state.md", rendered)


if __name__ == "__main__":
    unittest.main()
