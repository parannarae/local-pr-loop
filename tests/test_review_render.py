"""Unit tests for the skim-first summary renderer."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import review_render


def waiting_workflow() -> dict[str, Any]:
    return {
        "phase": "awaiting_initial_review",
        "primary_actor": "reviewer",
        "primary_action": {"kind": "publish_initial_review"},
        "allowed_events_by_actor": {"reviewer": ["review", "final_review"]},
    }


def make_document(
    history: list[dict[str, Any]],
    *,
    workflow: dict[str, Any] | None = None,
    terminal: dict[str, Any] | None = None,
    open_threads: list[str] | None = None,
    resolved_threads: list[str] | None = None,
    open_gaps: list[str] | None = None,
    resolved_gaps: list[str] | None = None,
    fingerprint: str | None = None,
) -> dict[str, Any]:
    return {
        "review_id": "fixture1",
        "name": "fixture-review",
        "state": {
            "workflow": workflow or waiting_workflow(),
            "threads": {
                "open": open_threads or [],
                "resolved": resolved_threads or [],
            },
            "validation_gaps": {
                "open": open_gaps or [],
                "resolved": resolved_gaps or [],
            },
            "source_fingerprint": fingerprint,
            "terminal": terminal,
        },
        "history": history,
    }


def make_thread(thread_id: str, priority: str, title: str, risk: str) -> dict[str, Any]:
    return {
        "op": "thread.open",
        "id": thread_id,
        "priority": priority,
        "contract": "internal",
        "title": title,
        "risk": risk,
        "evidence": {
            "basis": "source_inspection",
            "provenance": "fixture",
            "observed_at": "2026-08-14T01:00:00+00:00",
            "sanitized_result": "fixture evidence",
        },
        "required_behavior": "fixture required behavior",
    }


def reply(thread_id: str, decision: str, message: str, **extra: Any) -> dict[str, Any]:
    return {
        "op": "thread.reply",
        "thread_id": thread_id,
        "decision": decision,
        "message": message,
        **extra,
    }


def note(thread_id: str, tag: str, message: str) -> dict[str, Any]:
    return {
        "op": "note.attach",
        "target": {"kind": "thread", "id": thread_id},
        "tag": tag,
        "message": message,
    }


def check(name: str, result: str) -> dict[str, Any]:
    return {"op": "check.record", "check": name, "result": result}


def seven_event_lgtm_document() -> dict[str, Any]:
    """A sanitized structural mirror of loop q84vy559: three rounds to LGTM."""
    scope = ["a.py", "b.py"]
    snapshot = {"fingerprint": "f" * 64, "scope": scope}
    history = [
        {
            "kind": "review",
            "event_id": "evt_review0000",
            "occurred_at": "2026-08-14T01:00:00+00:00",
            "source_snapshot": snapshot,
            "operations": [
                make_thread("T1", "P1", "Baseline race", "Change absorbed at boundary"),
                make_thread("T2", "P2", "Version skew", "Package and artifact differ"),
                check("suite", "passed"),
            ],
        },
        {
            "kind": "owner_reply",
            "event_id": "evt_reply00001",
            "occurred_at": "2026-08-14T02:00:00+00:00",
            "completed_source_snapshot": snapshot,
            "operations": [
                reply("T1", "applied", "Fixed baseline."),
                reply("T2", "applied", "Bumped everywhere."),
                check("suite", "passed"),
                {
                    "op": "gap.open",
                    "gap_id": "G1",
                    "check": "guarded scope covers fixes",
                    "reason": "two files outside scope",
                    "material": True,
                },
            ],
        },
        {
            "kind": "reviewer_update",
            "event_id": "evt_update0001",
            "occurred_at": "2026-08-14T03:00:00+00:00",
            "source_snapshot": snapshot,
            "operations": [
                {
                    "op": "thread.resolve",
                    "thread_id": "T1",
                    "message": "Verified the repair.",
                },
                {
                    "op": "thread.comment",
                    "thread_id": "T2",
                    "message": "Bump crossed scope; revert.",
                },
                {
                    "op": "gap.resolve",
                    "gap_id": "G1",
                    "disposition": "performed",
                    "message": "Resolved by requiring the revert.",
                },
            ],
        },
        {
            "kind": "owner_reply",
            "event_id": "evt_reply00002",
            "occurred_at": "2026-08-14T04:00:00+00:00",
            "completed_source_snapshot": snapshot,
            "operations": [reply("T2", "applied", "Reverted.")],
        },
        {
            "kind": "reviewer_update",
            "event_id": "evt_update0002",
            "occurred_at": "2026-08-14T05:00:00+00:00",
            "source_snapshot": snapshot,
            "operations": [
                {
                    "op": "thread.resolve",
                    "thread_id": "T2",
                    "message": "Verified revert.",
                },
                make_thread("T3", "P2", "Unguarded tests", "Tests outside scope"),
            ],
        },
        {
            "kind": "owner_reply",
            "event_id": "evt_reply00003",
            "occurred_at": "2026-08-14T06:00:00+00:00",
            "completed_source_snapshot": snapshot,
            "operations": [reply("T3", "applied", "Relocated.")],
        },
        {
            "kind": "final_review",
            "event_id": "evt_final0001",
            "occurred_at": "2026-08-14T07:00:00+00:00",
            "source_snapshot": snapshot,
            "operations": [
                {
                    "op": "thread.resolve",
                    "thread_id": "T3",
                    "message": "Verified relocation.",
                },
                check("final suite", "passed"),
                {"op": "review.approve", "decision": "LGTM"},
            ],
        },
    ]
    return make_document(
        history,
        workflow={
            "phase": "terminal",
            "primary_actor": None,
            "primary_action": None,
            "allowed_events_by_actor": {},
        },
        terminal={"outcome": "lgtm", "occurred_at": "2026-08-14T07:00:00+00:00"},
        resolved_threads=["T1", "T2", "T3"],
        resolved_gaps=["G1"],
        fingerprint="f" * 64,
    )


def two_thread_document() -> dict[str, Any]:
    """One resolved and one open thread, each with a reply body to drop or keep."""
    open_thread = make_thread("T2", "P2", "Version skew", "Package and artifact differ")
    open_thread["paths"] = ["b.py"]
    resolved_thread = make_thread("T1", "P1", "Baseline race", "Change absorbed")
    resolved_thread["paths"] = ["a.py"]
    return make_document(
        [
            {
                "kind": "review",
                "event_id": "evt_review0000",
                "occurred_at": "2026-08-14T01:00:00+00:00",
                "operations": [resolved_thread, open_thread],
            },
            {
                "kind": "owner_reply",
                "event_id": "evt_reply00001",
                "occurred_at": "2026-08-14T02:00:00+00:00",
                "operations": [
                    reply("T1", "applied", "Serialized the baseline write."),
                    reply("T2", "applied", "Bumped both manifests."),
                ],
            },
            {
                "kind": "reviewer_update",
                "event_id": "evt_update0001",
                "occurred_at": "2026-08-14T03:00:00+00:00",
                "operations": [
                    {
                        "op": "thread.resolve",
                        "thread_id": "T1",
                        "message": "Verified against the suite.",
                    },
                    {
                        "op": "thread.comment",
                        "thread_id": "T2",
                        "message": "Still checking the skew.",
                    },
                ],
            },
        ],
        open_threads=["T2"],
        resolved_threads=["T1"],
    )


class ThreadSummaryTest(unittest.TestCase):
    # --- thread_summaries ---

    def test_summary_returns_routing_fields_and_drops_every_body(self) -> None:
        summaries = review_render.thread_summaries(two_thread_document())

        self.assertEqual(
            summaries,
            [
                {
                    "id": "T1",
                    "priority": "P1",
                    "status": "resolved",
                    "title": "Baseline race",
                    "paths": ["a.py"],
                },
                {
                    "id": "T2",
                    "priority": "P2",
                    "status": "open",
                    "title": "Version skew",
                    "paths": ["b.py"],
                },
            ],
        )

    def test_open_filter_excludes_a_resolved_thread(self) -> None:
        summaries = review_render.thread_summaries(two_thread_document(), True)

        self.assertEqual([item["id"] for item in summaries], ["T2"])

    def test_a_thread_without_declared_paths_summarizes_as_an_empty_list(self) -> None:
        document = two_thread_document()
        del document["history"][0]["operations"][1]["paths"]

        summaries = review_render.thread_summaries(document)

        self.assertEqual(summaries[1]["paths"], [])

    # --- render_summaries ---

    def test_human_summary_names_each_thread_on_one_line(self) -> None:
        rendered = review_render.render_summaries(two_thread_document(), True)

        self.assertEqual(
            rendered,
            "# Open Review Threads\n\n- T2 [P2] open: Version skew (b.py)\n",
        )

    def test_human_summary_reports_an_empty_open_set(self) -> None:
        rendered = review_render.render_summaries(make_document([]), True)

        self.assertIn("No threads.", rendered)

    # --- render_conversations ---

    def test_full_conversations_keep_every_body(self) -> None:
        rendered = review_render.render_conversations(two_thread_document())

        self.assertIn("Serialized the baseline write.", rendered)
        self.assertIn("Bumped both manifests.", rendered)
        self.assertIn("Verified against the suite.", rendered)
        self.assertIn("fixture required behavior", rendered)

    def test_open_filter_drops_a_resolved_conversation_but_keeps_its_bodies(
        self,
    ) -> None:
        rendered = review_render.render_conversations(two_thread_document(), True)

        self.assertNotIn("Baseline race", rendered)
        self.assertIn("Bumped both manifests.", rendered)

    def test_each_entry_is_labelled_by_the_act_it_records(self) -> None:
        rendered = review_render.render_conversations(two_thread_document())

        self.assertIn("### owner_reply — applied", rendered)
        self.assertIn("### reviewer_update — resolve", rendered)
        self.assertIn("### reviewer_update — comment", rendered)


class ReviewRenderTest(unittest.TestCase):
    # --- completed_rounds ---

    def test_counts_only_owner_replies_verified_by_a_reviewer_event(self) -> None:
        history = [
            {"kind": "review"},
            {"kind": "owner_reply"},
            {"kind": "reviewer_update"},
            {"kind": "owner_reply"},
        ]
        self.assertEqual(review_render.completed_rounds(history), 1)

    # --- escape_cell ---

    def test_escapes_pipes_and_collapses_line_breaks(self) -> None:
        self.assertEqual(
            review_render.escape_cell("a|b\nc\n\nd"),
            "a\\|b c<br>d",
        )

    # --- summary_notes ---

    def test_lifts_attached_notes_with_thread_attribution(self) -> None:
        document = make_document(
            [
                {
                    "kind": "owner_reply",
                    "operations": [
                        reply("T1", "applied", "Fixed."),
                        note("T1", "decision", "deferred bump"),
                    ],
                }
            ]
        )
        notes = review_render.summary_notes(document)
        self.assertEqual(
            notes, [{"text": "[decision] deferred bump", "source": "T1"}]
        )

    def test_a_note_cannot_be_written_or_removed_by_editing_a_message(self) -> None:
        document = make_document(
            [
                {
                    "kind": "owner_reply",
                    "operations": [
                        reply(
                            "T1",
                            "applied",
                            "Fixed.\nNote to user: [decision] deferred bump",
                        )
                    ],
                }
            ]
        )

        self.assertEqual(review_render.summary_notes(document), [])

    def test_a_repeated_note_contributes_once_per_transaction(self) -> None:
        document = make_document(
            [
                {
                    "kind": "owner_reply",
                    "operations": [
                        reply("T1", "applied", "Fixed."),
                        note("T1", "decision", "deferred bump"),
                        note("T1", "decision", "deferred bump"),
                        note("T1", "follow-up", "revisit after release"),
                    ],
                }
            ]
        )

        self.assertEqual(
            [item["text"] for item in review_render.summary_notes(document)],
            ["[decision] deferred bump", "[follow-up] revisit after release"],
        )

    def test_a_note_never_hides_the_blocked_work_alert(self) -> None:
        document = make_document(
            [
                {
                    "kind": "owner_reply",
                    "operations": [
                        reply(
                            "T1",
                            "deferred/blocked",
                            "Blocked.",
                            blocker="infra outage",
                            remaining_work="rerun probes",
                        ),
                        note("T1", "action-required", "waiting on infra"),
                    ],
                }
            ]
        )
        notes = review_render.summary_notes(document)
        texts_by_source = [(item["source"], item["text"]) for item in notes]

        self.assertIn(("T1", "[action-required] waiting on infra"), texts_by_source)
        self.assertIn(
            ("T1", "[blocked] infra outage Remaining work: rerun probes"),
            texts_by_source,
        )
        self.assertEqual(len(notes), 2)

    def test_timeout_terminal_adds_notes_for_open_threads_and_material_gap(
        self,
    ) -> None:
        document = make_document(
            [
                {
                    "kind": "owner_reply",
                    "operations": [
                        {
                            "op": "gap.open",
                            "gap_id": "G1",
                            "check": "live probe",
                            "reason": "service down",
                            "material": True,
                        }
                    ],
                }
            ],
            terminal={
                "outcome": "owner_timeout",
                "occurred_at": "2026-08-14T09:00:00+00:00",
            },
            open_threads=["T1"],
            open_gaps=["G1"],
        )
        notes = review_render.summary_notes(document)
        self.assertIn("owner_timeout", notes[0]["text"])
        self.assertIn("T1", notes[0]["text"])
        self.assertIn("live probe", notes[1]["text"])
        self.assertEqual(notes[1]["source"], "G1")

    # --- render_report ---

    def test_seven_event_lgtm_document_matches_summary_structure(self) -> None:
        report = review_render.render_report(seven_event_lgtm_document())
        lines = report.splitlines()
        self.assertEqual(
            lines[0], "# Review Summary — fixture-review (`fixture1`)"
        )
        self.assertIn(
            "- **Outcome: LGTM** · 3 completed review rounds, 7 events · 2026-08-14",
            lines,
        )
        # Section order is a requirement: notes before the issue table.
        self.assertLess(
            report.index("## Notes for You"), report.index("## Issue Summary")
        )
        self.assertLess(
            report.index("## Issue Summary"), report.index("## Verification")
        )
        self.assertIn(
            "None recorded — neither agent flagged a design-shifting change", report
        )
        rows = [line for line in lines if line.startswith(("| T", "| G"))]
        self.assertEqual(len(rows), 4)
        # Priority order: T1 (P1) before T2 and T3 (P2); gaps last.
        self.assertEqual(
            [row.split(" ")[1] for row in rows], ["T1", "T2", "T3", "G1"]
        )
        t2_row = next(row for row in rows if row.startswith("| T2"))
        self.assertIn("Bump crossed scope; revert.", t2_row)
        self.assertIn("**Fixed.** Verified revert.", t2_row)
        self.assertIn("LGTM applies to fingerprint `ffffffff…` over 2 files", report)

    def test_note_in_history_sets_attention_line_and_notes_section(self) -> None:
        document = seven_event_lgtm_document()
        document["history"][3]["operations"].append(
            note("T2", "decision", "bump deferred to release")
        )
        report = review_render.render_report(document)
        self.assertIn("- **Attention: 1 note for you**", report)
        self.assertIn(
            "- **[decision]** bump deferred to release *(T2)*", report
        )

    def test_mid_loop_header_names_waiting_actor_and_open_threads(self) -> None:
        document = make_document(
            [
                {
                    "kind": "review",
                    "occurred_at": "2026-08-14T01:00:00+00:00",
                    "operations": [make_thread("T1", "P2", "Finding", "Risk text")],
                }
            ],
            workflow={
                "phase": "owner_response",
                "primary_actor": "owner",
                "primary_action": {"kind": "reply_to_open_threads"},
                "allowed_events_by_actor": {"owner": ["owner_reply"]},
            },
            open_threads=["T1"],
        )
        report = review_render.render_report(document)
        self.assertIn(
            "- **In progress** — waiting on: owner to reply_to_open_threads"
            " · open: T1",
            report,
        )
        self.assertIn("in progress, awaiting owner", report)

    def test_empty_history_renders_empty_state_summary(self) -> None:
        report = review_render.render_report(make_document([]))
        self.assertIn(
            "- **In progress** — waiting on: reviewer to publish_initial_review"
            " · open: none",
            report,
        )
        self.assertIn("- No notes — nothing flagged for you", report)
        self.assertIn("No findings raised.", report)

    def test_declined_thread_renders_declined_label_and_reason(self) -> None:
        document = make_document(
            [
                {
                    "kind": "review",
                    "operations": [make_thread("T1", "P2", "Finding", "Risk")],
                },
                {
                    "kind": "owner_reply",
                    "operations": [
                        reply("T1", "declined", "Intentional behavior.")
                    ],
                },
                {
                    "kind": "reviewer_update",
                    "operations": [
                        {
                            "op": "thread.resolve",
                            "thread_id": "T1",
                            "message": "Independently verified.",
                        }
                    ],
                },
            ],
            workflow=waiting_workflow(),
            resolved_threads=["T1"],
        )
        report = review_render.render_report(document)
        self.assertIn("**Declined, independently verified.**", report)
        self.assertIn("Intentional behavior.", report)

    def test_note_attached_at_raise_time_is_lifted(self) -> None:
        document = make_document(
            [
                {
                    "kind": "review",
                    "operations": [
                        make_thread("T1", "P2", "Finding", "Risk"),
                        note("T1", "decision", "raised as a constraint"),
                    ],
                }
            ],
            open_threads=["T1"],
        )
        notes = review_render.summary_notes(document)
        self.assertEqual(
            notes, [{"text": "[decision] raised as a constraint", "source": "T1"}]
        )

    def test_header_names_prior_review_when_linked(self) -> None:
        document = make_document([])
        document["prior_review_id"] = "q84vy559"
        report = review_render.render_report(document)
        self.assertIn("- Prior review: `q84vy559`", report)

    def test_verification_rolls_up_earlier_rounds_and_keeps_failures(self) -> None:
        document = make_document(
            [
                {
                    "kind": "review",
                    "operations": [
                        check("suite: 46 tests", "passed"),
                        check("live probe", "failed"),
                    ],
                },
                {
                    "kind": "owner_reply",
                    "operations": [check("suite: 49 tests", "passed")],
                },
                {
                    "kind": "final_review",
                    "operations": [check("final suite: 68 tests", "passed")],
                },
            ]
        )
        report = review_render.render_report(document)
        self.assertIn("- passed: final suite: 68 tests", report)
        self.assertIn("- failed (earlier round): live probe", report)
        self.assertIn(
            "- Earlier rounds recorded 2 more passed checks;"
            " see `threads` or canonical JSON",
            report,
        )
        self.assertNotIn("- passed: suite: 46 tests", report)
        self.assertNotIn("- passed: suite: 49 tests", report)

    def test_timeout_verification_recovers_latest_guarded_snapshot(self) -> None:
        snapshot = {"fingerprint": "a" * 64, "scope": ["x.py"]}
        document = make_document(
            [
                {
                    "kind": "review",
                    "occurred_at": "2026-08-14T01:00:00+00:00",
                    "source_snapshot": snapshot,
                    "operations": [make_thread("T1", "P2", "Finding", "Risk")],
                },
                {
                    "kind": "owner_timeout",
                    "occurred_at": "2026-08-14T04:00:00+00:00",
                    "operations": [
                        {
                            "op": "timeout.declare",
                            "started_at": "2026-08-14T01:00:00+00:00",
                            "deadline": "2026-08-14T03:00:00+00:00",
                            "reason": "No response.",
                        }
                    ],
                },
            ],
            terminal={
                "outcome": "owner_timeout",
                "occurred_at": "2026-08-14T04:00:00+00:00",
            },
            open_threads=["T1"],
            fingerprint="a" * 64,
        )
        report = review_render.render_report(document)
        # The timeout event has no snapshot; scope must come from the earlier
        # event that recorded the guarded fingerprint.
        self.assertIn(
            "- Recorded source fingerprint `aaaaaaaa…` over 1 file: `x.py`",
            report,
        )
        self.assertIn(
            "- **Outcome: ended by owner_timeout — review incomplete**", report
        )

    def test_message_content_cannot_break_table_or_heading_structure(self) -> None:
        hostile = "evil|cell\n# fake heading\n\n<script>block</script>"
        document = make_document(
            [
                {
                    "kind": "review",
                    "operations": [make_thread("T1", "P2", hostile, hostile)],
                }
            ],
            open_threads=["T1"],
        )
        report = review_render.render_report(document)
        self.assertNotIn("\n# fake heading", report)
        self.assertNotIn("<script>", report)
        headings = [line for line in report.splitlines() if line.startswith("# ")]
        self.assertEqual(len(headings), 1)
        row = next(
            line for line in report.splitlines() if line.startswith("| T1")
        )
        self.assertIn("evil\\|cell", row)
        self.assertIn("&lt;script&gt;", row)


class StructureRenderTest(unittest.TestCase):
    def deferred_document(self, disposition: str) -> dict[str, Any]:
        return make_document(
            [
                {
                    "kind": "final_review",
                    "operations": [
                        {
                            "op": "review.approve",
                            "decision": "LGTM",
                            "structure_debt": {
                                "disposition": disposition,
                                "flagged_paths": ["src/reconciler.py"],
                                "message": "Real accretion.",
                            },
                        }
                    ],
                }
            ]
        )

    def test_deferred_structure_debt_surfaces_in_notes(self) -> None:
        report = review_render.render_report(
            self.deferred_document("structure_deferred")
        )
        self.assertIn("**[structure]**", report)
        self.assertIn("src/reconciler.py", report)
        self.assertIn("Real accretion.", report)

    def test_reviewed_structure_debt_stays_out_of_notes(self) -> None:
        report = review_render.render_report(
            self.deferred_document("structure_reviewed")
        )
        self.assertNotIn("[structure]", report)

    def test_structure_round_header_names_its_kind(self) -> None:
        document = make_document([])
        document["review_kind"] = "structure"
        report = review_render.render_report(document)
        self.assertIn(
            "- Kind: structure round — behavior-preserving shape review", report
        )
        document["review_kind"] = "correctness"
        self.assertNotIn(
            "Kind: structure round", review_render.render_report(document)
        )
