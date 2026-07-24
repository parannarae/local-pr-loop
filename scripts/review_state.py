#!/usr/bin/env python3
"""Validate thread-based local review JSON and render its latest report."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

STATUS_BY_KIND = {
    "review": "OWNER ACTION REQUIRED",
    "source_update": "OWNER ACTION REQUIRED",
    "owner_reply": "REVIEWER ACTION REQUIRED",
    "reviewer_update": "OWNER ACTION REQUIRED",
    "final_review": "LGTM",
    "reviewer_timeout": "REVIEWER TIMED OUT",
    "owner_timeout": "OWNER TIMED OUT",
}
ROLE_BY_KIND = {
    "review": "Reviewer",
    "source_update": "Reviewer",
    "owner_reply": "Owner",
    "reviewer_update": "Reviewer",
    "final_review": "Reviewer",
    "reviewer_timeout": "Owner",
    "owner_timeout": "Reviewer",
}
TERMINAL_KINDS = {"final_review", "reviewer_timeout", "owner_timeout"}
SOURCE_FIELD_BY_KIND = {
    "review": "source_snapshot",
    "source_update": "source_snapshot",
    "owner_reply": "completed_source_snapshot",
    "reviewer_update": "source_snapshot",
    "final_review": "source_snapshot",
}
TIME_FIELD_BY_KIND = {
    "review": "submitted_at",
    "source_update": "submitted_at",
    "owner_reply": "completed_at",
    "reviewer_update": "submitted_at",
    "final_review": "completed_at",
    "reviewer_timeout": "timed_out_at",
    "owner_timeout": "timed_out_at",
}
TIMEOUT_DURATION_BY_KIND = {
    "reviewer_timeout": timedelta(minutes=30),
    "owner_timeout": timedelta(hours=2),
}
THREAD_PRIORITIES = {"P0", "P1", "P2", "P3"}
THREAD_ID_PATTERN = re.compile(r"^T([1-9][0-9]*)$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
MODE_PATTERN = re.compile(r"^[0-7]{4}$")
REVIEW_ID_PATTERN = re.compile(r"^[abcdefghjkmnpqrstuvwxyz23456789]{8}$")
REVIEW_NAME_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
SNAPSHOT_IDENTITY_FIELDS = (
    "revision",
    "scope",
    "exclusions",
    "additional_inputs",
    "fingerprint",
)


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject ambiguous JSON objects instead of accepting the last duplicate key."""
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def load_json() -> Any:
    """Load one JSON value from standard input with duplicate-key protection."""
    return json.load(sys.stdin, object_pairs_hook=reject_duplicate_keys)


def event_time(event: dict[str, Any]) -> str:
    """Return the timestamp field associated with an event kind."""
    key = TIME_FIELD_BY_KIND.get(event.get("kind"))
    value = event.get(key) if key else None
    return value if isinstance(value, str) else ""


def require(errors: list[str], condition: bool, message: str) -> None:
    """Append a validation error when a required condition is false."""
    if not condition:
        errors.append(message)


def validate_string_list(
    errors: list[str],
    value: Any,
    prefix: str,
    *,
    unique: bool = False,
) -> None:
    valid = (
        isinstance(value, list)
        and all(isinstance(item, str) and bool(item) for item in value)
        and (not unique or len(value) == len(set(value)))
    )
    suffix = "unique non-empty strings" if unique else "non-empty strings"
    require(errors, valid, f"{prefix} must be a list of {suffix}")


def parse_timestamp(errors: list[str], value: Any, prefix: str) -> datetime | None:
    require(errors, isinstance(value, str) and bool(value), f"{prefix} is required")
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        errors.append(f"{prefix} must be ISO 8601")
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        errors.append(f"{prefix} must include a timezone")
        return None
    return parsed


def validate_thread_id(errors: list[str], value: Any, prefix: str) -> None:
    require(
        errors,
        isinstance(value, str) and bool(THREAD_ID_PATTERN.fullmatch(value)),
        f"{prefix} must match T<N>",
    )


def validate_thread(errors: list[str], thread: Any, prefix: str) -> None:
    require(errors, isinstance(thread, dict), f"{prefix} must be a mapping")
    if not isinstance(thread, dict):
        return
    validate_thread_id(errors, thread.get("id"), f"{prefix}.id")
    require(
        errors,
        thread.get("priority") in THREAD_PRIORITIES,
        f"{prefix}.priority must be one of {', '.join(sorted(THREAD_PRIORITIES))}",
    )
    for key in ("title", "risk", "evidence", "required_behavior"):
        require(
            errors,
            isinstance(thread.get(key), str) and bool(thread[key]),
            f"{prefix}.{key} must be a non-empty string",
        )


