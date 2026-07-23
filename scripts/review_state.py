#!/usr/bin/env python3
"""Validate local review JSON and render its latest report."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

STATUS_BY_KIND = {
    "review": "REVIEW SUBMITTED",
    "review_correction": "REVIEW SUBMITTED",
    "owner_response": "PR COMPLETED",
    "final_review": "LGTM",
    "reviewer_timeout": "REVIEWER TIMED OUT",
    "owner_timeout": "OWNER TIMED OUT",
}
TERMINAL_KINDS = {"final_review", "reviewer_timeout", "owner_timeout"}
SOURCE_FIELD_BY_KIND = {
    "review": "source_snapshot",
    "review_correction": "source_snapshot",
    "owner_response": "completed_source_snapshot",
    "final_review": "source_snapshot",
}
TIME_FIELD_BY_KIND = {
    "review": "submitted_at",
    "review_correction": "submitted_at",
    "owner_response": "completed_at",
    "final_review": "completed_at",
    "reviewer_timeout": "timed_out_at",
    "owner_timeout": "timed_out_at",
}
TIMEOUT_DURATION_BY_KIND = {
    "reviewer_timeout": timedelta(minutes=30),
    "owner_timeout": timedelta(hours=2),
}
FINDING_ID_PATTERN = re.compile(r"^I([1-9][0-9]*)-F([1-9][0-9]*)$")
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


def event_time(event: dict[str, Any]) -> str:
    key = TIME_FIELD_BY_KIND.get(event.get("kind"))
    value = event.get(key) if key else None
    return value if isinstance(value, str) else ""


def require(errors: list[str], condition: bool, message: str) -> None:
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


def validate_finding(
    errors: list[str], finding: Any, prefix: str, iteration: int
) -> None:
    require(errors, isinstance(finding, dict), f"{prefix} must be a mapping")
    if not isinstance(finding, dict):
        return
    for key in ("id", "priority", "title", "risk", "evidence", "required_behavior"):
        require(
            errors,
            isinstance(finding.get(key), str) and bool(finding[key]),
            f"{prefix}.{key} must be a non-empty string",
        )
    finding_id = finding.get("id")
    match = FINDING_ID_PATTERN.fullmatch(finding_id) if isinstance(finding_id, str) else None
    require(
        errors,
        bool(match) and int(match.group(1)) == iteration,
        f"{prefix}.id must match I{iteration}-F<M>",
    )


def validate_snapshot(
    errors: list[str], snapshot: Any, prefix: str, require_fingerprint: bool = True
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
    validate_string_list(errors, snapshot.get("exclusions"), f"{prefix}.exclusions", unique=True)
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
            if isinstance(item, dict):
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


def validate_validation(errors: list[str], value: Any, prefix: str) -> None:
    require(errors, isinstance(value, dict), f"{prefix} must be a mapping")
    if not isinstance(value, dict):
        return
    for key in ("performed", "unavailable", "remaining_gaps"):
        validate_string_list(errors, value.get(key), f"{prefix}.{key}")


def event_findings(event: dict[str, Any]) -> list[dict[str, Any]]:
    findings = event.get("findings", [])
    return findings if isinstance(findings, list) else []


def validate_event(event: Any) -> list[str]:
    errors: list[str] = []
    require(errors, isinstance(event, dict), "event must be a mapping")
    if not isinstance(event, dict):
        return errors

    kind = event.get("kind")
    require(errors, kind in STATUS_BY_KIND, f"unsupported event kind: {kind}")
    if kind not in STATUS_BY_KIND:
        return errors
    require(errors, event.get("status") == STATUS_BY_KIND[kind], f"{kind} status is invalid")
    valid_iteration = type(event.get("iteration")) is int and event["iteration"] >= 0
    require(
        errors,
        valid_iteration,
        "event.iteration must be a non-negative integer",
    )
    expected_role = (
        "Owner"
        if kind in {"owner_response", "reviewer_timeout"}
        else "Reviewer"
    )
    require(errors, event.get("role") == expected_role, f"{kind} role must be {expected_role}")
    time_field = TIME_FIELD_BY_KIND[kind]
    event_timestamp = parse_timestamp(errors, event.get(time_field), time_field)
    if not valid_iteration:
        return errors
    if kind != "final_review":
        require(errors, event["iteration"] >= 1, f"{kind}.iteration must be at least 1")

    source_field = SOURCE_FIELD_BY_KIND.get(kind)
    if source_field:
        validate_snapshot(errors, event.get(source_field), source_field)

    if kind == "review":
        findings = event_findings(event)
        require(errors, bool(findings), "review must contain at least one finding")
        for index, finding in enumerate(findings):
            validate_finding(errors, finding, f"findings[{index}]", event["iteration"])
        validate_validation(errors, event.get("validation"), "validation")

    elif kind == "review_correction":
        require(
            errors,
            isinstance(event.get("affected_findings"), list) and bool(event["affected_findings"]),
            "review_correction.affected_findings must be a non-empty string list",
        )
        validate_string_list(
            errors,
            event.get("affected_findings"),
            "review_correction.affected_findings",
            unique=True,
        )
        affected_findings = event.get("affected_findings", [])
        if isinstance(affected_findings, list):
            require(
                errors,
                all(
                    isinstance(identifier, str)
                    and bool(
                        (match := FINDING_ID_PATTERN.fullmatch(identifier))
                        and int(match.group(1)) == event["iteration"]
                    )
                    for identifier in affected_findings
                ),
                f"review_correction.affected_findings must match I{event['iteration']}-F<M>",
            )
        require(
            errors,
            isinstance(event.get("source_drift"), str) and bool(event["source_drift"]),
            "review_correction.source_drift must be a non-empty string",
        )
        require(
            errors,
            isinstance(event.get("correction"), str) and bool(event["correction"]),
            "review_correction.correction must be a non-empty string",
        )
        for index, finding in enumerate(event_findings(event)):
            validate_finding(errors, finding, f"findings[{index}]", event["iteration"])

    elif kind == "owner_response":
        validate_snapshot(errors, event.get("starting_source_snapshot"), "starting_source_snapshot")
        require(
            errors,
            isinstance(event.get("source_drift_assessment"), str)
            and bool(event["source_drift_assessment"]),
            "source drift assessment must be a non-empty string",
        )
        dispositions = event.get("dispositions")
        require(errors, isinstance(dispositions, list), "dispositions must be a list")
        if isinstance(dispositions, list):
            disposition_ids = []
            for index, disposition in enumerate(dispositions):
                prefix = f"dispositions[{index}]"
                require(errors, isinstance(disposition, dict), f"{prefix} must be a mapping")
                if isinstance(disposition, dict):
                    disposition_id = disposition.get("id")
                    require(
                        errors,
                        isinstance(disposition_id, str) and bool(disposition_id),
                        f"{prefix}.id must be a non-empty string",
                    )
                    if isinstance(disposition_id, str):
                        disposition_ids.append(disposition_id)
                        match = FINDING_ID_PATTERN.fullmatch(disposition_id)
                        require(
                            errors,
                            bool(match) and int(match.group(1)) == event["iteration"],
                            f"{prefix}.id must match I{event['iteration']}-F<M>",
                        )
                    require(
                        errors,
                        disposition.get("decision") in {"applied", "declined", "deferred/blocked"},
                        f"{prefix}.decision is invalid",
                    )
                    require(
                        errors,
                        isinstance(disposition.get("rationale"), str)
                        and bool(disposition["rationale"]),
                        f"{prefix}.rationale must be a non-empty string",
                    )
                    if disposition.get("decision") == "deferred/blocked":
                        required_details = (
                            "blocker",
                            "completed_work",
                            "remaining_work",
                            "validation_gap",
                        )
                        for key in required_details:
                            require(
                                errors,
                                isinstance(disposition.get(key), str) and bool(disposition[key]),
                                f"{prefix}.{key} must be a non-empty string",
                            )
            require(
                errors,
                len(disposition_ids) == len(set(disposition_ids)),
                "disposition IDs must be unique",
            )
        validate_string_list(errors, event.get("files_changed"), "files_changed", unique=True)
        require(
            errors,
            isinstance(event.get("guide_synchronization"), str)
            and bool(event["guide_synchronization"]),
            "guide_synchronization must be a non-empty string",
        )
        validate_string_list(errors, event.get("commits"), "commits", unique=True)
        validate_validation(errors, event.get("validation"), "validation")

    elif kind == "final_review":
        reviewed_through = event.get("reviewed_through")
        require(
            errors,
            reviewed_through == "initial_review"
            or (type(reviewed_through) is int and reviewed_through >= 1),
            "reviewed_through must be initial_review or a positive integer",
        )
        validate_string_list(errors, event.get("resolutions"), "final_review.resolutions")
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
        started_at = parse_timestamp(errors, event.get("started_at"), f"{kind}.started_at")
        deadline = parse_timestamp(errors, event.get("deadline"), f"{kind}.deadline")
        if started_at and deadline:
            require(
                errors,
                deadline == started_at + TIMEOUT_DURATION_BY_KIND[kind],
                f"{kind}.deadline has the wrong duration",
            )
        if event_timestamp and deadline:
            require(errors, event_timestamp >= deadline, f"{kind} occurred before its deadline")

    return errors


def validate_document(document: Any) -> list[str]:
    errors: list[str] = []
    require(errors, isinstance(document, dict), "document must be a mapping")
    if not isinstance(document, dict):
        return errors
    require(
        errors,
        type(document.get("schema_version")) is int and document["schema_version"] == 1,
        "schema_version must be integer 1",
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

    if not history:
        require(errors, state.get("marker") == "AWAITING REVIEW", "empty history must await review")
        require(
            errors,
            type(state.get("latest_iteration")) is int
            and state["latest_iteration"] == 0,
            "empty history iteration must be integer 0",
        )
        require(
            errors,
            state.get("latest_event_kind") is None,
            "empty history event kind must be null",
        )
        require(errors, state.get("updated_at") is None, "empty history update time must be null")
        return errors

    review_findings: dict[int, list[str]] = {}
    review_source_snapshots: dict[int, dict[str, Any]] = {}
    responded: set[int] = set()
    review_iterations: set[int] = set()
    terminal_seen = False
    latest_response_iteration: int | None = None
    previous_timestamp: datetime | None = None

    for index, event in enumerate(history):
        prefix = f"history[{index}]"
        for error in validate_event(event):
            errors.append(f"{prefix}: {error}")
        if not isinstance(event, dict):
            continue
        kind = event.get("kind")
        iteration = event.get("iteration")
        if type(iteration) is not int:
            continue
        previous = history[index - 1] if index > 0 and isinstance(history[index - 1], dict) else {}
        current_timestamp = parse_timestamp(errors, event_time(event), f"{prefix}.timestamp")
        if current_timestamp:
            require(
                errors,
                current_timestamp <= datetime.now(timezone.utc) + timedelta(minutes=5),
                f"{prefix}: timestamp is unreasonably in the future",
            )
            if previous_timestamp:
                require(
                    errors,
                    current_timestamp >= previous_timestamp,
                    f"{prefix}: timestamp precedes the previous event",
                )
            previous_timestamp = current_timestamp
        require(errors, not terminal_seen, f"{prefix}: event follows a terminal event")
        if kind == "review":
            open_iterations = review_iterations - responded
            require(errors, not open_iterations, f"{prefix}: another review is still open")
            expected_iteration = max(review_iterations, default=0) + 1
            require(
                errors,
                iteration == expected_iteration,
                f"{prefix}: expected review iteration {expected_iteration}",
            )
            require(
                errors,
                iteration not in review_iterations,
                f"{prefix}: duplicate review iteration",
            )
            review_iterations.add(iteration)
            ids = [item.get("id") for item in event_findings(event) if isinstance(item, dict)]
            review_findings[iteration] = [item for item in ids if isinstance(item, str)]
            source_snapshot = event.get("source_snapshot")
            if isinstance(source_snapshot, dict):
                review_source_snapshots[iteration] = source_snapshot
        elif kind == "review_correction":
            require(errors, iteration in review_iterations, f"{prefix}: correction has no review")
            require(errors, iteration not in responded, f"{prefix}: correction follows response")
            additions = [item.get("id") for item in event_findings(event) if isinstance(item, dict)]
            affected = event.get("affected_findings", [])
            known_ids = set(review_findings.get(iteration, []))
            require(
                errors,
                isinstance(affected, list)
                and all(isinstance(item, str) for item in affected)
                and set(affected).issubset(known_ids),
                f"{prefix}: correction references an unknown finding",
            )
            review_findings.setdefault(iteration, []).extend(
                item for item in additions if isinstance(item, str)
            )
            source_snapshot = event.get("source_snapshot")
            if isinstance(source_snapshot, dict):
                review_source_snapshots[iteration] = source_snapshot
        elif kind == "owner_response":
            require(errors, iteration in review_iterations, f"{prefix}: response has no review")
            require(errors, iteration not in responded, f"{prefix}: duplicate response")
            require(
                errors,
                iteration == max(review_iterations, default=0),
                f"{prefix}: response does not match the latest review",
            )
            require(
                errors,
                snapshot_identity(event.get("starting_source_snapshot"))
                == snapshot_identity(review_source_snapshots.get(iteration)),
                f"{prefix}: starting snapshot does not match the latest review",
            )
            responded.add(iteration)
            latest_response_iteration = iteration
            dispositions = event.get("dispositions", [])
            ids = [
                item.get("id")
                for item in dispositions
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            ]
            require(
                errors,
                sorted(ids) == sorted(review_findings.get(iteration, [])),
                f"{prefix}: dispositions must match every finding exactly once",
            )
        elif kind == "final_review":
            require(errors, not (review_iterations - responded), f"{prefix}: review is still open")
            reviewed_through = event.get("reviewed_through")
            expected = max(review_iterations, default=0)
            expected_reviewed_through: int | str = (
                expected if expected else "initial_review"
            )
            require(
                errors,
                type(reviewed_through) is type(expected_reviewed_through)
                and reviewed_through == expected_reviewed_through,
                f"{prefix}: reviewed_through does not match completed history",
            )
        elif kind == "reviewer_timeout":
            require(
                errors,
                index > 0
                and previous.get("kind") == "owner_response"
                and previous.get("iteration") == iteration
                and latest_response_iteration == iteration,
                f"{prefix}: reviewer timeout must follow its completed response",
            )
            expected_start = parse_timestamp(
                errors, previous.get("completed_at"), f"{prefix}.expected_start"
            )
            actual_start = parse_timestamp(
                errors, event.get("started_at"), f"{prefix}.started_at"
            )
            if expected_start and actual_start:
                require(
                    errors,
                    actual_start == expected_start,
                    f"{prefix}: reviewer timeout start does not match the response",
                )
        elif kind == "owner_timeout":
            require(
                errors,
                index > 0
                and previous.get("kind") in {"review", "review_correction"}
                and previous.get("iteration") == iteration
                and iteration in (review_iterations - responded),
                f"{prefix}: owner timeout must follow its open review",
            )
            expected_start = parse_timestamp(
                errors, previous.get("submitted_at"), f"{prefix}.expected_start"
            )
            actual_start = parse_timestamp(
                errors, event.get("started_at"), f"{prefix}.started_at"
            )
            if expected_start and actual_start:
                require(
                    errors,
                    actual_start == expected_start,
                    f"{prefix}: owner timeout start does not match the submission",
                )
        if kind in TERMINAL_KINDS:
            terminal_seen = True

    all_finding_ids = [item for values in review_findings.values() for item in values]
    require(
        errors,
        len(all_finding_ids) == len(set(all_finding_ids)),
        "finding IDs must be globally unique",
    )
    for iteration, identifiers in review_findings.items():
        sequences = []
        for identifier in identifiers:
            match = FINDING_ID_PATTERN.fullmatch(identifier)
            if match and int(match.group(1)) == iteration:
                sequences.append(int(match.group(2)))
        require(
            errors,
            sorted(sequences) == list(range(1, len(identifiers) + 1)),
            f"iteration {iteration} finding IDs must be sequential",
        )
    latest = history[-1]
    if isinstance(latest, dict):
        require(errors, state.get("marker") == latest.get("status"), "state marker is stale")
        require(
            errors,
            type(state.get("latest_iteration")) is int
            and state["latest_iteration"] == latest.get("iteration"),
            "state latest_iteration is stale",
        )
        require(
            errors,
            state.get("latest_event_kind") == latest.get("kind"),
            "state latest_event_kind is stale",
        )
        require(errors, state.get("updated_at") == event_time(latest), "state updated_at is stale")
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


def new_document(review_id: str, name: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "review_id": review_id,
        "name": name,
        "state": {
            "marker": "AWAITING REVIEW",
            "latest_iteration": 0,
            "latest_event_kind": None,
            "updated_at": None,
        },
        "history": [],
    }


def event_template(kind: str, iteration: int) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    base: dict[str, Any] = {
        "kind": kind,
        "iteration": iteration,
        "status": STATUS_BY_KIND[kind],
        "role": "Owner" if kind in {"owner_response", "reviewer_timeout"} else "Reviewer",
    }
    if kind in {"review", "review_correction"}:
        base["submitted_at"] = now
    elif kind in {"owner_response", "final_review"}:
        base["completed_at"] = now
    else:
        base["timed_out_at"] = now

    if kind == "review":
        base.update(
            {
                "source_snapshot": blank_snapshot(),
                "findings": [
                    {
                        "id": f"I{iteration}-F1",
                        "priority": "P1",
                        "title": "",
                        "risk": "",
                        "evidence": "",
                        "required_behavior": "",
                    }
                ],
                "validation": {"performed": [], "unavailable": [], "remaining_gaps": []},
            }
        )
    elif kind == "review_correction":
        base.update(
            {
                "source_snapshot": blank_snapshot(),
                "source_drift": "",
                "correction": "",
                "affected_findings": [],
                "findings": [],
            }
        )
    elif kind == "owner_response":
        base.update(
            {
                "starting_source_snapshot": blank_snapshot(),
                "source_drift_assessment": "",
                "completed_source_snapshot": blank_snapshot(),
                "dispositions": [],
                "files_changed": [],
                "guide_synchronization": "",
                "validation": {"performed": [], "unavailable": [], "remaining_gaps": []},
                "commits": [],
            }
        )
    elif kind == "final_review":
        base.update(
            {
                "reviewed_through": iteration if iteration else "initial_review",
                "source_snapshot": blank_snapshot(),
                "resolutions": [],
                "decision": "",
                "validation": {"performed": [], "unavailable": [], "remaining_gaps": []},
            }
        )
    else:
        base.update({"reason": "", "started_at": "", "deadline": ""})
    return base


def markdown_list(values: Any, empty: str = "None") -> list[str]:
    if not isinstance(values, list) or not values:
        return [f"- {empty}"]
    return [f"- {value}" for value in values]


def render_findings(findings: Any) -> list[str]:
    lines: list[str] = []
    for finding in findings if isinstance(findings, list) else []:
        lines.extend(
            [
                f"### {finding['id']} [{finding['priority']}]: {finding['title']}",
                "",
                finding["risk"],
                "",
                f"Evidence: {finding['evidence']}",
                "",
                f"Required behavior: {finding['required_behavior']}",
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
        )
    event = history[-1]
    kind = event["kind"]
    lines = [
        "# Latest Review Report",
        "",
        f"- Review ID: {document['review_id']}",
        f"- Review name: {document['name']}",
        f"- Status: {event['status']}",
        f"- Iteration: {event['iteration']}",
        f"- Role: {event['role']}",
        f"- Recorded at: {event_time(event)}",
    ]
    source_field = SOURCE_FIELD_BY_KIND.get(kind)
    if source_field:
        source = event.get(source_field, {})
        lines.append(f"- Source fingerprint: {source.get('fingerprint', 'Unavailable')}")

    if kind == "review":
        lines.extend(["", "## Findings", "", *render_findings(event.get("findings"))])
    elif kind == "review_correction":
        lines.extend(
            [
                "",
                "## Correction",
                "",
                f"- Affected findings: {', '.join(event.get('affected_findings', []))}",
                f"- Correction: {event.get('correction', '')}",
                f"- Source drift: {event.get('source_drift', '')}",
            ]
        )
        if event.get("findings"):
            lines.extend(["", "## Added Findings", "", *render_findings(event["findings"])])
    elif kind == "owner_response":
        lines.extend(
            [
                "",
                "## Source State",
                "",
                f"- Starting fingerprint: {event['starting_source_snapshot']['fingerprint']}",
                f"- Completed fingerprint: {event['completed_source_snapshot']['fingerprint']}",
                f"- Drift assessment: {event.get('source_drift_assessment', '')}",
            ]
        )
        lines.extend(["", "## Decisions", ""])
        for disposition in event.get("dispositions", []):
            lines.extend(
                [
                    f"### {disposition['id']}: {disposition['decision']}",
                    "",
                    disposition["rationale"],
                    "",
                ]
            )
            if disposition.get("decision") == "deferred/blocked":
                lines.extend(
                    [
                        f"- Blocker: {disposition.get('blocker', '')}",
                        f"- Completed work: {disposition.get('completed_work', '')}",
                        f"- Remaining work: {disposition.get('remaining_work', '')}",
                        f"- Validation gap: {disposition.get('validation_gap', '')}",
                        "",
                    ]
                )
        lines.extend(["## Files Changed", "", *markdown_list(event.get("files_changed")), ""])
        lines.extend(
            [
                "## Guide Synchronization",
                "",
                event.get("guide_synchronization", ""),
                "",
            ]
        )
        lines.extend(["## Resulting Revisions", "", *markdown_list(event.get("commits")), ""])
    elif kind == "final_review":
        lines.append(f"- Reviewed through: {event.get('reviewed_through', '')}")
        lines.extend(["", "## Decision", "", event.get("decision", ""), ""])
        lines.extend(["## Resolutions", "", *markdown_list(event.get("resolutions")), ""])
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
        lines.extend(["## Validation", "", *markdown_list(validation.get("performed")), ""])
        lines.extend(
            ["## Unavailable Checks", "", *markdown_list(validation.get("unavailable")), ""]
        )
        lines.extend(
            ["## Remaining Gaps", "", *markdown_list(validation.get("remaining_gaps")), ""]
        )
    return "\n".join(lines).rstrip() + "\n"


def append_event(document: Any, event: Any) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise ValueError("review document must be a JSON object")
    history = document.get("history")
    state = document.get("state")
    if not isinstance(history, list) or not isinstance(state, dict):
        raise ValueError("review document has invalid history or state")
    if not isinstance(event, dict):
        raise ValueError("event must be a JSON object")

    history.append(event)
    state.update(
        {
            "marker": event.get("status"),
            "latest_iteration": event.get("iteration"),
            "latest_event_kind": event.get("kind"),
            "updated_at": event_time(event),
        }
    )
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
    template_parser.add_argument("iteration", type=int)
    args = parser.parse_args()

    if args.command == "template":
        print(json.dumps(event_template(args.kind, args.iteration), indent=2))
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
