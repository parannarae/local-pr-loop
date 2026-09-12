"""Project immutable review history into canonical workflow state."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from review_schema import (
    ACTOR_BY_KIND,
    GAP_ID_PATTERN,
    REVIEW_ID_PATTERN,
    REVIEW_KINDS,
    REVIEW_NAME_PATTERN,
    STRUCTURE_POLICIES,
    TERMINAL_OUTCOME_BY_KIND,
    THREAD_ID_PATTERN,
    operations_of,
    parse_timestamp,
    reject_unknown,
    require,
    snapshot_identity,
    snapshot_scope_basis,
    unsupported_revision_error,
    validate_event,
)

COMPARISON_BASE_PATTERN = re.compile(r"^[0-9a-f]{40}$")

# The field naming the thread or gap each operation acts on.
OPERATION_IDENTIFIER_FIELD = {
    "thread.open": "id",
    "thread.reply": "thread_id",
    "thread.comment": "thread_id",
    "thread.resolve": "thread_id",
    "thread.reopen": "thread_id",
    "gap.open": "gap_id",
    "gap.resolve": "gap_id",
}

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
    # Validated IDs match T<N>, so the number is everything after the prefix.
    return sorted(values, key=lambda value: int(value[1:]))


def record_of(operation: dict[str, Any], status: str) -> dict[str, Any]:
    """Keep an opening operation's fields as a durable record, without its `op`."""

    return {
        **{key: value for key, value in operation.items() if key != "op"},
        "status": status,
    }


def act_on_thread(
    errors: list[str],
    threads: dict[str, dict[str, Any]],
    touched: set[str],
    thread_id: str,
    prefix: str,
) -> bool:
    """Record one lifecycle act on a known thread; report whether it stands.

    Acting twice on one thread in one transaction is rejected rather than
    resolved last-writer-wins, because the handoff would otherwise record two
    conflicting answers with no way to tell which the agent meant.
    """

    require(errors, thread_id in threads, f"{prefix} references unknown thread")
    if thread_id not in threads:
        return False
    require(
        errors,
        thread_id not in touched,
        f"{prefix} acts on {thread_id} twice in one transaction",
    )
    touched.add(thread_id)
    return True


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
        # Per-transaction bookkeeping. `touched` is every thread this transaction
        # opened or acted on: it bounds what a note may target and makes a second
        # act on one thread in one handoff a rejection rather than a silent
        # last-writer-wins.
        touched: set[str] = set()
        replied: set[str] = set()
        addressed: set[str] = set()
        transaction_replies: dict[str, dict[str, Any]] = {}
        resolved_gap_ids: set[str] = set()
        note_targets: list[tuple[str, str]] = []
        failed_checks = False

        for op_index, operation in enumerate(operations_of(event)):
            if not isinstance(operation, dict):
                continue
            name = operation.get("op")
            if not isinstance(name, str):
                continue
            op_prefix = f"{prefix}.operations[{op_index}]"
            # An identifier that is not a string is already an event-validation
            # error; skipping it here keeps set lookups from raising on it.
            identifier_field = OPERATION_IDENTIFIER_FIELD.get(name)
            if identifier_field is not None and not isinstance(
                operation.get(identifier_field), str
            ):
                continue
            if name == "thread.open":
                expected = f"T{next_thread}"
                thread_id = operation.get("id")
                require(
                    errors, thread_id == expected, f"{op_prefix}.id must be {expected}"
                )
                if THREAD_ID_PATTERN.fullmatch(thread_id):
                    require(
                        errors, thread_id not in threads, f"{op_prefix}.id is duplicated"
                    )
                    threads[thread_id] = record_of(operation, "open")
                    touched.add(thread_id)
                    next_thread += 1
            elif name == "gap.open":
                expected = f"G{next_gap}"
                gap_id = operation.get("gap_id")
                require(
                    errors,
                    gap_id == expected,
                    f"{op_prefix}.gap_id must be {expected}",
                )
                if GAP_ID_PATTERN.fullmatch(gap_id):
                    require(
                        errors, gap_id not in gaps, f"{op_prefix}.gap_id is duplicated"
                    )
                    gaps[gap_id] = record_of(operation, "open")
                    next_gap += 1
            elif name == "gap.resolve":
                gap_id = operation.get("gap_id")
                require(
                    errors,
                    gap_id not in resolved_gap_ids,
                    f"{op_prefix} resolves {gap_id} twice in one transaction",
                )
                require(
                    errors,
                    gap_id in gaps and gaps[gap_id]["status"] == "open",
                    f"{op_prefix} references a non-open gap",
                )
                resolved_gap_ids.add(gap_id)
                if gap_id in gaps:
                    gaps[gap_id]["status"] = "resolved"
            elif name == "thread.reply":
                thread_id = operation.get("thread_id")
                if act_on_thread(errors, threads, touched, thread_id, op_prefix):
                    require(
                        errors,
                        thread_id in open_ids,
                        f"{op_prefix} can reply only to an open thread",
                    )
                    replied.add(thread_id)
                    transaction_replies[thread_id] = operation
            elif name == "thread.comment":
                thread_id = operation.get("thread_id")
                if act_on_thread(errors, threads, touched, thread_id, op_prefix):
                    require(
                        errors,
                        thread_id in open_ids,
                        f"{op_prefix} can comment only on an open thread",
                    )
                    addressed.add(thread_id)
            elif name == "thread.resolve":
                thread_id = operation.get("thread_id")
                if act_on_thread(errors, threads, touched, thread_id, op_prefix):
                    require(
                        errors,
                        thread_id in open_ids,
                        f"{op_prefix} can resolve only an open thread",
                    )
                    prior = latest_reply.get(thread_id)
                    if isinstance(prior, dict) and prior.get("decision") == "declined":
                        verification = operation.get("verification")
                        require(
                            errors,
                            isinstance(verification, dict)
                            and verification.get("independent") is True,
                            f"{op_prefix}: declined thread requires independent "
                            "verification",
                        )
                    threads[thread_id]["status"] = "resolved"
                    addressed.add(thread_id)
            elif name == "thread.reopen":
                thread_id = operation.get("thread_id")
                if act_on_thread(errors, threads, touched, thread_id, op_prefix):
                    require(
                        errors,
                        thread_id in resolved_ids,
                        f"{op_prefix} can reopen only a resolved thread",
                    )
                    threads[thread_id]["status"] = "open"
            elif name == "note.attach":
                target = operation.get("target")
                target_id = target.get("id") if isinstance(target, dict) else None
                note_targets.append(
                    (op_prefix, target_id if isinstance(target_id, str) else "")
                )
            elif name == "check.record" and operation.get("result") == "failed":
                failed_checks = True

        for note_prefix, target_id in note_targets:
            require(
                errors,
                target_id in touched,
                f"{note_prefix} targets a thread this transaction does not act on",
            )

        if kind in {"review", "source_update"}:
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
                f"{prefix}: completed snapshot changes guarded source basis; "
                "use source_update",
            )
            require(
                errors,
                replied == open_ids,
                f"{prefix}: replies must address every open thread",
            )
            latest_reply = transaction_replies
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
            require(
                errors,
                addressed == open_ids,
                f"{prefix}: must address every open thread",
            )
            if kind == "reviewer_update":
                require(
                    errors,
                    any(value["status"] == "open" for value in threads.values()),
                    f"{prefix}: use final_review when every thread is resolved",
                )
                handoff_started_at = event.get("occurred_at")
            else:
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
            declarations = [
                operation
                for operation in operations_of(event)
                if isinstance(operation, dict)
                and operation.get("op") == "timeout.declare"
            ]
            require(
                errors,
                bool(declarations)
                and declarations[0].get("started_at") == expected_start,
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
            # Validated IDs match G<N>, so the number is everything after the prefix.
            "open": sorted(
                (gap_id for gap_id, gap in gaps.items() if gap["status"] == "open"),
                key=lambda value: int(value[1:]),
            ),
            "resolved": sorted(
                (gap_id for gap_id, gap in gaps.items() if gap["status"] == "resolved"),
                key=lambda value: int(value[1:]),
            ),
        },
        "latest_event": latest_event,
        "terminal": terminal,
    }
    return errors, state, threads