def validate_thread_action(
    errors: list[str],
    action: Any,
    prefix: str,
    *,
    allowed_actions: set[str],
) -> None:
    require(errors, isinstance(action, dict), f"{prefix} must be a mapping")
    if not isinstance(action, dict):
        return
    validate_thread_id(errors, action.get("thread_id"), f"{prefix}.thread_id")
    require(
        errors,
        action.get("action") in allowed_actions,
        f"{prefix}.action must be one of {', '.join(sorted(allowed_actions))}",
    )
    require(
        errors,
        isinstance(action.get("message"), str) and bool(action["message"]),
        f"{prefix}.message must be a non-empty string",
    )


def validate_owner_reply(errors: list[str], reply: Any, prefix: str) -> None:
    require(errors, isinstance(reply, dict), f"{prefix} must be a mapping")
    if not isinstance(reply, dict):
        return
    validate_thread_id(errors, reply.get("thread_id"), f"{prefix}.thread_id")
    require(
        errors,
        reply.get("decision") in {"applied", "declined", "deferred/blocked"},
        f"{prefix}.decision is invalid",
    )
    for key in ("message", "evidence"):
        require(
            errors,
            isinstance(reply.get(key), str) and bool(reply[key]),
            f"{prefix}.{key} must be a non-empty string",
        )
    if reply.get("decision") == "deferred/blocked":
        for key in ("blocker", "completed_work", "remaining_work", "validation_gap"):
            require(
                errors,
                isinstance(reply.get(key), str) and bool(reply[key]),
                f"{prefix}.{key} must be a non-empty string",
            )


def validate_resolution(errors: list[str], resolution: Any, prefix: str) -> None:
    require(errors, isinstance(resolution, dict), f"{prefix} must be a mapping")
    if not isinstance(resolution, dict):
        return
    validate_thread_id(errors, resolution.get("thread_id"), f"{prefix}.thread_id")
    require(
        errors,
        isinstance(resolution.get("message"), str) and bool(resolution["message"]),
        f"{prefix}.message must be a non-empty string",
    )


def validate_snapshot(
    errors: list[str],
    snapshot: Any,
    prefix: str,
    require_fingerprint: bool = True,
) -> None:
    require(errors, isinstance(snapshot, dict), f"{prefix} must be a mapping")
    if not isinstance(snapshot, dict):
        return
    require(
        errors,
        isinstance(snapshot.get("revision"), str)
        and bool(REVISION_PATTERN.fullmatch(snapshot["revision"])),
        f"{prefix}.revision must be a full Git object ID",
    )
    scope = snapshot.get("scope")
    validate_string_list(errors, scope, f"{prefix}.scope", unique=True)
    require(errors, bool(scope), f"{prefix}.scope must not be empty")
    validate_string_list(
        errors, snapshot.get("exclusions"), f"{prefix}.exclusions", unique=True
    )
    additional_inputs = snapshot.get("additional_inputs", [])
    require(
        errors,
        isinstance(additional_inputs, list),
        f"{prefix}.additional_inputs must be a list",
    )
    if isinstance(additional_inputs, list):
        paths = []
        for index, item in enumerate(additional_inputs):
            item_prefix = f"{prefix}.additional_inputs[{index}]"
            require(errors, isinstance(item, dict), f"{item_prefix} must be a mapping")
            if not isinstance(item, dict):
                continue
            require(
                errors,
                isinstance(item.get("path"), str) and bool(item["path"]),
                f"{item_prefix}.path must be a non-empty string",
            )
            if isinstance(item.get("path"), str):
                paths.append(item["path"])
            require(
                errors,
                item.get("kind") in {"file", "symlink"},
                f"{item_prefix}.kind is invalid",
            )
            require(
                errors,
                isinstance(item.get("mode"), str)
                and bool(MODE_PATTERN.fullmatch(item["mode"])),
                f"{item_prefix}.mode must be four octal digits",
            )
            require(
                errors,
                isinstance(item.get("sha256"), str)
                and bool(SHA256_PATTERN.fullmatch(item["sha256"])),
                f"{item_prefix}.sha256 must be lowercase SHA-256",
            )
            if item.get("kind") == "symlink":
                require(
                    errors,
                    isinstance(item.get("link_target"), str)
                    and bool(item["link_target"]),
                    f"{item_prefix}.link_target is required for a symlink",
                )
        require(
            errors,
            len(paths) == len(set(paths)),
            f"{prefix}.additional_inputs paths must be unique",
        )
    if require_fingerprint:
        require(
            errors,
            isinstance(snapshot.get("fingerprint"), str)
            and bool(SHA256_PATTERN.fullmatch(snapshot["fingerprint"])),
            f"{prefix}.fingerprint must be lowercase SHA-256",
        )


