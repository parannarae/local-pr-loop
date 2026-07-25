#!/usr/bin/env python3
"""Validate and project local-pr-loop calendar-revision artifacts."""

from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

FORMAT = "local-pr-loop"
FORMAT_REVISION = "2026-07-25.1"
CREATOR_VERSION = "0.3.0"

ACTOR_BY_KIND = {
    "review": "reviewer",
    "source_update": "reviewer",
    "owner_reply": "owner",
    "reviewer_update": "reviewer",
    "final_review": "reviewer",
    "reviewer_timeout": "owner",
    "owner_timeout": "reviewer",
}
SOURCE_FIELD_BY_KIND = {
    "review": "source_snapshot",
    "source_update": "source_snapshot",
    "owner_reply": "completed_source_snapshot",
    "reviewer_update": "source_snapshot",
    "final_review": "source_snapshot",
}
TERMINAL_OUTCOME_BY_KIND = {
    "final_review": "lgtm",
    "reviewer_timeout": "reviewer_timeout",
    "owner_timeout": "owner_timeout",
}
TIMEOUT_DURATION_BY_KIND = {
    "reviewer_timeout": timedelta(minutes=30),
    "owner_timeout": timedelta(hours=2),
}
THREAD_PRIORITIES = {"P0", "P1", "P2", "P3"}
EVIDENCE_BASES = {
    "source_inspection",
    "test_result",
    "live_probe",
    "captured_fixture",
    "authoritative_contract",
}
EXTERNAL_EVIDENCE_BASES = {
    "live_probe",
    "captured_fixture",
    "authoritative_contract",
}
THREAD_ID_PATTERN = re.compile(r"^T([1-9][0-9]*)$")
EVENT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{11,63}$")
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
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def load_json() -> Any:
    return json.load(sys.stdin, object_pairs_hook=reject_duplicate_keys)


def require(errors: list[str], condition: bool, message: str) -> None:
    if not condition:
        errors.append(message)


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


def validate_string_list(
    errors: list[str], value: Any, prefix: str, *, unique: bool = False
) -> None:
    valid = isinstance(value, list) and all(
        isinstance(item, str) and bool(item) for item in value
    )
    valid = valid and (not unique or len(value) == len(set(value)))
    suffix = "unique non-empty strings" if unique else "non-empty strings"
    require(errors, valid, f"{prefix} must be a list of {suffix}")


