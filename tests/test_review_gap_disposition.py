"""Validation-gap disposition tests.

A gap resolved without performing its check must never read as a passing check.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import review_render
import review_schema


JUSTIFICATION = {
    "unperformed_check": "live probe of the media service",
    "fail_closed_behavior": "a missing validator raises before any byte is retained",
}


def gap_event(disposition: str | None, justification: dict | None = None) -> dict:
    resolution: dict = {
        "gap_id": "G1",
        "message": "The live probe was still unavailable.",
        "evidence": {
            "basis": "source_inspection",
            "provenance": "scripts/reader.py",
            "observed_at": "2026-08-17T12:00:00+00:00",
            "sanitized_result": "Missing validators fail before any byte is retained.",
        },
    }
    if disposition is not None:
        resolution["disposition"] = disposition
    if justification is not None:
        resolution["justification"] = justification
    return resolution


def document_with_resolved_gap(disposition: str) -> dict:
    return {
        "state": {
            "workflow": {
                "phase": "terminal",
                "primary_actor": None,
                "primary_action": None,
                "allowed_events_by_actor": {},
            },
            "validation_gaps": {"open": [], "resolved": ["G1"]},
            "threads": {"open": [], "resolved": ["G1"]},
            "terminal": {"outcome": "lgtm"},
        },
        "history": [
            {
                "kind": "review",
                "validation": {
                    "performed": [],
                    "gaps": [
                        {
                            "gap_id": "G1",
                            "check": "live probe of the media service",
                            "reason": "no network access in this environment",
                            "material": False,
                        }
                    ],
                },
            },
            {
                "kind": "final_review",
                "gap_resolutions": [
                    gap_event(
                        disposition,
                        JUSTIFICATION
                        if disposition == "unavailable_non_material"
                        else None,
                    )
                ],
            },
        ],
    }


# --- gap disposition validation ---


class GapDispositionValidationTest(unittest.TestCase):
    def errors_for(self, resolution: dict) -> list[str]:
        event = {
            "event_id": "evt_" + "a" * 24,
            "kind": "final_review",
            "gap_resolutions": [resolution],
            "occurred_at": "2026-08-17T12:00:00+00:00",
        }
        return review_schema.validate_event(event)

    def test_a_resolution_without_a_disposition_is_rejected(self) -> None:
        messages = " ".join(self.errors_for(gap_event(None)))

        self.assertIn("disposition", messages)

    def test_an_unrecognized_disposition_is_rejected(self) -> None:
        messages = " ".join(self.errors_for(gap_event("waived")))

        self.assertIn("disposition", messages)

    def test_an_unavailable_check_requires_a_structured_justification(self) -> None:
        """The report states the check was not performed and that it fails closed.

        Those claims must come from the event, not from the renderer, so a resolution
        that omits them cannot be published.
        """

        messages = " ".join(self.errors_for(gap_event("unavailable_non_material")))

        self.assertIn("justification", messages)
        self.assertIn("unperformed_check", messages)
        self.assertIn("fail_closed_behavior", messages)

    def test_a_justification_missing_the_fail_closed_behavior_is_rejected(self) -> None:
        messages = " ".join(
            self.errors_for(
                gap_event(
                    "unavailable_non_material",
                    {"unperformed_check": "live probe of the media service"},
                )
            )
        )

        self.assertIn("fail_closed_behavior", messages)

    def test_a_blank_justification_field_is_rejected(self) -> None:
        messages = " ".join(
            self.errors_for(
                gap_event(
                    "unavailable_non_material",
                    {**JUSTIFICATION, "unperformed_check": ""},
                )
            )
        )

        self.assertIn("unperformed_check", messages)

    def test_an_unknown_justification_field_is_rejected(self) -> None:
        messages = " ".join(
            self.errors_for(
                gap_event(
                    "unavailable_non_material", {**JUSTIFICATION, "waiver": "approved"}
                )
            )
        )

        self.assertIn("waiver", messages)

    def test_a_performed_check_may_not_carry_a_justification(self) -> None:
        messages = " ".join(self.errors_for(gap_event("performed", JUSTIFICATION)))

        self.assertIn("justification", messages)

    def test_a_complete_justification_is_accepted(self) -> None:
        messages = " ".join(
            self.errors_for(gap_event("unavailable_non_material", JUSTIFICATION))
        )

        self.assertNotIn("justification", messages)

    def test_the_supported_dispositions_do_not_include_a_material_gap(self) -> None:
        # A still-material gap is not resolved at all; it stays open and blocks LGTM.
        self.assertEqual(
            review_schema.GAP_DISPOSITIONS,
            {"performed", "unavailable_non_material"},
        )


# --- report rendering ---


class GapDispositionRenderingTest(unittest.TestCase):
    def test_an_unavailable_check_is_never_rendered_as_performed(self) -> None:
        lines = review_render.render_issue_summary(
            document_with_resolved_gap("unavailable_non_material")
        )
        row = next(line for line in lines if "G1" in line)

        self.assertIn("without performing the check", row)
        # The named check and fail-closed behavior are quoted from the event.
        self.assertIn("live probe of the media service", row)
        self.assertIn("before any byte is retained", row)

    def test_a_performed_check_renders_as_an_ordinary_resolution(self) -> None:
        lines = review_render.render_issue_summary(
            document_with_resolved_gap("performed")
        )
        row = next(line for line in lines if "G1" in line)

        self.assertIn("Resolved.", row)
        self.assertNotIn("without performing", row)

    def test_gap_records_carry_the_disposition(self) -> None:
        records = review_render.gap_records(
            document_with_resolved_gap("unavailable_non_material")["history"]
        )

        self.assertEqual(records["G1"]["disposition"], "unavailable_non_material")


if __name__ == "__main__":
    unittest.main()