def snapshot_identity(snapshot: Any) -> dict[str, Any] | None:
    if not isinstance(snapshot, dict):
        return None
    return {field: snapshot.get(field) for field in SNAPSHOT_IDENTITY_FIELDS}


def snapshot_scope_basis(snapshot: Any) -> dict[str, Any] | None:
    """Return the reviewer-controlled paths that define a guarded source scope."""
    if not isinstance(snapshot, dict):
        return None
    additional_inputs = snapshot.get("additional_inputs")
    if not isinstance(additional_inputs, list):
        return None
    return {
        "scope": snapshot.get("scope"),
        "exclusions": snapshot.get("exclusions"),
        "additional_input_paths": [
            item.get("path") for item in additional_inputs if isinstance(item, dict)
        ],
    }


def validate_validation(errors: list[str], value: Any, prefix: str) -> None:
    require(errors, isinstance(value, dict), f"{prefix} must be a mapping")
    if not isinstance(value, dict):
        return
    for key in ("performed", "unavailable", "remaining_gaps"):
        validate_string_list(errors, value.get(key), f"{prefix}.{key}")


def validate_event(event: Any) -> list[str]:
    """Validate one event independently of its surrounding history."""
    errors: list[str] = []
    require(errors, isinstance(event, dict), "event must be a mapping")
    if not isinstance(event, dict):
        return errors

    kind = event.get("kind")
    require(errors, kind in STATUS_BY_KIND, f"unsupported event kind: {kind}")
    if kind not in STATUS_BY_KIND:
        return errors
    require(
        errors, event.get("status") == STATUS_BY_KIND[kind], f"{kind} status is invalid"
    )
    require(errors, event.get("role") == ROLE_BY_KIND[kind], f"{kind} role is invalid")
    time_field = TIME_FIELD_BY_KIND[kind]
    event_timestamp = parse_timestamp(errors, event.get(time_field), time_field)

    source_field = SOURCE_FIELD_BY_KIND.get(kind)
    if source_field:
        validate_snapshot(errors, event.get(source_field), source_field)

    if kind == "review":
        threads = event.get("threads")
        require(
            errors,
            isinstance(threads, list) and bool(threads),
            "review must open threads",
        )
        if isinstance(threads, list):
            for index, thread in enumerate(threads):
                validate_thread(errors, thread, f"threads[{index}]")
        validate_validation(errors, event.get("validation"), "validation")

    elif kind == "source_update":
        require(
            errors,
            isinstance(event.get("reason"), str) and bool(event["reason"]),
            "source_update.reason must be a non-empty string",
        )
        impacts = event.get("thread_impacts")
        require(errors, isinstance(impacts, list), "thread_impacts must be a list")
        if isinstance(impacts, list):
            for index, impact in enumerate(impacts):
                validate_thread_action(
                    errors,
                    impact,
                    f"thread_impacts[{index}]",
                    allowed_actions={"comment", "reopen"},
                )
        new_threads = event.get("new_threads")
        require(errors, isinstance(new_threads, list), "new_threads must be a list")
        if isinstance(new_threads, list):
            for index, thread in enumerate(new_threads):
                validate_thread(errors, thread, f"new_threads[{index}]")
        validate_validation(errors, event.get("validation"), "validation")

    elif kind == "owner_reply":
        validate_snapshot(
            errors, event.get("starting_source_snapshot"), "starting_source_snapshot"
        )
        require(
            errors,
            isinstance(event.get("source_drift_assessment"), str)
            and bool(event["source_drift_assessment"]),
            "source_drift_assessment must be a non-empty string",
        )
        replies = event.get("replies")
        require(
            errors,
            isinstance(replies, list) and bool(replies),
            "owner_reply must have replies",
        )
        if isinstance(replies, list):
            for index, reply in enumerate(replies):
                validate_owner_reply(errors, reply, f"replies[{index}]")
        validate_string_list(
            errors, event.get("files_changed"), "files_changed", unique=True
        )
        require(
            errors,
            isinstance(event.get("guide_synchronization"), str)
            and bool(event["guide_synchronization"]),
            "guide_synchronization must be a non-empty string",
        )
        validate_string_list(errors, event.get("commits"), "commits", unique=True)
        validate_validation(errors, event.get("validation"), "validation")

    elif kind == "reviewer_update":
        decisions = event.get("decisions")
        require(
            errors,
            isinstance(decisions, list) and bool(decisions),
            "reviewer_update must decide every open thread",
        )
        if isinstance(decisions, list):
            for index, decision in enumerate(decisions):
                validate_thread_action(
                    errors,
                    decision,
                    f"decisions[{index}]",
                    allowed_actions={"comment", "reopen", "resolve"},
                )
        new_threads = event.get("new_threads")
        require(errors, isinstance(new_threads, list), "new_threads must be a list")
        if isinstance(new_threads, list):
            for index, thread in enumerate(new_threads):
                validate_thread(errors, thread, f"new_threads[{index}]")
        validate_validation(errors, event.get("validation"), "validation")

    elif kind == "final_review":
        resolutions = event.get("resolutions")
        require(
            errors,
            isinstance(resolutions, list),
            "final_review.resolutions must be a list",
        )
        if isinstance(resolutions, list):
            for index, resolution in enumerate(resolutions):
                validate_resolution(errors, resolution, f"resolutions[{index}]")
        require(
            errors,
            isinstance(event.get("decision"), str) and bool(event["decision"]),
            "final_review.decision must be a non-empty string",
        )
        validate_validation(errors, event.get("validation"), "validation")

    elif kind in {"reviewer_timeout", "owner_timeout"}:
        require(
            errors,
            isinstance(event.get("reason"), str) and bool(event["reason"]),
            f"{kind}.reason must be a non-empty string",
        )
        started_at = parse_timestamp(
            errors, event.get("started_at"), f"{kind}.started_at"
        )
        deadline = parse_timestamp(errors, event.get("deadline"), f"{kind}.deadline")
        if started_at and deadline:
            require(
                errors,
                deadline == started_at + TIMEOUT_DURATION_BY_KIND[kind],
                f"{kind}.deadline has the wrong duration",
            )
        if event_timestamp and deadline:
            require(
                errors,
                event_timestamp >= deadline,
                f"{kind} occurred before its deadline",
            )

    return errors


