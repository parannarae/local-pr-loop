"""Shared immutable workflow constants."""

from __future__ import annotations

from datetime import timedelta

SOURCE_FIELD_BY_KIND = {
    "review": "source_snapshot",
    "source_update": "source_snapshot",
    "owner_reply": "completed_source_snapshot",
    "reviewer_update": "source_snapshot",
    "final_review": "source_snapshot",
}


def source_field_for(kind: object) -> str | None:
    """Return the snapshot field a recognized event kind records, or None.

    Takes the kind as read from untrusted history, so a missing or non-string
    value means "no snapshot field" rather than a lookup error.
    """
    if isinstance(kind, str):
        return SOURCE_FIELD_BY_KIND.get(kind)
    return None


TIMEOUT_DURATION_BY_KIND = {
    "reviewer_timeout": timedelta(minutes=30),
    "owner_timeout": timedelta(hours=2),
    "initial_review_timeout": timedelta(hours=2),
}
