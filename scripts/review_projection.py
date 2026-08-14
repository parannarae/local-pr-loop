"""Project immutable review history into canonical workflow state."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from review_schema import (
    ACTOR_BY_KIND,
    FORMAT,
    FORMAT_REVISION,
    GAP_ID_PATTERN,
    REVIEW_ID_PATTERN,
    REVIEW_NAME_PATTERN,
    TERMINAL_OUTCOME_BY_KIND,
    THREAD_ID_PATTERN,
    parse_timestamp,
    reject_unknown,
    require,
    snapshot_identity,
    snapshot_scope_basis,
    validate_event,
)

__all__ = [
    "default_state",
    "project_history",
    "validate_document",
    "workflow_for",
]


def workflow_for(kind: str | None, terminal: dict[str, Any] | None) -> dict[str, Any]:
    """Return the workflow routing derived from the latest event and outcome."""
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
            "allowed_events_by_actor": {
                "reviewer": ["review", "final_review"],
                "owner": ["initial_review_timeout"],
            },
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
    """Return the canonical state for an empty review history."""
    return {
        "workflow": workflow_for(None, None),
        "source_fingerprint": None,
        "threads": {"open": [], "resolved": []},
        "validation_gaps": {"open": [], "resolved": []},
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
    created_at: str | None = None,
) -> tuple[list[str], dict[str, Any], dict[str, dict[str, Any]]]:
    """Validate immutable history and derive canonical state and thread records.

    `created_at` is the document creation timestamp; it anchors the
    `initial_review_timeout` clock, the only handoff that starts before any
    event exists.
    """
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
    gaps: dict[str, dict[str, Any]] = {}
    next_gap = 1

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
        validation = event.get("validation")
        event_gaps = validation.get("gaps") if isinstance(validation, dict) else []
        if isinstance(event_gaps, list):
            for gap_index, gap in enumerate(event_gaps):
                if not isinstance(gap, dict):
                    continue
                gap_id = gap.get("gap_id")
                expected = f"G{next_gap}"
                require(
                    errors,
                    gap_id == expected,
                    f"{prefix}.validation.gaps[{gap_index}].gap_id must be {expected}",
                )
                if isinstance(gap_id, str) and GAP_ID_PATTERN.fullmatch(gap_id):
                    require(
                        errors,
                        gap_id not in gaps,
                        f"{prefix}.validation.gaps[{gap_index}].gap_id is duplicated",
                    )
                    gaps[gap_id] = {**gap, "status": "open"}
                    next_gap += 1
        if kind in {"reviewer_update", "final_review"}:
            resolutions = event.get("gap_resolutions", [])
            resolution_ids = [
                item.get("gap_id") for item in resolutions if isinstance(item, dict)
            ]
            require(
                errors,
                len(resolution_ids) == len(set(resolution_ids)),
                f"{prefix}: gap resolution IDs must be unique",
            )
            for gap_id in resolution_ids:
                require(
                    errors,
                    gap_id in gaps and gaps[gap_id]["status"] == "open",
                    f"{prefix}: gap resolution references a non-open gap",
                )
                if gap_id in gaps:
                    gaps[gap_id]["status"] = "resolved"
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
                failed_checks = [
                    item
                    for item in event.get("validation", {}).get("performed", [])
                    if isinstance(item, dict) and item.get("result") == "failed"
                ]
                require(
                    errors, not failed_checks, f"{prefix}: LGTM forbids failed checks"
                )
                open_material_gaps = {
                    gap_id
                    for gap_id, gap in gaps.items()
                    if gap["status"] == "open" and gap.get("material") is True
                }
                require(
                    errors,
                    not open_material_gaps,
                    f"{prefix}: LGTM forbids unresolved material gaps",
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
            expected_start = (
                created_at if kind == "initial_review_timeout" else handoff_started_at
            )
            require(
                errors,
                event.get("started_at") == expected_start,
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
        "validation_gaps": {
            "open": sorted(
                (gap_id for gap_id, gap in gaps.items() if gap["status"] == "open"),
                key=lambda value: int(GAP_ID_PATTERN.fullmatch(value).group(1)),
            ),
            "resolved": sorted(
                (gap_id for gap_id, gap in gaps.items() if gap["status"] == "resolved"),
                key=lambda value: int(GAP_ID_PATTERN.fullmatch(value).group(1)),
            ),
        },
        "latest_event": latest_event,
        "terminal": terminal,
    }
    return errors, state, threads


def validate_document(document: Any) -> list[str]:
    """Return document-envelope and stale-projection validation errors."""
    errors: list[str] = []
    require(errors, isinstance(document, dict), "document must be a mapping")
    if not isinstance(document, dict):
        return errors
    reject_unknown(
        errors,
        document,
        {
            "format",
            "format_revision",
            "created_by",
            "created_at",
            "review_id",
            "prior_review_id",
            "name",
            "state",
            "history",
        },
        "document",
    )
    parse_timestamp(errors, document.get("created_at"), "created_at")
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
    if isinstance(creator, dict):
        reject_unknown(errors, creator, {"version"}, "created_by")
    require(
        errors,
        isinstance(document.get("review_id"), str)
        and bool(REVIEW_ID_PATTERN.fullmatch(document["review_id"])),
        "review_id must be an eight-character review ID",
    )
    prior_review_id = document.get("prior_review_id")
    require(
        errors,
        prior_review_id is None
        or (
            isinstance(prior_review_id, str)
            and bool(REVIEW_ID_PATTERN.fullmatch(prior_review_id))
            and prior_review_id != document.get("review_id")
        ),
        "prior_review_id must be null or a different review ID",
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
        history_errors, expected, _ = project_history(
            history, document.get("created_at")
        )
        errors.extend(history_errors)
        require(errors, state == expected, "state projection is stale")
    return errors