def default_state() -> dict[str, Any]:
    return {
        "marker": "AWAITING REVIEW",
        "latest_event_kind": None,
        "updated_at": None,
        "current_source_fingerprint": None,
        "open_threads": [],
        "resolved_threads": [],
    }


def sorted_thread_ids(values: set[str]) -> list[str]:
    return sorted(
        values,
        key=lambda identifier: int(THREAD_ID_PATTERN.fullmatch(identifier).group(1)),
    )


def project_history(
    history: list[Any],
) -> tuple[list[str], dict[str, Any], dict[str, dict[str, Any]]]:
    """Validate event ordering and derive state and durable thread projections."""
    errors: list[str] = []
    state = default_state()
    threads: dict[str, dict[str, Any]] = {}
    current_snapshot: dict[str, Any] | None = None
    next_thread_number = 1
    terminal_seen = False
    previous_timestamp: datetime | None = None
    handoff_started_at: str | None = None

    def add_threads(items: Any, prefix: str) -> None:
        nonlocal next_thread_number
        if not isinstance(items, list):
            return
        for index, thread in enumerate(items):
            if not isinstance(thread, dict):
                continue
            thread_id = thread.get("id")
            expected = f"T{next_thread_number}"
            require(
                errors,
                thread_id == expected,
                f"{prefix}[{index}].id must be the next thread ID {expected}",
            )
            if isinstance(thread_id, str) and THREAD_ID_PATTERN.fullmatch(thread_id):
                require(
                    errors,
                    thread_id not in threads,
                    f"{prefix}[{index}].id is duplicated",
                )
                threads[thread_id] = {**thread, "status": "open"}
                next_thread_number += 1

    for index, event in enumerate(history):
        prefix = f"history[{index}]"
        for error in validate_event(event):
            errors.append(f"{prefix}: {error}")
        if not isinstance(event, dict):
            continue
        kind = event.get("kind")
        if kind not in STATUS_BY_KIND:
            continue
        timestamp = parse_timestamp(errors, event_time(event), f"{prefix}.timestamp")
        if timestamp:
            require(
                errors,
                timestamp <= datetime.now(timezone.utc) + timedelta(minutes=5),
                f"{prefix}: timestamp is unreasonably in the future",
            )
            if previous_timestamp:
                require(
                    errors,
                    timestamp >= previous_timestamp,
                    f"{prefix}: timestamp precedes the previous event",
                )
            previous_timestamp = timestamp
        require(errors, not terminal_seen, f"{prefix}: event follows a terminal event")

        open_ids = {
            thread_id
            for thread_id, thread in threads.items()
            if thread.get("status") == "open"
        }
        resolved_ids = set(threads) - open_ids
        marker = state["marker"]

        if kind == "review":
            require(errors, index == 0, f"{prefix}: review must be the first event")
            add_threads(event.get("threads"), f"{prefix}.threads")
            current_snapshot = event.get("source_snapshot")
            handoff_started_at = event_time(event)

        elif kind == "source_update":
            require(
                errors,
                marker in {"OWNER ACTION REQUIRED", "REVIEWER ACTION REQUIRED"},
                f"{prefix}: source_update requires an active non-terminal review",
            )
            impacts = event.get("thread_impacts", [])
            seen: set[str] = set()
            if isinstance(impacts, list):
                for impact_index, impact in enumerate(impacts):
                    if not isinstance(impact, dict):
                        continue
                    impact_prefix = f"{prefix}.thread_impacts[{impact_index}]"
                    thread_id = impact.get("thread_id")
                    known_thread = isinstance(thread_id, str) and thread_id in threads
                    require(
                        errors,
                        known_thread,
                        f"{impact_prefix} references unknown thread",
                    )
                    if not isinstance(thread_id, str):
                        continue
                    require(
                        errors,
                        thread_id not in seen,
                        f"{impact_prefix} duplicates a thread",
                    )
                    seen.add(thread_id)
                    if not known_thread:
                        continue
                    if impact.get("action") == "reopen":
                        require(
                            errors,
                            thread_id in resolved_ids,
                            f"{impact_prefix} can reopen only a resolved thread",
                        )
                        threads[thread_id]["status"] = "open"
                    elif impact.get("action") == "comment":
                        require(
                            errors,
                            thread_id in open_ids,
                            f"{impact_prefix} can comment only on an open thread",
                        )
            add_threads(event.get("new_threads"), f"{prefix}.new_threads")
            current_snapshot = event.get("source_snapshot")
            handoff_started_at = event_time(event)

        elif kind == "owner_reply":
            require(
                errors,
                marker == "OWNER ACTION REQUIRED",
                f"{prefix}: owner_reply requires an owner-action state",
            )
            require(
                errors, bool(open_ids), f"{prefix}: owner_reply has no open threads"
            )
            require(
                errors,
                snapshot_identity(event.get("starting_source_snapshot"))
                == snapshot_identity(current_snapshot),
                f"{prefix}: starting snapshot does not match current source",
            )
            require(
                errors,
                snapshot_scope_basis(event.get("completed_source_snapshot"))
                == snapshot_scope_basis(current_snapshot),
                f"{prefix}: completed snapshot changes the guarded source basis; "
                "use source_update",
            )
            replies = event.get("replies", [])
            reply_ids = [
                reply.get("thread_id")
                for reply in replies
                if isinstance(reply, dict) and isinstance(reply.get("thread_id"), str)
            ]
            require(
                errors,
                len(reply_ids) == len(set(reply_ids)),
                f"{prefix}: reply thread IDs must be unique",
            )
            require(
                errors,
                set(reply_ids) == open_ids,
                f"{prefix}: replies must address every open thread exactly once",
            )
            current_snapshot = event.get("completed_source_snapshot")
            handoff_started_at = event_time(event)

        elif kind == "reviewer_update":
            require(
                errors,
                marker == "REVIEWER ACTION REQUIRED",
                f"{prefix}: reviewer_update requires a reviewer-action state",
            )
            require(
                errors,
                snapshot_identity(event.get("source_snapshot"))
                == snapshot_identity(current_snapshot),
                f"{prefix}: reviewer snapshot does not match current source",
            )
            decisions = event.get("decisions", [])
            decision_ids = [
                decision.get("thread_id")
                for decision in decisions
                if isinstance(decision, dict)
                and isinstance(decision.get("thread_id"), str)
            ]
            require(
                errors,
                len(decision_ids) == len(set(decision_ids)),
                f"{prefix}: decision thread IDs must be unique",
            )
            decided_open_ids = {
                decision.get("thread_id")
                for decision in decisions
                if isinstance(decision, dict)
                and isinstance(decision.get("thread_id"), str)
                and decision.get("thread_id") in open_ids
                and decision.get("action") in {"comment", "resolve"}
            }
            require(
                errors,
                decided_open_ids == open_ids,
                f"{prefix}: decisions must address every open thread exactly once",
            )
            if isinstance(decisions, list):
                for decision_index, decision in enumerate(decisions):
                    if not isinstance(decision, dict):
                        continue
                    decision_prefix = f"{prefix}.decisions[{decision_index}]"
                    thread_id = decision.get("thread_id")
                    known_thread = isinstance(thread_id, str) and thread_id in threads
                    require(
                        errors,
                        known_thread,
                        f"{decision_prefix} references unknown thread",
                    )
                    if not known_thread:
                        continue
                    action = decision.get("action")
                    if thread_id in open_ids:
                        require(
                            errors,
                            action in {"comment", "resolve"},
                            f"{decision_prefix} must comment on or resolve an open thread",
                        )
                        if action == "resolve":
                            threads[thread_id]["status"] = "resolved"
                    else:
                        require(
                            errors,
                            action == "reopen",
                            f"{decision_prefix} can only reopen a resolved thread",
                        )
                        if action == "reopen":
                            threads[thread_id]["status"] = "open"
            add_threads(event.get("new_threads"), f"{prefix}.new_threads")
            remaining_open = {
                thread_id
                for thread_id, thread in threads.items()
                if thread.get("status") == "open"
            }
            require(
                errors,
                bool(remaining_open),
                f"{prefix}: use final_review when every thread can be resolved",
            )
            handoff_started_at = event_time(event)

        elif kind == "final_review":
            if index == 0:
                resolutions = event.get("resolutions", [])
                require(
                    errors,
                    isinstance(resolutions, list) and not resolutions,
                    f"{prefix}: initial approval cannot resolve unknown threads",
                )
                current_snapshot = event.get("source_snapshot")
            else:
                require(
                    errors,
                    marker == "REVIEWER ACTION REQUIRED",
                    f"{prefix}: final_review requires a reviewer-action state",
                )
                require(
                    errors,
                    snapshot_identity(event.get("source_snapshot"))
                    == snapshot_identity(current_snapshot),
                    f"{prefix}: final snapshot does not match current source",
                )
                resolutions = event.get("resolutions", [])
                resolution_ids = [
                    resolution.get("thread_id")
                    for resolution in resolutions
                    if isinstance(resolution, dict)
                    and isinstance(resolution.get("thread_id"), str)
                ]
                require(
                    errors,
                    len(resolution_ids) == len(set(resolution_ids)),
                    f"{prefix}: resolution thread IDs must be unique",
                )
                require(
                    errors,
                    set(resolution_ids) == open_ids,
                    f"{prefix}: final_review must resolve every open thread exactly once",
                )
                for thread_id in resolution_ids:
                    if thread_id in threads:
                        threads[thread_id]["status"] = "resolved"
            remaining_open = {
                thread_id
                for thread_id, thread in threads.items()
                if thread.get("status") == "open"
            }
            require(
                errors,
                not remaining_open,
                f"{prefix}: final_review leaves open threads",
            )
            terminal_seen = True

        elif kind in {"owner_timeout", "reviewer_timeout"}:
            expected_marker = (
                "OWNER ACTION REQUIRED"
                if kind == "owner_timeout"
                else "REVIEWER ACTION REQUIRED"
            )
            require(
                errors,
                marker == expected_marker,
                f"{prefix}: {kind} does not match the active role",
            )
            require(
                errors,
                event.get("started_at") == handoff_started_at,
                f"{prefix}: timeout start does not match the active handoff",
            )
            terminal_seen = True

        if current_snapshot is not None and isinstance(current_snapshot, dict):
            current_fingerprint = current_snapshot.get("fingerprint")
        else:
            current_fingerprint = None
        open_after = {
            thread_id
            for thread_id, thread in threads.items()
            if thread.get("status") == "open"
        }
        state = {
            "marker": STATUS_BY_KIND[kind],
            "latest_event_kind": kind,
            "updated_at": event_time(event),
            "current_source_fingerprint": current_fingerprint,
            "open_threads": sorted_thread_ids(open_after),
            "resolved_threads": sorted_thread_ids(set(threads) - open_after),
        }

    return errors, state, threads