def validate_snapshot(
    errors: list[str], snapshot: Any, prefix: str, *, fingerprint: bool = True
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
    validate_string_list(errors, snapshot.get("scope"), f"{prefix}.scope", unique=True)
    require(errors, bool(snapshot.get("scope")), f"{prefix}.scope must not be empty")
    validate_string_list(
        errors, snapshot.get("exclusions"), f"{prefix}.exclusions", unique=True
    )
    inputs = snapshot.get("additional_inputs")
    require(
        errors, isinstance(inputs, list), f"{prefix}.additional_inputs must be a list"
    )
    paths: list[str] = []
    if isinstance(inputs, list):
        for index, item in enumerate(inputs):
            item_prefix = f"{prefix}.additional_inputs[{index}]"
            require(errors, isinstance(item, dict), f"{item_prefix} must be a mapping")
            if not isinstance(item, dict):
                continue
            path = item.get("path")
            require(
                errors,
                isinstance(path, str) and bool(path),
                f"{item_prefix}.path must be a non-empty string",
            )
            if isinstance(path, str):
                paths.append(path)
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
    if fingerprint:
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
    if not isinstance(snapshot, dict):
        return None
    inputs = snapshot.get("additional_inputs")
    if not isinstance(inputs, list):
        return None
    return {
        "scope": snapshot.get("scope"),
        "exclusions": snapshot.get("exclusions"),
        "additional_input_paths": [
            item.get("path") for item in inputs if isinstance(item, dict)
        ],
    }


def validate_evidence(errors: list[str], value: Any, prefix: str) -> None:
    require(errors, isinstance(value, dict), f"{prefix} must be a mapping")
    if not isinstance(value, dict):
        return
    require(
        errors,
        value.get("basis") in EVIDENCE_BASES,
        f"{prefix}.basis must be a supported evidence basis",
    )
    for key in ("provenance", "sanitized_result"):
        require(
            errors,
            isinstance(value.get(key), str) and bool(value[key]),
            f"{prefix}.{key} must be a non-empty string",
        )
    parse_timestamp(errors, value.get("observed_at"), f"{prefix}.observed_at")
    digest = value.get("artifact_digest")
    require(
        errors,
        digest is None
        or (isinstance(digest, str) and bool(SHA256_PATTERN.fullmatch(digest))),
        f"{prefix}.artifact_digest must be lowercase SHA-256 when present",
    )


def validate_validation(errors: list[str], value: Any, prefix: str) -> None:
    require(errors, isinstance(value, dict), f"{prefix} must be a mapping")
    if not isinstance(value, dict):
        return
    performed = value.get("performed")
    gaps = value.get("gaps")
    require(errors, isinstance(performed, list), f"{prefix}.performed must be a list")
    if isinstance(performed, list):
        for index, check in enumerate(performed):
            item_prefix = f"{prefix}.performed[{index}]"
            require(errors, isinstance(check, dict), f"{item_prefix} must be a mapping")
            if not isinstance(check, dict):
                continue
            require(
                errors,
                isinstance(check.get("check"), str) and bool(check["check"]),
                f"{item_prefix}.check must be a non-empty string",
            )
            require(
                errors,
                check.get("result") in {"passed", "failed"},
                f"{item_prefix}.result must be passed or failed",
            )
            if "evidence" in check:
                validate_evidence(errors, check["evidence"], f"{item_prefix}.evidence")
    require(errors, isinstance(gaps, list), f"{prefix}.gaps must be a list")
    if isinstance(gaps, list):
        for index, gap in enumerate(gaps):
            item_prefix = f"{prefix}.gaps[{index}]"
            require(errors, isinstance(gap, dict), f"{item_prefix} must be a mapping")
            if not isinstance(gap, dict):
                continue
            for key in ("check", "reason"):
                require(
                    errors,
                    isinstance(gap.get(key), str) and bool(gap[key]),
                    f"{item_prefix}.{key} must be a non-empty string",
                )
            require(
                errors,
                type(gap.get("material")) is bool,
                f"{item_prefix}.material must be boolean",
            )


def validate_thread(errors: list[str], thread: Any, prefix: str) -> None:
    require(errors, isinstance(thread, dict), f"{prefix} must be a mapping")
    if not isinstance(thread, dict):
        return
    thread_id = thread.get("id")
    require(
        errors,
        isinstance(thread_id, str) and bool(THREAD_ID_PATTERN.fullmatch(thread_id)),
        f"{prefix}.id must match T<N>",
    )
    require(
        errors,
        thread.get("priority") in THREAD_PRIORITIES,
        f"{prefix}.priority must be one of {', '.join(sorted(THREAD_PRIORITIES))}",
    )
    require(
        errors,
        thread.get("contract") in {"internal", "external"},
        f"{prefix}.contract must be internal or external",
    )
    for key in ("title", "risk", "required_behavior"):
        require(
            errors,
            isinstance(thread.get(key), str) and bool(thread[key]),
            f"{prefix}.{key} must be a non-empty string",
        )
    validate_evidence(errors, thread.get("evidence"), f"{prefix}.evidence")
    evidence = thread.get("evidence")
    if (
        thread.get("priority") in {"P1", "P2"}
        and thread.get("contract") == "external"
        and isinstance(evidence, dict)
    ):
        require(
            errors,
            evidence.get("basis") in EXTERNAL_EVIDENCE_BASES,
            f"{prefix}: external-contract P1/P2 evidence must use "
            "live_probe, captured_fixture, or authoritative_contract",
        )


def validate_action(
    errors: list[str],
    action: Any,
    prefix: str,
    allowed: set[str],
    *,
    resolution: bool = False,
) -> None:
    require(errors, isinstance(action, dict), f"{prefix} must be a mapping")
    if not isinstance(action, dict):
        return
    thread_id = action.get("thread_id")
    require(
        errors,
        isinstance(thread_id, str) and bool(THREAD_ID_PATTERN.fullmatch(thread_id)),
        f"{prefix}.thread_id must match T<N>",
    )
    if "action" in action:
        require(
            errors,
            action.get("action") in allowed,
            f"{prefix}.action must be one of {', '.join(sorted(allowed))}",
        )
    require(
        errors,
        isinstance(action.get("message"), str) and bool(action["message"]),
        f"{prefix}.message must be a non-empty string",
    )
    if resolution and "verification" in action:
        verification = action["verification"]
        require(
            errors,
            isinstance(verification, dict),
            f"{prefix}.verification must be a mapping",
        )
        if isinstance(verification, dict):
            require(
                errors,
                verification.get("independent") is True,
                f"{prefix}.verification.independent must be true",
            )
            validate_evidence(
                errors, verification.get("evidence"), f"{prefix}.verification.evidence"
            )


def validate_event(event: Any) -> list[str]:
    errors: list[str] = []
    require(errors, isinstance(event, dict), "event must be a mapping")
    if not isinstance(event, dict):
        return errors
    kind = event.get("kind")
    require(errors, kind in ACTOR_BY_KIND, f"unsupported event kind: {kind}")
    if kind not in ACTOR_BY_KIND:
        return errors
    require(
        errors,
        isinstance(event.get("event_id"), str)
        and bool(EVENT_ID_PATTERN.fullmatch(event["event_id"])),
        "event_id must be 12-64 lowercase identifier characters",
    )
    parse_timestamp(errors, event.get("occurred_at"), "occurred_at")
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
                validate_action(
                    errors,
                    impact,
                    f"thread_impacts[{index}]",
                    {"comment", "reopen"},
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
        for key in ("source_drift_assessment", "guide_synchronization"):
            require(
                errors,
                isinstance(event.get(key), str) and bool(event[key]),
                f"{key} must be a non-empty string",
            )
        replies = event.get("replies")
        require(
            errors,
            isinstance(replies, list) and bool(replies),
            "owner_reply must have replies",
        )
        if isinstance(replies, list):
            for index, reply in enumerate(replies):
                prefix = f"replies[{index}]"
                validate_action(errors, reply, prefix, set())
                require(
                    errors,
                    reply.get("decision")
                    in {"applied", "declined", "deferred/blocked"},
                    f"{prefix}.decision is invalid",
                )
                validate_evidence(errors, reply.get("evidence"), f"{prefix}.evidence")
                if reply.get("decision") == "deferred/blocked":
                    for key in (
                        "blocker",
                        "completed_work",
                        "remaining_work",
                        "validation_gap",
                    ):
                        require(
                            errors,
                            isinstance(reply.get(key), str) and bool(reply[key]),
                            f"{prefix}.{key} must be a non-empty string",
                        )
        validate_string_list(
            errors, event.get("files_changed"), "files_changed", unique=True
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
                validate_action(
                    errors,
                    decision,
                    f"decisions[{index}]",
                    {"comment", "reopen", "resolve"},
                    resolution=decision.get("action") == "resolve"
                    if isinstance(decision, dict)
                    else False,
                )
        new_threads = event.get("new_threads")
        require(errors, isinstance(new_threads, list), "new_threads must be a list")
        if isinstance(new_threads, list):
            for index, thread in enumerate(new_threads):
                validate_thread(errors, thread, f"new_threads[{index}]")
        validate_validation(errors, event.get("validation"), "validation")
    elif kind == "final_review":
        resolutions = event.get("resolutions")
        require(errors, isinstance(resolutions, list), "resolutions must be a list")
        if isinstance(resolutions, list):
            for index, resolution in enumerate(resolutions):
                validate_action(
                    errors,
                    resolution,
                    f"resolutions[{index}]",
                    set(),
                    resolution=True,
                )
        require(
            errors,
            isinstance(event.get("decision"), str) and bool(event["decision"]),
            "decision must be a non-empty string",
        )
        validate_validation(errors, event.get("validation"), "validation")
    else:
        require(
            errors,
            isinstance(event.get("reason"), str) and bool(event["reason"]),
            f"{kind}.reason must be a non-empty string",
        )
        started = parse_timestamp(errors, event.get("started_at"), "started_at")
        deadline = parse_timestamp(errors, event.get("deadline"), "deadline")
        occurred = parse_timestamp(errors, event.get("occurred_at"), "occurred_at")
        if started and deadline:
            require(
                errors,
                deadline == started + TIMEOUT_DURATION_BY_KIND[kind],
                f"{kind}.deadline has the wrong duration",
            )
        if occurred and deadline:
            require(
                errors, occurred >= deadline, f"{kind} occurred before its deadline"
            )
    return errors


def workflow_for(kind: str | None, terminal: dict[str, Any] | None) -> dict[str, Any]:
    if terminal:
        return {
            "phase": "terminal",
            "primary_actor": None,
            "primary_action": None,
            "allowed_events_by_actor": {},
        }
    if kind is None:
        return {
            "phase": "awaiting_initial_review",
            "primary_actor": "reviewer",
            "primary_action": {"kind": "publish_initial_review"},
            "allowed_events_by_actor": {"reviewer": ["review", "final_review"]},
        }
    if kind in {"review", "source_update", "reviewer_update"}:
        return {
            "phase": "owner_response",
            "primary_actor": "owner",
            "primary_action": {"kind": "reply_to_open_threads"},
            "allowed_events_by_actor": {
                "owner": ["owner_reply"],
                "reviewer": ["source_update", "owner_timeout"],
            },
        }
    return {
        "phase": "reviewer_verification",
        "primary_actor": "reviewer",
        "primary_action": {"kind": "verify_owner_reply"},
        "allowed_events_by_actor": {
            "reviewer": ["reviewer_update", "final_review", "source_update"],
            "owner": ["reviewer_timeout"],
        },
    }


def default_state() -> dict[str, Any]:
    return {
        "workflow": workflow_for(None, None),
        "source_fingerprint": None,
        "threads": {"open": [], "resolved": []},
        "latest_event": None,
        "terminal": None,
    }


def sorted_thread_ids(values: set[str]) -> list[str]:
    return sorted(
        values, key=lambda value: int(THREAD_ID_PATTERN.fullmatch(value).group(1))
    )


def material_gaps(event: dict[str, Any]) -> bool:
    validation = event.get("validation")
    gaps = validation.get("gaps") if isinstance(validation, dict) else None
    return isinstance(gaps, list) and any(
        isinstance(gap, dict) and gap.get("material") is True for gap in gaps
    )


def project_history(
    history: list[Any],
) -> tuple[list[str], dict[str, Any], dict[str, dict[str, Any]]]:
    errors: list[str] = []
    threads: dict[str, dict[str, Any]] = {}
    current_snapshot: dict[str, Any] | None = None
    latest_reply: dict[str, dict[str, Any]] = {}
    next_thread = 1
    previous_time: datetime | None = None
    handoff_started_at: str | None = None
    terminal: dict[str, Any] | None = None
    latest_kind: str | None = None
    latest_event: dict[str, Any] | None = None
    event_ids: set[str] = set()

    def add_threads(items: Any, prefix: str) -> None:
        nonlocal next_thread
        if not isinstance(items, list):
            return
        for index, thread in enumerate(items):
            if not isinstance(thread, dict):
                continue
            expected = f"T{next_thread}"
            thread_id = thread.get("id")
            require(
                errors,
                thread_id == expected,
                f"{prefix}[{index}].id must be {expected}",
            )
            if isinstance(thread_id, str) and THREAD_ID_PATTERN.fullmatch(thread_id):
                require(
                    errors,
                    thread_id not in threads,
                    f"{prefix}[{index}].id is duplicated",
                )
                threads[thread_id] = {**thread, "status": "open"}
                next_thread += 1

    for index, event in enumerate(history):
        prefix = f"history[{index}]"
        errors.extend(f"{prefix}: {error}" for error in validate_event(event))
        if not isinstance(event, dict) or event.get("kind") not in ACTOR_BY_KIND:
            continue
        kind = event["kind"]
        event_id = event.get("event_id")
        if isinstance(event_id, str):
            require(
                errors, event_id not in event_ids, f"{prefix}: event_id is duplicated"
            )
            event_ids.add(event_id)
        occurred = parse_timestamp(
            errors, event.get("occurred_at"), f"{prefix}.occurred_at"
        )
        if occurred:
            require(
                errors,
                occurred <= datetime.now(timezone.utc) + timedelta(minutes=5),
                f"{prefix}: timestamp is unreasonably in the future",
            )
            if previous_time:
                require(
                    errors,
                    occurred > previous_time,
                    f"{prefix}: occurred_at must increase",
                )
            previous_time = occurred
        require(errors, terminal is None, f"{prefix}: event follows a terminal event")
        workflow = workflow_for(latest_kind, terminal)
        allowed = workflow["allowed_events_by_actor"].get(ACTOR_BY_KIND[kind], [])
        require(
            errors,
            kind in allowed,
            f"{prefix}: {kind} is not allowed in {workflow['phase']}",
        )

        open_ids = {key for key, value in threads.items() if value["status"] == "open"}
        resolved_ids = set(threads) - open_ids
        if kind == "review":
            add_threads(event.get("threads"), f"{prefix}.threads")
            current_snapshot = event.get("source_snapshot")
            handoff_started_at = event.get("occurred_at")
        elif kind == "source_update":
            seen: set[str] = set()
            for action_index, action in enumerate(event.get("thread_impacts", [])):
                if not isinstance(action, dict):
                    continue
                action_prefix = f"{prefix}.thread_impacts[{action_index}]"
                thread_id = action.get("thread_id")
                require(
                    errors,
                    thread_id in threads,
                    f"{action_prefix} references unknown thread",
                )
                require(
                    errors,
                    thread_id not in seen,
                    f"{action_prefix} duplicates a thread",
                )
                if not isinstance(thread_id, str) or thread_id not in threads:
                    continue
                seen.add(thread_id)
                if action.get("action") == "reopen":
                    require(
                        errors,
                        thread_id in resolved_ids,
                        f"{action_prefix} can reopen only resolved",
                    )
                    threads[thread_id]["status"] = "open"
                else:
                    require(
                        errors,
                        thread_id in open_ids,
                        f"{action_prefix} can comment only open",
                    )
            add_threads(event.get("new_threads"), f"{prefix}.new_threads")
            current_snapshot = event.get("source_snapshot")
            handoff_started_at = event.get("occurred_at")
        elif kind == "owner_reply":
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
                f"{prefix}: completed snapshot changes guarded source basis; use source_update",
            )
            replies = event.get("replies", [])
            ids = [
                reply.get("thread_id") for reply in replies if isinstance(reply, dict)
            ]
            require(
                errors, len(ids) == len(set(ids)), f"{prefix}: reply IDs must be unique"
            )
            require(
                errors,
                set(ids) == open_ids,
                f"{prefix}: replies must address every open thread",
            )
            latest_reply = {
                reply["thread_id"]: reply
                for reply in replies
                if isinstance(reply, dict) and isinstance(reply.get("thread_id"), str)
            }
            current_snapshot = event.get("completed_source_snapshot")
            handoff_started_at = event.get("occurred_at")
        elif kind in {"reviewer_update", "final_review"}:
            if latest_kind is None and kind == "final_review":
                current_snapshot = event.get("source_snapshot")
            else:
                require(
                    errors,
                    snapshot_identity(event.get("source_snapshot"))
                    == snapshot_identity(current_snapshot),
                    f"{prefix}: reviewer snapshot does not match current source",
                )
            actions = event.get(
                "decisions" if kind == "reviewer_update" else "resolutions", []
            )
            ids = [
                action.get("thread_id")
                for action in actions
                if isinstance(action, dict)
            ]
            require(
                errors,
                len(ids) == len(set(ids)),
                f"{prefix}: thread IDs must be unique",
            )
            addressed = {
                action.get("thread_id")
                for action in actions
                if isinstance(action, dict)
                and action.get("thread_id") in open_ids
                and (
                    kind == "final_review"
                    or action.get("action") in {"comment", "resolve"}
                )
            }
            require(
                errors,
                addressed == open_ids,
                f"{prefix}: must address every open thread",
            )
            for action_index, action in enumerate(actions):
                if not isinstance(action, dict):
                    continue
                thread_id = action.get("thread_id")
                action_prefix = f"{prefix}.{('decisions' if kind == 'reviewer_update' else 'resolutions')}[{action_index}]"
                require(
                    errors,
                    thread_id in threads,
                    f"{action_prefix} references unknown thread",
                )
                if thread_id not in threads:
                    continue
                resolving = kind == "final_review" or action.get("action") == "resolve"
                if resolving:
                    prior = latest_reply.get(thread_id)
                    if isinstance(prior, dict) and prior.get("decision") == "declined":
                        require(
                            errors,
                            isinstance(action.get("verification"), dict)
                            and action["verification"].get("independent") is True,
                            f"{action_prefix}: declined thread requires independent verification",
                        )
                    threads[thread_id]["status"] = "resolved"
                elif action.get("action") == "reopen":
                    require(
                        errors,
                        thread_id in resolved_ids,
                        f"{action_prefix} can reopen only resolved",
                    )
                    threads[thread_id]["status"] = "open"
            if kind == "reviewer_update":
                add_threads(event.get("new_threads"), f"{prefix}.new_threads")
                require(
                    errors,
                    any(value["status"] == "open" for value in threads.values()),
                    f"{prefix}: use final_review when every thread is resolved",
                )
                handoff_started_at = event.get("occurred_at")
            else:
                require(
                    errors,
                    not material_gaps(event),
                    f"{prefix}: LGTM forbids material gaps",
                )
                require(
                    errors,
                    not any(value["status"] == "open" for value in threads.values()),
                    f"{prefix}: final_review leaves open threads",
                )
                terminal = {
                    "outcome": TERMINAL_OUTCOME_BY_KIND[kind],
                    "occurred_at": event.get("occurred_at"),
                }
        else:
            require(
                errors,
                event.get("started_at") == handoff_started_at,
                f"{prefix}: timeout start does not match active handoff",
            )
            terminal = {
                "outcome": TERMINAL_OUTCOME_BY_KIND[kind],
                "occurred_at": event.get("occurred_at"),
            }
        latest_kind = kind
        latest_event = {
            "event_id": event.get("event_id"),
            "kind": kind,
            "occurred_at": event.get("occurred_at"),
        }

    open_ids = {key for key, value in threads.items() if value["status"] == "open"}
    fingerprint = (
        current_snapshot.get("fingerprint")
        if isinstance(current_snapshot, dict)
        else None
    )
    state = {
        "workflow": workflow_for(latest_kind, terminal),
        "source_fingerprint": fingerprint,
        "threads": {
            "open": sorted_thread_ids(open_ids),
            "resolved": sorted_thread_ids(set(threads) - open_ids),
        },
        "latest_event": latest_event,
        "terminal": terminal,
    }
    return errors, state, threads


def validate_document(document: Any) -> list[str]:
    errors: list[str] = []
    require(errors, isinstance(document, dict), "document must be a mapping")
    if not isinstance(document, dict):
        return errors
    revision = document.get("format_revision")
    require(errors, document.get("format") == FORMAT, f"format must be {FORMAT}")
    require(
        errors,
        revision == FORMAT_REVISION,
        f"unsupported format_revision {revision!r}; current revision is "
        f"{FORMAT_REVISION}; preserve this artifact and start a new loop",
    )
    creator = document.get("created_by")
    require(
        errors,
        isinstance(creator, dict)
        and isinstance(creator.get("version"), str)
        and bool(creator["version"]),
        "created_by.version is required",
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
    history = document.get("history")
    state = document.get("state")
    require(errors, isinstance(history, list), "history must be a list")
    require(errors, isinstance(state, dict), "state must be a mapping")
    if isinstance(history, list) and isinstance(state, dict):
        history_errors, expected, _ = project_history(history)
        errors.extend(history_errors)
        require(errors, state == expected, "state projection is stale")
    return errors


def blank_snapshot() -> dict[str, Any]:
    return {
        "revision": "",
        "scope": [],
        "fingerprint": "",
        "exclusions": [],
        "additional_inputs": [],
    }


def blank_evidence() -> dict[str, Any]:
    return {
        "basis": "source_inspection",
        "provenance": "",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "sanitized_result": "",
    }


def blank_validation() -> dict[str, list[Any]]:
    return {"performed": [], "gaps": []}


def blank_thread() -> dict[str, Any]:
    return {
        "id": "T1",
        "priority": "P1",
        "contract": "internal",
        "title": "",
        "risk": "",
        "evidence": blank_evidence(),
        "required_behavior": "",
    }


def new_document(review_id: str, name: str) -> dict[str, Any]:
    return {
        "format": FORMAT,
        "format_revision": FORMAT_REVISION,
        "created_by": {"version": CREATOR_VERSION},
        "review_id": review_id,
        "name": name,
        "state": default_state(),
        "history": [],
    }


def event_template(kind: str) -> dict[str, Any]:
    base: dict[str, Any] = {
        "event_id": f"evt_{secrets.token_hex(12)}",
        "kind": kind,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
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
                "decision": "",
                "validation": blank_validation(),
            }
        )
    else:
        base.update({"reason": "", "started_at": "", "deadline": ""})
    return base


def append_event(document: Any, event: Any) -> dict[str, Any]:
    if not isinstance(document, dict) or not isinstance(document.get("history"), list):
        raise TypeError("review document has invalid history")
    if not isinstance(event, dict):
        raise TypeError("event must be a JSON object")
    next_history = [*document["history"], event]
    errors, state, _ = project_history(next_history)
    if errors:
        raise ValueError("; ".join(errors))
    document["history"] = next_history
    document["state"] = state
    return document


def evidence_summary(value: Any) -> str:
    if not isinstance(value, dict):
        return "Unavailable"
    return f"{value.get('basis', 'unknown')}: {value.get('sanitized_result', '')}"


def render_report(document: dict[str, Any]) -> str:
    state = document["state"]
    workflow = state["workflow"]
    lines = [
        "# Latest Review Report",
        "",
        f"- Review ID: {document['review_id']}",
        f"- Review name: {document['name']}",
        f"- Workflow phase: {workflow['phase']}",
        f"- Primary actor: {workflow['primary_actor'] or 'None'}",
        f"- Primary action: {(workflow['primary_action'] or {}).get('kind', 'None')}",
        f"- Open threads: {', '.join(state['threads']['open']) or 'None'}",
        f"- Resolved threads: {', '.join(state['threads']['resolved']) or 'None'}",
        f"- Source fingerprint: {state['source_fingerprint'] or 'Unavailable'}",
    ]
    if not document["history"]:
        return "\n".join(lines) + "\n"
    event = document["history"][-1]
    lines.extend(
        [
            f"- Latest event: {event['kind']} ({event['event_id']})",
            f"- Recorded at: {event['occurred_at']}",
            "",
        ]
    )
    threads = event.get("threads", []) or event.get("new_threads", [])
    if threads:
        lines.extend(["## Threads", ""])
        for thread in threads:
            lines.extend(
                [
                    f"### {thread['id']} [{thread['priority']}]: {thread['title']}",
                    "",
                    thread["risk"],
                    "",
                    f"Evidence: {evidence_summary(thread['evidence'])}",
                    "",
                    f"Required behavior: {thread['required_behavior']}",
                    "",
                ]
            )
    actions = (
        event.get("replies")
        or event.get("decisions")
        or event.get("resolutions")
        or event.get("thread_impacts")
        or []
    )
    if actions:
        lines.extend(["## Thread Actions", ""])
        for action in actions:
            label = action.get("decision") or action.get("action") or "resolved"
            lines.extend(
                [f"### {action['thread_id']}: {label}", "", action["message"], ""]
            )
    validation = event.get("validation")
    if isinstance(validation, dict):
        lines.extend(["## Validation", ""])
        for check in validation.get("performed", []):
            lines.append(f"- {check.get('result')}: {check.get('check')}")
        if not validation.get("performed"):
            lines.append("- None")
        lines.extend(["", "## Validation Gaps", ""])
        for gap in validation.get("gaps", []):
            material = "material" if gap.get("material") else "non-material"
            lines.append(f"- [{material}] {gap.get('check')}: {gap.get('reason')}")
        if not validation.get("gaps"):
            lines.append("- None")
    if state["terminal"]:
        lines.extend(
            [
                "",
                "## Terminal Outcome",
                "",
                f"- Outcome: {state['terminal']['outcome']}",
                f"- Occurred at: {state['terminal']['occurred_at']}",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def emit_validation(errors: list[str]) -> int:
    if errors:
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("valid")
    return 0


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
    template_parser.add_argument("kind", choices=ACTOR_BY_KIND)
    args = parser.parse_args()
    if args.command == "template":
        print(json.dumps(event_template(args.kind), indent=2))
        return 0
    if args.command == "init":
        document = new_document(args.review_id, args.name)
        errors = validate_document(document)
        if errors:
            return emit_validation(errors)
        print(json.dumps(document, indent=2))
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
        kind = args.kind or (value.get("kind") if isinstance(value, dict) else None)
        field = SOURCE_FIELD_BY_KIND.get(kind)
        print(
            json.dumps(value.get(field))
            if field and isinstance(value, dict)
            else "null"
        )
        return 0
    if args.command == "report":
        print(render_report(value), end="")
        return 0
    if args.command == "state":
        print(json.dumps(value["state"], indent=2))
        return 0
    if args.command == "append-event":
        try:
            event = json.loads(
                Path(args.event_json).read_text(),
                object_pairs_hook=reject_duplicate_keys,
            )
            updated = append_event(value, event)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            print(f"cannot append review event: {error}", file=sys.stderr)
            return 1
        print(json.dumps(updated, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