def structure_debt_acknowledgments(history: Any) -> list[dict[str, Any]]:
    """Return the `structure_debt` payload of every recorded `review.approve`."""

    found: list[dict[str, Any]] = []
    if not isinstance(history, list):
        return found
    for event in history:
        for operation in operations_of(event):
            if (
                isinstance(operation, dict)
                and operation.get("op") == "review.approve"
                and operation.get("structure_debt") is not None
            ):
                found.append(operation["structure_debt"])
    return found


def validate_document(document: Any) -> list[str]:
    """Return document-envelope and stale-projection validation errors.

    The storage contract is checked first and alone: a document this reader
    cannot interpret in full is refused before any other field is read, so an
    unreadable revision can never reach a gate as an empty default.
    """
    boundary = unsupported_revision_error(document)
    if boundary is not None:
        return [boundary]
    errors: list[str] = []
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
            "review_kind",
            "structure_policy",
            "comparison_base",
            "state",
            "history",
        },
        "document",
    )
    require(
        errors,
        document.get("review_kind") in REVIEW_KINDS,
        "review_kind must be one of " + ", ".join(sorted(REVIEW_KINDS)),
    )
    require(
        errors,
        document.get("structure_policy") in STRUCTURE_POLICIES,
        "structure_policy must be one of " + ", ".join(sorted(STRUCTURE_POLICIES)),
    )
    comparison_base = document.get("comparison_base")
    require(
        errors,
        comparison_base is None
        or (
            isinstance(comparison_base, str)
            and bool(COMPARISON_BASE_PATTERN.fullmatch(comparison_base))
        ),
        "comparison_base must be null or a full lowercase commit SHA",
    )
    if document.get("review_kind") == "structure":
        require(
            errors,
            not structure_debt_acknowledgments(document.get("history")),
            "a structure round records no structure_debt; the acknowledgment belongs "
            "to the correctness loop that flagged the files",
        )
    parse_timestamp(errors, document.get("created_at"), "created_at")
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