def validate_document(document: Any) -> list[str]:
    errors: list[str] = []
    require(errors, isinstance(document, dict), "document must be a mapping")
    if not isinstance(document, dict):
        return errors
    require(
        errors,
        type(document.get("schema_version")) is int and document["schema_version"] == 2,
        "schema_version must be integer 2",
    )
    require(
        errors,
        isinstance(document.get("review_id"), str)
        and bool(REVIEW_ID_PATTERN.fullmatch(document["review_id"])),
        "review_id must be an eight-character review ID",
    )
    require(
        errors,
        isinstance(document.get("name"), str)
        and bool(REVIEW_NAME_PATTERN.fullmatch(document["name"])),
        "name must contain lowercase letters, digits, and single hyphens",
    )
    state = document.get("state")
    history = document.get("history")
    require(errors, isinstance(state, dict), "state must be a mapping")
    require(errors, isinstance(history, list), "history must be a list")
    if not isinstance(state, dict) or not isinstance(history, list):
        return errors

    history_errors, expected_state, _ = project_history(history)
    errors.extend(history_errors)
    require(errors, state == expected_state, "state projection is stale")
    return errors


def emit_validation(errors: list[str]) -> int:
    if errors:
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("valid")
    return 0


def blank_snapshot() -> dict[str, Any]:
    return {
        "revision": "",
        "scope": [],
        "fingerprint": "",
        "exclusions": [],
        "additional_inputs": [],
    }


