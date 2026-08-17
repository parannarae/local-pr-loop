"""Build blank and history-aware review event drafts."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any

from review_contract import SOURCE_FIELD_BY_KIND, TIMEOUT_DURATION_BY_KIND


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


def blank_validation() -> dict[str, list[Any]]:
    """Return an empty validation result."""
    return {"performed": [], "gaps": []}


def blank_thread() -> dict[str, Any]:
    """Return the documented initial thread shape."""
    return {
        "id": "T1",
        "priority": "P1",
        "contract": "internal",
        "title": "",
        "risk": "",
        "evidence": blank_evidence(),
        "required_behavior": "",
    }


def event_template(kind: str) -> dict[str, Any]:
    """Build a blank event of the requested documented kind."""
    base: dict[str, Any] = {
        "event_id": f"evt_{secrets.token_hex(12)}",
        "kind": kind,
    }
    if kind == "review":
        base.update(
            {
                "source_snapshot": blank_snapshot(),
                "threads": [blank_thread()],
                "validation": blank_validation(),
            }
        )
    elif kind == "source_update":
        base.update(
            {
                "source_snapshot": blank_snapshot(),
                "reason": "",
                "thread_impacts": [],
                "new_threads": [],
                "validation": blank_validation(),
            }
        )
    elif kind == "owner_reply":
        base.update(
            {
                "starting_source_snapshot": blank_snapshot(),
                "source_drift_assessment": "",
                "completed_source_snapshot": blank_snapshot(),
                "replies": [],
                "files_changed": [],
                "guide_synchronization": "",
                "validation": blank_validation(),
                "commits": [],
            }
        )
    elif kind == "reviewer_update":
        base.update(
            {
                "source_snapshot": blank_snapshot(),
                "decisions": [],
                "new_threads": [],
                "validation": blank_validation(),
            }
        )
    elif kind == "final_review":
        base.update(
            {
                "source_snapshot": blank_snapshot(),
                "resolutions": [],
                "gap_resolutions": [],
                "decision": "",
                "validation": blank_validation(),
            }
        )
    else:
        base.update({"reason": "", "started_at": "", "deadline": ""})
    base["occurred_at"] = datetime.now(timezone.utc).isoformat()
    return base


def _current_snapshot(document: dict[str, Any]) -> dict[str, Any] | None:
    for event in reversed(document.get("history", [])):
        if not isinstance(event, dict):
            continue
        field = SOURCE_FIELD_BY_KIND.get(event.get("kind"))
        value = event.get(field) if field else None
        if isinstance(value, dict):
            return value
    return None


def _latest_owner_replies(
    document: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    for event in reversed(document.get("history", [])):
        if isinstance(event, dict) and event.get("kind") == "owner_reply":
            return {
                item["thread_id"]: item
                for item in event.get("replies", [])
                if isinstance(item, dict) and isinstance(item.get("thread_id"), str)
            }
    return {}


def contextual_event_template(
    document: dict[str, Any], kind: str, guarded_snapshot: dict[str, Any]
) -> dict[str, Any]:
    """Build a draft prefilled from projected workflow obligations."""
    template = event_template(kind)
    open_threads = document["state"]["threads"]["open"]
    open_gaps = document["state"]["validation_gaps"]["open"]
    prior_snapshot = _current_snapshot(document)
    if kind in SOURCE_FIELD_BY_KIND:
        template[SOURCE_FIELD_BY_KIND[kind]] = guarded_snapshot
    if kind == "review":
        template["threads"][0]["id"] = "T1"
    elif kind == "owner_reply":
        template["starting_source_snapshot"] = prior_snapshot or guarded_snapshot
        template["completed_source_snapshot"] = guarded_snapshot
        template["replies"] = [
            {
                "thread_id": thread_id,
                "decision": "applied",
                "message": "",
                "evidence": blank_evidence(),
            }
            for thread_id in open_threads
        ]
    elif kind in {"reviewer_update", "final_review"}:
        replies = _latest_owner_replies(document)
        action_key = "decisions" if kind == "reviewer_update" else "resolutions"
        actions = []
        for thread_id in open_threads:
            action: dict[str, Any] = {"thread_id": thread_id, "message": ""}
            if kind == "reviewer_update":
                action["action"] = "comment"
            if replies.get(thread_id, {}).get("decision") == "declined":
                action["verification"] = {
                    "independent": True,
                    "evidence": blank_evidence(),
                }
            actions.append(action)
        template[action_key] = actions
        template["gap_resolutions"] = [
            {
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
    elif kind in {"owner_timeout", "reviewer_timeout", "initial_review_timeout"}:
        latest = document["state"].get("latest_event")
        started_text = (
            document.get("created_at")
            if kind == "initial_review_timeout"
            else latest["occurred_at"] if isinstance(latest, dict) else None
        )
        if started_text:
            # Copy the anchor verbatim: projection compares started_at to the
            # canonical anchor as raw text, so reserializing a Z-form value as
            # +00:00 would generate a rejected event. Parse only for the
            # deadline arithmetic.
            started = datetime.fromisoformat(started_text.replace("Z", "+00:00"))
            template["started_at"] = started_text
            template["deadline"] = (
                started + TIMEOUT_DURATION_BY_KIND[kind]
            ).isoformat()
        template["reason"] = "The active handoff deadline elapsed without a response."
    template["occurred_at"] = datetime.now(timezone.utc).isoformat()
    return template
