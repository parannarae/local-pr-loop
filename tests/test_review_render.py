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


def seven_event_lgtm_document() -> dict[str, Any]:
    """A sanitized structural mirror of loop q84vy559: three rounds to LGTM."""
    scope = ["a.py", "b.py"]
    snapshot = {"fingerprint": "f" * 64, "scope": scope}
    history = [
        {
            "kind": "review",
            "event_id": "evt_review0000",
            "occurred_at": "2026-08-14T01:00:00+00:00",
            "threads": [
                make_thread("T1", "P1", "Baseline race", "Change absorbed at boundary"),
                make_thread("T2", "P2", "Version skew", "Package and artifact differ"),
            ],
            "source_snapshot": snapshot,
            "validation": {"performed": [{"check": "suite", "result": "passed"}], "gaps": []},
        },
        {
            "kind": "owner_reply",
            "event_id": "evt_reply00001",
            "occurred_at": "2026-08-14T02:00:00+00:00",
            "replies": [
                {"thread_id": "T1", "decision": "applied", "message": "Fixed baseline."},
                {"thread_id": "T2", "decision": "applied", "message": "Bumped everywhere."},
            ],
            "validation": {
                "performed": [{"check": "suite", "result": "passed"}],
                "gaps": [
                    {
                        "gap_id": "G1",
                        "check": "guarded scope covers fixes",
                        "reason": "two files outside scope",
                        "material": True,
                    }
                ],
            },
            "completed_source_snapshot": snapshot,
        },
        {
            "kind": "reviewer_update",
            "event_id": "evt_update0001",
            "occurred_at": "2026-08-14T03:00:00+00:00",
            "decisions": [
                {"thread_id": "T1", "action": "resolve", "message": "Verified the repair."},
                {"thread_id": "T2", "action": "comment", "message": "Bump crossed scope; revert."},
            ],
            "gap_resolutions": [
                {"gap_id": "G1", "message": "Resolved by requiring the revert."}
            ],
            "source_snapshot": snapshot,
        },
        {
            "kind": "owner_reply",
            "event_id": "evt_reply00002",
            "occurred_at": "2026-08-14T04:00:00+00:00",
            "replies": [{"thread_id": "T2", "decision": "applied", "message": "Reverted."}],
            "completed_source_snapshot": snapshot,
        },
        {
            "kind": "reviewer_update",
            "event_id": "evt_update0002",
            "occurred_at": "2026-08-14T05:00:00+00:00",
            "decisions": [{"thread_id": "T2", "action": "resolve", "message": "Verified revert."}],
            "new_threads": [
                make_thread("T3", "P2", "Unguarded tests", "Tests outside scope")
            ],
            "source_snapshot": snapshot,
        },
        {
            "kind": "owner_reply",
            "event_id": "evt_reply00003",
            "occurred_at": "2026-08-14T06:00:00+00:00",
            "replies": [{"thread_id": "T3", "decision": "applied", "message": "Relocated."}],
            "completed_source_snapshot": snapshot,
        },
        {
            "kind": "final_review",
            "event_id": "evt_final0001",
            "occurred_at": "2026-08-14T07:00:00+00:00",
            "decision": "LGTM",
            "resolutions": [{"thread_id": "T3", "message": "Verified relocation."}],
            "source_snapshot": snapshot,
            "validation": {"performed": [{"check": "final suite", "result": "passed"}], "gaps": []},
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

    def test_lifts_marked_note_lines_with_thread_attribution(self) -> None:
        document = make_document(
            [
                {
                    "kind": "owner_reply",
                    "replies": [
                        {
                            "thread_id": "T1",
                            "decision": "applied",
                            "message": "Fixed.\nNote to user: [decision] deferred bump",
                        }
                    ],
                }
            ]
        )
        notes = review_render.summary_notes(document)
        self.assertEqual(
            notes, [{"text": "[decision] deferred bump", "source": "T1"}]
        )

    def test_blocked_alert_survives_unrelated_note_and_yields_to_duplicate(
        self,
    ) -> None:
        document = make_document(
            [
                {
                    "kind": "owner_reply",
                    "replies": [
                        {
                            "thread_id": "T1",
                            "decision": "deferred/blocked",
                            "message": "Blocked.\nNote to user: waiting on infra",
                            "blocker": "infra outage",
                            "remaining_work": "rerun probes",
                        },
                        {
                            "thread_id": "T2",
                            "decision": "deferred/blocked",
                            "message": (
                                "Blocked.\nNote to user: [blocked] missing"
                                " dataset Remaining work: load fixture"
                            ),
                            "blocker": "missing dataset",
                            "remaining_work": "load fixture",
                        },
                    ],
                }
            ]
        )
        notes = review_render.summary_notes(document)
        # T1's unrelated note must not hide its blocked-work alert; T2's note
        # is an exact normalized duplicate, so the automatic copy is dropped.
        texts_by_source = [(note["source"], note["text"]) for note in notes]
        self.assertIn(("T1", "waiting on infra"), texts_by_source)
        self.assertIn(
            ("T1", "[blocked] infra outage Remaining work: rerun probes"),
            texts_by_source,
        )
        self.assertEqual(
            [item for item in texts_by_source if item[0] == "T2"],
            [("T2", "[blocked] missing dataset Remaining work: load fixture")],
        )
        self.assertEqual(len(notes), 3)

    def test_timeout_terminal_adds_notes_for_open_threads_and_material_gap(
        self,
    ) -> None:
        document = make_document(
            [
                {
                    "kind": "owner_reply",
                    "validation": {
                        "performed": [],
                        "gaps": [
                            {
                                "gap_id": "G1",
                                "check": "live probe",
                                "reason": "service down",
                                "material": True,
                            }
                        ],
                    },
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
        rows = [line for line in lines if line.startswith("| T") or line.startswith("| G")]
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
        document["history"][3]["replies"][0]["message"] += (
            "\nNote to user: [decision] bump deferred to release"
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
                    "threads": [make_thread("T1", "P2", "Finding", "Risk text")],
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
                    "threads": [make_thread("T1", "P2", "Finding", "Risk")],
                },
                {
                    "kind": "owner_reply",
                    "replies": [
                        {
                            "thread_id": "T1",
                            "decision": "declined",
                            "message": "Intentional behavior.",
                        }
                    ],
                },
                {
                    "kind": "reviewer_update",
                    "decisions": [
                        {
                            "thread_id": "T1",
                            "action": "resolve",
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

    def test_timeout_verification_recovers_latest_guarded_snapshot(self) -> None:
        snapshot = {"fingerprint": "a" * 64, "scope": ["x.py"]}
        document = make_document(
            [
                {
                    "kind": "review",
                    "occurred_at": "2026-08-14T01:00:00+00:00",
                    "threads": [make_thread("T1", "P2", "Finding", "Risk")],
                    "source_snapshot": snapshot,
                },
                {
                    "kind": "owner_timeout",
                    "occurred_at": "2026-08-14T04:00:00+00:00",
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
                    "threads": [make_thread("T1", "P2", hostile, hostile)],
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