def blank_validation() -> dict[str, list[str]]:
    return {"performed": [], "unavailable": [], "remaining_gaps": []}


def blank_thread(thread_id: str = "T1") -> dict[str, str]:
    return {
        "id": thread_id,
        "priority": "P1",
        "title": "",
        "risk": "",
        "evidence": "",
        "required_behavior": "",
    }


def new_document(review_id: str, name: str) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "review_id": review_id,
        "name": name,
        "state": default_state(),
        "history": [],
    }


def event_template(kind: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    base: dict[str, Any] = {
        "kind": kind,
        "status": STATUS_BY_KIND[kind],
        "role": ROLE_BY_KIND[kind],
    }
    time_field = TIME_FIELD_BY_KIND[kind]
    base[time_field] = now

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
                "decision": "",
                "validation": blank_validation(),
            }
        )
    else:
        base.update({"reason": "", "started_at": "", "deadline": ""})
    return base


def markdown_list(values: Any, empty: str = "None") -> list[str]:
    if not isinstance(values, list) or not values:
        return [f"- {empty}"]
    return [f"- {value}" for value in values]


def render_threads(threads: Any) -> list[str]:
    lines: list[str] = []
    for thread in threads if isinstance(threads, list) else []:
        lines.extend(
            [
                f"### {thread['id']} [{thread['priority']}]: {thread['title']}",
                "",
                thread["risk"],
                "",
                f"Evidence: {thread['evidence']}",
                "",
                f"Required behavior: {thread['required_behavior']}",
                "",
            ]
        )
    return lines


