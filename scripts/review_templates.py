"""Build blank and history-aware review transaction drafts."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any

from review_contract import (
    SOURCE_FIELD_BY_KIND,
    TIMEOUT_DURATION_BY_KIND,
    source_field_for,
)


def blank_snapshot() -> dict[str, Any]:
    """Return the documented source-snapshot shape with blank values."""
    return {
        "revision": "",
        "scope": [],
        "fingerprint": "",
        "exclusions": [],
        "additional_inputs": [],
        "staged_sha256": "",
        "unstaged_sha256": "",
        "untracked": [],
    }


def blank_evidence() -> dict[str, Any]:
    """Return the documented evidence shape with blank descriptive fields."""
    return {
        "basis": "source_inspection",
        "provenance": "",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "sanitized_result": "",
    }


def event_template(kind: str) -> dict[str, Any]:
    """Build a blank transaction of the requested documented kind.

    Only obligations whose set is already known are prefilled as skeletons. A
    review's findings are not such a set, so it opens with no operations and the
    composer appends each `thread.open` it is given.
    """
    base: dict[str, Any] = {
        "event_id": f"evt_{secrets.token_hex(12)}",
        "kind": kind,
    }
    operations: list[dict[str, Any]] = []
    if kind == "review":
        base["source_snapshot"] = blank_snapshot()
    elif kind == "source_update":
        base["source_snapshot"] = blank_snapshot()
        operations = [
            {"op": "source.replace", "snapshot": blank_snapshot(), "reason": ""}
        ]
    elif kind == "owner_reply":
        base.update(
            {
                "starting_source_snapshot": blank_snapshot(),
                "completed_source_snapshot": blank_snapshot(),
                "source_drift_assessment": "",
                "guide_synchronization": "",
                "changed_files": [],
                "revisions": [],
            }
        )
    elif kind == "reviewer_update":
        base["source_snapshot"] = blank_snapshot()
    elif kind == "final_review":
        base["source_snapshot"] = blank_snapshot()
        operations = [{"op": "review.approve", "decision": ""}]
    else:
        operations = [
            {"op": "timeout.declare", "started_at": "", "deadline": "", "reason": ""}
        ]
    base["operations"] = operations
    base["occurred_at"] = datetime.now(timezone.utc).isoformat()
    return base


def _current_snapshot(document: dict[str, Any]) -> dict[str, Any] | None:
    for event in reversed(document.get("history", [])):
        if not isinstance(event, dict):
            continue
        field = source_field_for(event.get("kind"))
        value = event.get(field) if field else None
        if isinstance(value, dict):
            return value
    return None


def latest_owner_replies(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return the most recent owner reply per thread, keyed by thread ID.

    Only the latest `owner_reply` transaction is read: resolving a declined
    thread demands independent verification against that reply, not against an
    earlier one the owner has since superseded.
    """
    for event in reversed(document.get("history", [])):
        if not isinstance(event, dict) or event.get("kind") != "owner_reply":
            continue
        operations = event.get("operations")
        return {
            item["thread_id"]: item
            for item in (operations if isinstance(operations, list) else [])
            if isinstance(item, dict)
            and item.get("op") == "thread.reply"
            and isinstance(item.get("thread_id"), str)
        }
    return {}


def _gap_resolution_skeletons(open_gaps: list[str]) -> list[dict[str, Any]]:
    return [
        {
            "op": "gap.resolve",
            "gap_id": gap_id,
            # "performed" when the check was finally run, or
            # "unavailable_non_material" when it still was not. For the latter the
            # message must name the check that was not performed, the independent
            # evidence used to judge the residual risk, and the fail-closed behavior
            # that makes it non-material. A gap that is still material stays open.
            "disposition": "",
            "message": "",
            "evidence": blank_evidence(),
        }
        for gap_id in open_gaps
    ]


def contextual_event_template(
    document: dict[str, Any],
    kind: str,
    guarded_snapshot: dict[str, Any],
    flagged_paths: list[str] | None = None,
) -> dict[str, Any]:
    """Build a draft prefilled from projected workflow obligations.

    `flagged_paths` is the accretion ledger's flagged set, computed by the caller
    against the guarded tree; a non-empty value prefills the `structure_debt`
    acknowledgment a final_review's `review.approve` must then carry.
    """
    template = event_template(kind)
    open_threads = document["state"]["threads"]["open"]
    open_gaps = document["state"]["validation_gaps"]["open"]
    prior_snapshot = _current_snapshot(document)
    if kind in SOURCE_FIELD_BY_KIND:
        template[SOURCE_FIELD_BY_KIND[kind]] = guarded_snapshot
    if kind == "source_update":
        template["operations"][0]["snapshot"] = guarded_snapshot
    elif kind == "owner_reply":
        template["starting_source_snapshot"] = prior_snapshot or guarded_snapshot
        template["completed_source_snapshot"] = guarded_snapshot
        template["operations"] = [
            {
                "op": "thread.reply",
                "thread_id": thread_id,
                "decision": "applied",
                "message": "",
                "evidence": blank_evidence(),
            }
            for thread_id in open_threads
        ]
    elif kind in {"reviewer_update", "final_review"}:
        replies = latest_owner_replies(document)
        operations: list[dict[str, Any]] = []
        for thread_id in open_threads:
            if kind == "reviewer_update":
                operations.append(
                    {"op": "thread.comment", "thread_id": thread_id, "message": ""}
                )
                continue
            resolution: dict[str, Any] = {
                "op": "thread.resolve",
                "thread_id": thread_id,
                "message": "",
            }
            if replies.get(thread_id, {}).get("decision") == "declined":
                resolution["verification"] = {
                    "independent": True,
                    "evidence": blank_evidence(),
                }
            operations.append(resolution)
        operations.extend(_gap_resolution_skeletons(open_gaps))
        if kind == "final_review":
            approval: dict[str, Any] = {"op": "review.approve", "decision": ""}
            if flagged_paths:
                approval["structure_debt"] = {
                    # "structure_reviewed" when the flagged growth is not accretion or a
                    # structure round already covered it; "structure_deferred" when the
                    # debt is real and left for a later structure round. The message
                    # records why.
                    "disposition": "",
                    "flagged_paths": sorted(flagged_paths),
                    "message": "",
                }
            operations.append(approval)
        template["operations"] = operations
    elif kind in TIMEOUT_DURATION_BY_KIND:
        declaration = template["operations"][0]
        latest = document["state"].get("latest_event")
        started_text = (
            document.get("created_at")
            if kind == "initial_review_timeout"
            else latest["occurred_at"] if isinstance(latest, dict) else None
        )
        if started_text:
            # Copy the anchor verbatim: projection compares started_at to the
            # canonical anchor as raw text, so reserializing a Z-form value as
            # +00:00 would generate a rejected transaction. Parse only for the
            # deadline arithmetic.
            started = datetime.fromisoformat(started_text.replace("Z", "+00:00"))
            declaration["started_at"] = started_text
            declaration["deadline"] = (
                started + TIMEOUT_DURATION_BY_KIND[kind]
            ).isoformat()
        declaration["reason"] = (
            "The active handoff deadline elapsed without a response."
        )
    template["occurred_at"] = datetime.now(timezone.utc).isoformat()
    return template
