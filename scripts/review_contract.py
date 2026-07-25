"""Shared immutable workflow constants."""

from datetime import timedelta

SOURCE_FIELD_BY_KIND = {
    "review": "source_snapshot",
    "source_update": "source_snapshot",
    "owner_reply": "completed_source_snapshot",
    "reviewer_update": "source_snapshot",
    "final_review": "source_snapshot",
}
TIMEOUT_DURATION_BY_KIND = {
    "reviewer_timeout": timedelta(minutes=30),
    "owner_timeout": timedelta(hours=2),
}