def render_thread_actions(actions: Any, action_key: str) -> list[str]:
    if not isinstance(actions, list) or not actions:
        return ["- None", ""]
    lines: list[str] = []
    for action in actions:
        lines.extend(
            [
                f"### {action['thread_id']}: {action[action_key]}",
                "",
                action["message"],
                "",
            ]
        )
    return lines


def render_report(document: dict[str, Any]) -> str:
    history = document.get("history", [])
    if not history:
        return (
            "# Latest Review Report\n\n"
            f"- Review ID: {document['review_id']}\n"
            f"- Review name: {document['name']}\n"
            "- Status: AWAITING REVIEW\n"
            "- Open threads: None\n"
        )
    event = history[-1]
    kind = event["kind"]
    state = document["state"]
    lines = [
        "# Latest Review Report",
        "",
        f"- Review ID: {document['review_id']}",
        f"- Review name: {document['name']}",
        f"- Status: {event['status']}",
        f"- Role: {event['role']}",
        f"- Recorded at: {event_time(event)}",
        f"- Open threads: {', '.join(state['open_threads']) or 'None'}",
        f"- Resolved threads: {', '.join(state['resolved_threads']) or 'None'}",
    ]
    lines.append(
        "- Source fingerprint: "
        f"{state.get('current_source_fingerprint') or 'Unavailable'}"
    )

    if kind == "review":
        lines.extend(
            ["", "## Opened Threads", "", *render_threads(event.get("threads"))]
        )
    elif kind == "source_update":
        lines.extend(["", "## Source Update", "", event.get("reason", ""), ""])
        lines.extend(
            [
                "## Thread Impacts",
                "",
                *render_thread_actions(event.get("thread_impacts"), "action"),
            ]
        )
        if event.get("new_threads"):
            lines.extend(
                [
                    "## Newly Opened Threads",
                    "",
                    *render_threads(event.get("new_threads")),
                ]
            )
    elif kind == "owner_reply":
        lines.extend(
            [
                "",
                "## Source State",
                "",
                f"- Starting fingerprint: {event['starting_source_snapshot']['fingerprint']}",
                f"- Completed fingerprint: {event['completed_source_snapshot']['fingerprint']}",
                f"- Drift assessment: {event.get('source_drift_assessment', '')}",
                "",
                "## Owner Replies",
                "",
                *render_thread_actions(event.get("replies"), "decision"),
                "## Files Changed",
                "",
                *markdown_list(event.get("files_changed")),
                "",
                "## Guide Synchronization",
                "",
                event.get("guide_synchronization", ""),
                "",
                "## Resulting Revisions",
                "",
                *markdown_list(event.get("commits")),
                "",
            ]
        )
    elif kind == "reviewer_update":
        lines.extend(
            [
                "",
                "## Reviewer Decisions",
                "",
                *render_thread_actions(event.get("decisions"), "action"),
            ]
        )
        if event.get("new_threads"):
            lines.extend(
                [
                    "## Newly Opened Threads",
                    "",
                    *render_threads(event.get("new_threads")),
                ]
            )
    elif kind == "final_review":
        lines.extend(
            [
                "",
                "## Decision",
                "",
                event.get("decision", ""),
                "",
                "## Resolutions",
                "",
                *render_thread_actions(
                    [
                        {**resolution, "action": "resolved"}
                        for resolution in event.get("resolutions", [])
                    ],
                    "action",
                ),
            ]
        )
    else:
        lines.extend(
            [
                "",
                "## Timeout",
                "",
                f"- Started at: {event.get('started_at', '')}",
                f"- Deadline: {event.get('deadline', '')}",
                f"- Timed out at: {event.get('timed_out_at', '')}",
                "",
                event.get("reason", ""),
                "",
            ]
        )

    validation = event.get("validation")
    if isinstance(validation, dict):
        if lines and lines[-1]:
            lines.append("")
        lines.extend(
            ["## Validation", "", *markdown_list(validation.get("performed")), ""]
        )
        lines.extend(
            [
                "## Unavailable Checks",
                "",
                *markdown_list(validation.get("unavailable")),
                "",
            ]
        )
        lines.extend(
            [
                "## Remaining Gaps",
                "",
                *markdown_list(validation.get("remaining_gaps")),
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def append_event(document: Any, event: Any) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise ValueError("review document must be a JSON object")
    history = document.get("history")
    if not isinstance(history, list) or not isinstance(document.get("state"), dict):
        raise ValueError("review document has invalid history or state")
    if not isinstance(event, dict):
        raise ValueError("event must be a JSON object")

    next_history = [*history, event]
    errors, state, _ = project_history(next_history)
    if errors:
        raise ValueError("; ".join(errors))
    document["history"] = next_history
    document["state"] = state
    return document


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("--review-id")
    subparsers.add_parser("validate-event")
    subparsers.add_parser("report")
    subparsers.add_parser("state")
    init_parser = subparsers.add_parser("init")
    init_parser.add_argument("review_id")
    init_parser.add_argument("name")
    snapshot_parser = subparsers.add_parser("source-snapshot")
    snapshot_parser.add_argument("--kind")
    append_parser = subparsers.add_parser("append-event")
    append_parser.add_argument("event_json")
    template_parser = subparsers.add_parser("template")
    template_parser.add_argument("kind", choices=STATUS_BY_KIND)
    args = parser.parse_args()

    if args.command == "template":
        print(json.dumps(event_template(args.kind), indent=2))
        return 0
    if args.command == "init":
        document = new_document(args.review_id, args.name)
        errors = validate_document(document)
        if errors:
            return emit_validation(errors)
        print(json.dumps(document, ensure_ascii=True, indent=2))
        return 0

    try:
        value = load_json()
    except (ValueError, json.JSONDecodeError) as error:
        print(f"invalid JSON: {error}", file=sys.stderr)
        return 1
    if args.command == "validate":
        errors = validate_document(value)
        if (
            args.review_id
            and isinstance(value, dict)
            and value.get("review_id") != args.review_id
        ):
            errors.append("review_id does not match the selected review")
        return emit_validation(errors)
    if args.command == "validate-event":
        return emit_validation(validate_event(value))
    if args.command == "source-snapshot":
        kind = args.kind or value.get("kind")
        field = SOURCE_FIELD_BY_KIND.get(kind)
        print(json.dumps(value.get(field)) if field else "null")
        return 0
    if args.command == "report":
        print(render_report(value), end="")
        return 0
    if args.command == "state":
        if not isinstance(value, dict) or not isinstance(value.get("state"), dict):
            print("review document has invalid state", file=sys.stderr)
            return 1
        print(json.dumps(value["state"], indent=2))
        return 0
    if args.command == "append-event":
        try:
            event = json.loads(
                Path(args.event_json).read_text(),
                object_pairs_hook=reject_duplicate_keys,
            )
            updated = append_event(value, event)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            print(f"cannot append review event: {error}", file=sys.stderr)
            return 1
        print(json.dumps(updated, ensure_ascii=True, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
