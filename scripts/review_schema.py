"""Validate review document and event shapes."""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from typing import Any

from review_contract import SOURCE_FIELD_BY_KIND, TIMEOUT_DURATION_BY_KIND

__all__ = [
    "ACTOR_BY_KIND",
    "CREATOR_VERSION",
    "EVENT_ID_PATTERN",
    "EVIDENCE_BASES",
    "FORMAT",
    "FORMAT_REVISION",
    "REVIEW_KINDS",
    "SHA256_PATTERN",
    "STRUCTURE_DEBT_DISPOSITIONS",
    "STRUCTURE_POLICIES",
    "load_json",
    "reject_duplicate_keys",
    "validate_event",
]

FORMAT = "local-pr-loop"
FORMAT_REVISION = "2026-08-21.1"
CREATOR_VERSION = "0.7.0"

ACTOR_BY_KIND = {
    "review": "reviewer",
    "source_update": "reviewer",
    "owner_reply": "owner",
    "reviewer_update": "reviewer",
    "final_review": "reviewer",
    "reviewer_timeout": "owner",
    "owner_timeout": "reviewer",
    "initial_review_timeout": "owner",
}
TERMINAL_OUTCOME_BY_KIND = {
    "final_review": "lgtm",
    "reviewer_timeout": "reviewer_timeout",
    "owner_timeout": "owner_timeout",
    "initial_review_timeout": "initial_review_timeout",
}
THREAD_PRIORITIES = {"P0", "P1", "P2", "P3"}
# What one loop reviews for. A correctness loop finds defects at sites; a structure loop
# reviews shape across its whole scope while preserving behavior.
REVIEW_KINDS = {"correctness", "structure"}
# Whether a correctness terminal with accretion-flagged files chains a structure round
# automatically, records the deferral for the user, or ignores the ledger entirely.
STRUCTURE_POLICIES = {"auto", "defer", "off"}
# How a correctness final_review disposed of accretion-flagged files. "structure_reviewed"
# never means a check ran here; it records the reviewer's judgment that the flagged growth
# is not accretion or is already covered by a structure round.
STRUCTURE_DEBT_DISPOSITIONS = {"structure_reviewed", "structure_deferred"}
# How a validation gap was disposed of. A gap that is still material is not resolved at
# all: it stays open and blocks LGTM, so it has no disposition value here.
GAP_DISPOSITIONS = {
    # The check was later performed, and direct evidence records its result.
    "performed",
    # The check remains unavailable. Independent evidence shows the residual risk is
    # non-material because the behavior fails closed. This never means the check passed.
    "unavailable_non_material",
}
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
GAP_ID_PATTERN = re.compile(r"^G([1-9][0-9]*)$")
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


def reject_unknown(
    errors: list[str], value: dict[str, Any], allowed: set[str], prefix: str
) -> None:
    unknown = sorted(set(value) - allowed)
    require(
        errors,
        not unknown,
        f"{prefix} contains unknown fields: {', '.join(unknown)}",
    )


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
    reject_unknown(
        errors,
        snapshot,
        {
            "revision",
            "scope",
            "fingerprint",
            "exclusions",
            "additional_inputs",
            "staged_sha256",
            "unstaged_sha256",
            "untracked",
        },
        prefix,
    )
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
            reject_unknown(
                errors,
                item,
                {"path", "kind", "mode", "sha256", "link_target"},
                item_prefix,
            )
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
        for key in ("staged_sha256", "unstaged_sha256"):
            require(
                errors,
                isinstance(snapshot.get(key), str)
                and bool(SHA256_PATTERN.fullmatch(snapshot[key])),
                f"{prefix}.{key} must be lowercase SHA-256",
            )
        untracked = snapshot.get("untracked")
        require(
            errors, isinstance(untracked, list), f"{prefix}.untracked must be a list"
        )
        if isinstance(untracked, list):
            for index, item in enumerate(untracked):
                item_prefix = f"{prefix}.untracked[{index}]"
                require(
                    errors, isinstance(item, dict), f"{item_prefix} must be a mapping"
                )
                if not isinstance(item, dict):
                    continue
                reject_unknown(
                    errors,
                    item,
                    {"path", "kind", "mode", "sha256", "link_target"},
                    item_prefix,
                )
                for key in ("path", "kind", "mode", "sha256"):
                    require(
                        errors,
                        isinstance(item.get(key), str) and bool(item[key]),
                        f"{item_prefix}.{key} must be a non-empty string",
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


GAP_JUSTIFICATION_FIELDS = {"unperformed_check", "fail_closed_behavior"}


def validate_gap_justification(errors: list[str], resolution: Any, prefix: str) -> None:
    """Require the reasoning behind resolving a gap whose check never ran.

    The report states that the check was not performed and that the residual risk was
    judged non-material and fail-closed. Those are claims about the review, so the event
    has to carry them as recorded facts rather than let the renderer assert them.
    """

    justification = resolution.get("justification")
    unavailable = resolution.get("disposition") == "unavailable_non_material"
    if unavailable and justification is None:
        errors.append(
            f"{prefix}.justification is required for an unavailable_non_material "
            "disposition, naming unperformed_check and fail_closed_behavior"
        )
        return
    if justification is None:
        return
    if not unavailable:
        errors.append(
            f"{prefix}.justification applies only to an unavailable_non_material "
            "disposition"
        )
        return
    require(
        errors, isinstance(justification, dict), f"{prefix}.justification must be a mapping"
    )
    if not isinstance(justification, dict):
        return
    reject_unknown(
        errors, justification, GAP_JUSTIFICATION_FIELDS, f"{prefix}.justification"
    )
    for field in sorted(GAP_JUSTIFICATION_FIELDS):
        require(
            errors,
            isinstance(justification.get(field), str) and bool(justification[field]),
            f"{prefix}.justification.{field} must be a non-empty string",
        )


def validate_evidence(errors: list[str], value: Any, prefix: str) -> None:
    require(errors, isinstance(value, dict), f"{prefix} must be a mapping")
    if not isinstance(value, dict):
        return
    reject_unknown(
        errors,
        value,
        {"basis", "provenance", "observed_at", "sanitized_result", "artifact_digest"},
        prefix,
    )
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
    if value.get("basis") == "captured_fixture":
        require(
            errors,
            isinstance(digest, str) and bool(SHA256_PATTERN.fullmatch(digest)),
            f"{prefix}.artifact_digest is required for captured_fixture",
        )


def validate_validation(errors: list[str], value: Any, prefix: str) -> None:
    require(errors, isinstance(value, dict), f"{prefix} must be a mapping")
    if not isinstance(value, dict):
        return
    reject_unknown(errors, value, {"performed", "gaps"}, prefix)
    performed = value.get("performed")
    gaps = value.get("gaps")
    require(errors, isinstance(performed, list), f"{prefix}.performed must be a list")
    if isinstance(performed, list):
        for index, check in enumerate(performed):
            item_prefix = f"{prefix}.performed[{index}]"
            require(errors, isinstance(check, dict), f"{item_prefix} must be a mapping")
            if not isinstance(check, dict):
                continue
            reject_unknown(errors, check, {"check", "result", "evidence"}, item_prefix)
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
            reject_unknown(
                errors, gap, {"gap_id", "check", "reason", "material"}, item_prefix
            )
            require(
                errors,
                isinstance(gap.get("gap_id"), str)
                and bool(GAP_ID_PATTERN.fullmatch(gap["gap_id"])),
                f"{item_prefix}.gap_id must match G<N>",
            )
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
    if isinstance(performed, list) and isinstance(gaps, list):
        material_checks = {
            gap.get("check")
            for gap in gaps
            if isinstance(gap, dict) and gap.get("material") is True
        }
        for index, check in enumerate(performed):
            if isinstance(check, dict) and check.get("result") == "failed":
                require(
                    errors,
                    check.get("check") in material_checks,
                    f"{prefix}.performed[{index}]: failed check requires a matching "
                    "material validation gap",
                )


def validate_thread(errors: list[str], thread: Any, prefix: str) -> None:
    require(errors, isinstance(thread, dict), f"{prefix} must be a mapping")
    if not isinstance(thread, dict):
        return
    reject_unknown(
        errors,
        thread,
        {
            "id",
            "priority",
            "contract",
            "title",
            "risk",
            "evidence",
            "required_behavior",
            "message",
            "paths",
        },
        prefix,
    )
    if "paths" in thread:
        validate_string_list(errors, thread["paths"], f"{prefix}.paths", unique=True)
    message = thread.get("message")
    require(
        errors,
        message is None or (isinstance(message, str) and bool(message)),
        f"{prefix}.message must be a non-empty string when present",
    )
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


def validate_structure_debt(errors: list[str], value: Any, prefix: str) -> None:
    """Validate the acknowledgment of accretion-flagged files on a final_review.

    Presence is enforced at publish time against the current ledger, not here: whether
    files are flagged depends on the guarded tree, and projection must stay valid after
    the tree moves on.
    """

    require(errors, isinstance(value, dict), f"{prefix} must be a mapping")
    if not isinstance(value, dict):
        return
    reject_unknown(
        errors, value, {"disposition", "flagged_paths", "message"}, prefix
    )
    require(
        errors,
        value.get("disposition") in STRUCTURE_DEBT_DISPOSITIONS,
        f"{prefix}.disposition must be one of "
        + ", ".join(sorted(STRUCTURE_DEBT_DISPOSITIONS)),
    )
    validate_string_list(
        errors, value.get("flagged_paths"), f"{prefix}.flagged_paths", unique=True
    )
    require(
        errors,
        bool(value.get("flagged_paths")),
        f"{prefix}.flagged_paths must not be empty",
    )
    require(
        errors,
        isinstance(value.get("message"), str) and bool(value["message"]),
        f"{prefix}.message must be a non-empty string",
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
    allowed_fields = {"thread_id", "message", "action"}
    if resolution:
        allowed_fields.add("verification")
    reject_unknown(errors, action, allowed_fields, prefix)
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
            reject_unknown(
                errors,
                verification,
                {"independent", "evidence"},
                f"{prefix}.verification",
            )
            require(
                errors,
                verification.get("independent") is True,
                f"{prefix}.verification.independent must be true",
            )
            validate_evidence(
                errors, verification.get("evidence"), f"{prefix}.verification.evidence"
            )


def validate_event(event: Any) -> list[str]:
    """Return every closed-schema and semantic error in one review event."""
    errors: list[str] = []
    require(errors, isinstance(event, dict), "event must be a mapping")
    if not isinstance(event, dict):
        return errors
    kind = event.get("kind")
    require(errors, kind in ACTOR_BY_KIND, f"unsupported event kind: {kind}")
    if kind not in ACTOR_BY_KIND:
        return errors
    allowed_by_kind = {
        "review": {"source_snapshot", "threads", "validation"},
        "source_update": {
            "source_snapshot",
            "reason",
            "thread_impacts",
            "new_threads",
            "validation",
        },
        "owner_reply": {
            "starting_source_snapshot",
            "source_drift_assessment",
            "completed_source_snapshot",
            "replies",
            "files_changed",
            "guide_synchronization",
            "validation",
            "commits",
        },
        "reviewer_update": {
            "source_snapshot",
            "decisions",
            "new_threads",
            "gap_resolutions",
            "validation",
        },
        "final_review": {
            "source_snapshot",
            "resolutions",
            "gap_resolutions",
            "decision",
            "validation",
            "structure_debt",
        },
        "reviewer_timeout": {"reason", "started_at", "deadline"},
        "owner_timeout": {"reason", "started_at", "deadline"},
        "initial_review_timeout": {"reason", "started_at", "deadline"},
    }
    reject_unknown(
        errors,
        event,
        {"event_id", "kind", "occurred_at"} | allowed_by_kind[kind],
        "event",
    )
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
                require(errors, isinstance(reply, dict), f"{prefix} must be a mapping")
                if not isinstance(reply, dict):
                    continue
                reject_unknown(
                    errors,
                    reply,
                    {
                        "thread_id",
                        "decision",
                        "message",
                        "evidence",
                        "blocker",
                        "completed_work",
                        "remaining_work",
                        "validation_gap",
                    },
                    prefix,
                )
                thread_id = reply.get("thread_id")
                require(
                    errors,
                    isinstance(thread_id, str)
                    and bool(THREAD_ID_PATTERN.fullmatch(thread_id)),
                    f"{prefix}.thread_id must match T<N>",
                )
                require(
                    errors,
                    isinstance(reply.get("message"), str) and bool(reply["message"]),
                    f"{prefix}.message must be a non-empty string",
                )
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
        if "structure_debt" in event:
            validate_structure_debt(errors, event["structure_debt"], "structure_debt")
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
    if kind in {"reviewer_update", "final_review"}:
        gap_resolutions = event.get("gap_resolutions")
        require(
            errors,
            isinstance(gap_resolutions, list),
            "gap_resolutions must be a list",
        )
        if isinstance(gap_resolutions, list):
            for index, resolution in enumerate(gap_resolutions):
                prefix = f"gap_resolutions[{index}]"
                require(
                    errors, isinstance(resolution, dict), f"{prefix} must be a mapping"
                )
                if not isinstance(resolution, dict):
                    continue
                reject_unknown(
                    errors,
                    resolution,
                    {
                        "gap_id",
                        "message",
                        "evidence",
                        "disposition",
                        "justification",
                    },
                    prefix,
                )
                require(
                    errors,
                    resolution.get("disposition") in GAP_DISPOSITIONS,
                    f"{prefix}.disposition must be one of "
                    + ", ".join(sorted(GAP_DISPOSITIONS))
                    + "; a gap that is still material stays open instead",
                )
                validate_gap_justification(errors, resolution, prefix)
                require(
                    errors,
                    isinstance(resolution.get("gap_id"), str)
                    and bool(GAP_ID_PATTERN.fullmatch(resolution["gap_id"])),
                    f"{prefix}.gap_id must match G<N>",
                )
                require(
                    errors,
                    isinstance(resolution.get("message"), str)
                    and bool(resolution["message"]),
                    f"{prefix}.message must be a non-empty string",
                )
                validate_evidence(
                    errors, resolution.get("evidence"), f"{prefix}.evidence"
                )

    event_timestamp = parse_timestamp(errors, event.get("occurred_at"), "occurred_at")

    def check_evidence_times(value: Any, prefix: str) -> None:
        if isinstance(value, dict):
            if {"basis", "provenance", "observed_at", "sanitized_result"} <= set(value):
                observed = parse_timestamp(
                    errors, value.get("observed_at"), f"{prefix}.observed_at"
                )
                if observed and event_timestamp:
                    require(
                        errors,
                        observed <= event_timestamp,
                        f"{prefix}.observed_at must not follow event.occurred_at",
                    )
            for key, item in value.items():
                check_evidence_times(item, f"{prefix}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                check_evidence_times(item, f"{prefix}[{index}]")

    check_evidence_times(event, "event")
    return errors
