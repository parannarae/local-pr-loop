"""Validate review document and transaction shapes."""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from typing import Any

from review_contract import SOURCE_FIELD_BY_KIND, TIMEOUT_DURATION_BY_KIND
from review_notes import NOTE_TAGS

__all__ = [
    "ACTOR_BY_KIND",
    "CREATOR_VERSION",
    "EVENT_ID_PATTERN",
    "EVIDENCE_BASES",
    "FORMAT",
    "FORMAT_REVISION",
    "OPERATIONS_BY_KIND",
    "OPERATION_FIELDS",
    "REVIEW_KINDS",
    "SHA256_PATTERN",
    "STRUCTURE_DEBT_DISPOSITIONS",
    "STRUCTURE_POLICIES",
    "load_json_from_stdin",
    "operations_of",
    "reject_duplicate_keys",
    "unsupported_revision_error",
    "validate_event",
]

FORMAT = "local-pr-loop"
# Calendar revision of the persisted storage contract, independent of the skill
# version. This revision records structural acts as typed operations.
FORMAT_REVISION = "2026-09-11.1"
CREATOR_VERSION = "0.10.0"

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
THREAD_DECISIONS = {"applied", "declined", "deferred/blocked"}
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

# The operation vocabulary of this format revision, as required and optional fields
# beyond `op`. The set is closed: an unknown operation and an unknown field inside a
# known one are rejected alike, which is what keeps untyped payloads out of history.
OPERATION_FIELDS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "thread.open": (
        frozenset(
            {
                "id",
                "priority",
                "contract",
                "title",
                "risk",
                "required_behavior",
                "paths",
                "evidence",
            }
        ),
        frozenset({"message", "anchors"}),
    ),
    "thread.reply": (
        frozenset({"thread_id", "decision", "message", "evidence"}),
        frozenset({"blocker", "completed_work", "remaining_work", "validation_gap"}),
    ),
    "thread.comment": (frozenset({"thread_id", "message"}), frozenset()),
    "thread.resolve": (
        frozenset({"thread_id", "message"}),
        frozenset({"verification"}),
    ),
    "thread.reopen": (frozenset({"thread_id", "message"}), frozenset()),
    "gap.open": (
        frozenset({"gap_id", "check", "reason", "material"}),
        frozenset(),
    ),
    "gap.resolve": (
        frozenset({"gap_id", "disposition", "message", "evidence"}),
        frozenset({"justification"}),
    ),
    "check.record": (frozenset({"check", "result"}), frozenset({"evidence"})),
    "note.attach": (frozenset({"target", "tag", "message"}), frozenset()),
    "source.replace": (frozenset({"snapshot", "reason"}), frozenset()),
    "review.approve": (frozenset({"decision"}), frozenset({"structure_debt"})),
    "timeout.declare": (
        frozenset({"started_at", "deadline", "reason"}),
        frozenset(),
    ),
}

# Recording what a transaction validated, and flagging something for the user, belong
# to every handoff. A timeout carries its declaration and nothing else.
COMMON_OPERATIONS = frozenset({"check.record", "gap.open", "note.attach"})

OPERATIONS_BY_KIND: dict[str, frozenset[str]] = {
    "review": COMMON_OPERATIONS | {"thread.open"},
    "source_update": COMMON_OPERATIONS
    | {"source.replace", "thread.open", "thread.comment", "thread.reopen"},
    "owner_reply": COMMON_OPERATIONS | {"thread.reply"},
    "reviewer_update": COMMON_OPERATIONS
    | {
        "thread.open",
        "thread.comment",
        "thread.resolve",
        "thread.reopen",
        "gap.resolve",
    },
    "final_review": COMMON_OPERATIONS
    | {"thread.resolve", "review.approve", "gap.resolve"},
    "reviewer_timeout": frozenset({"timeout.declare"}),
    "owner_timeout": frozenset({"timeout.declare"}),
    "initial_review_timeout": frozenset({"timeout.declare"}),
}

# Envelope fields each kind carries beyond the four every transaction has.
ENVELOPE_FIELDS_BY_KIND: dict[str, frozenset[str]] = {
    "review": frozenset({"source_snapshot"}),
    "source_update": frozenset({"source_snapshot"}),
    "owner_reply": frozenset(
        {
            "starting_source_snapshot",
            "completed_source_snapshot",
            "source_drift_assessment",
            "guide_synchronization",
            "changed_files",
            "revisions",
        }
    ),
    "reviewer_update": frozenset({"source_snapshot"}),
    "final_review": frozenset({"source_snapshot"}),
    "reviewer_timeout": frozenset(),
    "owner_timeout": frozenset(),
    "initial_review_timeout": frozenset(),
}


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def load_json_from_stdin() -> Any:
    """Read one JSON value from standard input.

    Raises:
        ValueError: An object repeats a key, which would silently discard one of two
            conflicting values; `json.JSONDecodeError` for malformed input is a subclass.
    """

    return json.load(sys.stdin, object_pairs_hook=reject_duplicate_keys)


def unsupported_revision_error(document: Any) -> str | None:
    """Return why this document's storage contract is unreadable here, or None.

    Callers must run this before reading any other field. An artifact written
    against a different revision carries a different field set, so interpreting
    it would read absent fields as empty defaults and skip the publication gates
    those fields feed.
    """

    if not isinstance(document, dict):
        return "document must be a mapping"
    format_value = document.get("format")
    revision = document.get("format_revision")
    if format_value == FORMAT and revision == FORMAT_REVISION:
        return None
    return (
        f"unsupported storage contract: format {format_value!r} revision "
        f"{revision!r}; these tools read {FORMAT} {FORMAT_REVISION} only. No "
        "migration exists in either direction; preserve this artifact and start "
        "a new loop"
    )


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


def require_text(
    errors: list[str], value: dict[str, Any], key: str, prefix: str
) -> None:
    """Require one non-empty prose field on a mapping."""
    require(
        errors,
        isinstance(value.get(key), str) and bool(value[key]),
        f"{prefix}.{key} must be a non-empty string",
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


def validate_digested_entries(errors: list[str], entries: Any, prefix: str) -> None:
    """Validate one list of digested snapshot entries, rejecting duplicate paths.

    `additional_inputs` and `untracked` carry the same entry shape from
    `source_snapshot.py`, so both are held to it: an entry that names a kind this
    format does not record, or a digest that is not SHA-256, never reaches canonical
    history.
    """

    require(errors, isinstance(entries, list), f"{prefix} must be a list")
    if not isinstance(entries, list):
        return
    paths: list[str] = []
    for index, item in enumerate(entries):
        item_prefix = f"{prefix}[{index}]"
        require(errors, isinstance(item, dict), f"{item_prefix} must be a mapping")
        if not isinstance(item, dict):
            continue
        reject_unknown(
            errors, item, {"path", "kind", "mode", "sha256", "link_target"}, item_prefix
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
                isinstance(item.get("link_target"), str) and bool(item["link_target"]),
                f"{item_prefix}.link_target is required for a symlink",
            )
    require(errors, len(paths) == len(set(paths)), f"{prefix} paths must be unique")


def validate_snapshot(errors: list[str], snapshot: Any, prefix: str) -> None:
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
    validate_digested_entries(
        errors, snapshot.get("additional_inputs"), f"{prefix}.additional_inputs"
    )
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
    validate_digested_entries(errors, snapshot.get("untracked"), f"{prefix}.untracked")


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
    judged non-material and fail-closed. Those are claims about the review, so the
    operation has to carry them as recorded facts rather than let the renderer assert
    them.
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
        errors,
        isinstance(justification, dict),
        f"{prefix}.justification must be a mapping",
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


def validate_evidence(
    errors: list[str], value: Any, prefix: str, occurred: datetime | None
) -> None:
    """Validate one evidence record against the instant of the transaction carrying it.

    `occurred` is the transaction's `occurred_at`, or None when it could not be parsed;
    the ordering check is then skipped rather than compared against nothing.
    """

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
        require_text(errors, value, key, prefix)
    observed = parse_timestamp(
        errors, value.get("observed_at"), f"{prefix}.observed_at"
    )
    if observed and occurred:
        require(
            errors,
            observed <= occurred,
            f"{prefix}.observed_at must not follow event.occurred_at",
        )
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


def validate_structure_debt(errors: list[str], value: Any, prefix: str) -> None:
    """Validate the acknowledgment of accretion-flagged files on a `review.approve`.

    Presence is enforced at publish time against the current ledger, not here: whether
    files are flagged depends on the guarded tree, and projection must stay valid after
    the tree moves on.
    """

    require(errors, isinstance(value, dict), f"{prefix} must be a mapping")
    if not isinstance(value, dict):
        return
    reject_unknown(errors, value, {"disposition", "flagged_paths", "message"}, prefix)
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
    require_text(errors, value, "message", prefix)


# --- operations ---


def validate_thread_reference(errors: list[str], value: Any, prefix: str) -> None:
    require(
        errors,
        isinstance(value, str) and bool(THREAD_ID_PATTERN.fullmatch(value)),
        f"{prefix} must match T<N>",
    )


def validate_anchors(errors: list[str], operation: dict[str, Any], prefix: str) -> None:
    """Validate optional line ranges pinned against the guarded snapshot's revision."""

    anchors = operation.get("anchors")
    require(errors, isinstance(anchors, list), f"{prefix}.anchors must be a list")
    if not isinstance(anchors, list):
        return
    paths = operation.get("paths")
    declared: set[str] = set()
    if isinstance(paths, list):
        declared = {item for item in paths if isinstance(item, str)}
    seen: list[str] = []
    for index, anchor in enumerate(anchors):
        item_prefix = f"{prefix}.anchors[{index}]"
        require(errors, isinstance(anchor, dict), f"{item_prefix} must be a mapping")
        if not isinstance(anchor, dict):
            continue
        reject_unknown(errors, anchor, {"path", "start_line", "end_line"}, item_prefix)
        path = anchor.get("path")
        require(
            errors,
            isinstance(path, str) and path in declared,
            f"{item_prefix}.path must also appear in paths",
        )
        lines: dict[str, int] = {}
        for key in ("start_line", "end_line"):
            value = anchor.get(key)
            valid = type(value) is int and value >= 1
            require(errors, valid, f"{item_prefix}.{key} must be a 1-based line number")
            if valid:
                lines[key] = value
        if len(lines) == 2:
            require(
                errors,
                lines["end_line"] >= lines["start_line"],
                f"{item_prefix}.end_line must not precede start_line",
            )
        # Compared as text so an entry whose values are not hashable still
        # participates in the uniqueness check instead of raising.
        seen.append(
            repr(
                (
                    anchor.get("path"),
                    anchor.get("start_line"),
                    anchor.get("end_line"),
                )
            )
        )
    require(
        errors, len(seen) == len(set(seen)), f"{prefix}.anchors entries must be unique"
    )


def validate_thread_open(
    errors: list[str], operation: dict[str, Any], prefix: str, occurred: datetime | None
) -> None:
    validate_thread_reference(errors, operation.get("id"), f"{prefix}.id")
    require(
        errors,
        operation.get("priority") in THREAD_PRIORITIES,
        f"{prefix}.priority must be one of {', '.join(sorted(THREAD_PRIORITIES))}",
    )
    require(
        errors,
        operation.get("contract") in {"internal", "external"},
        f"{prefix}.contract must be internal or external",
    )
    for key in ("title", "risk", "required_behavior"):
        require_text(errors, operation, key, prefix)
    validate_string_list(errors, operation.get("paths"), f"{prefix}.paths", unique=True)
    message = operation.get("message")
    require(
        errors,
        message is None or (isinstance(message, str) and bool(message)),
        f"{prefix}.message must be a non-empty string when present",
    )
    validate_evidence(errors, operation.get("evidence"), f"{prefix}.evidence", occurred)
    evidence = operation.get("evidence")
    if (
        operation.get("priority") in {"P1", "P2"}
        and operation.get("contract") == "external"
        and isinstance(evidence, dict)
    ):
        require(
            errors,
            evidence.get("basis") in EXTERNAL_EVIDENCE_BASES,
            f"{prefix}: external-contract P1/P2 evidence must use "
            "live_probe, captured_fixture, or authoritative_contract",
        )
    if "anchors" in operation:
        validate_anchors(errors, operation, prefix)


def validate_thread_reply(
    errors: list[str], operation: dict[str, Any], prefix: str, occurred: datetime | None
) -> None:
    validate_thread_reference(errors, operation.get("thread_id"), f"{prefix}.thread_id")
    require_text(errors, operation, "message", prefix)
    require(
        errors,
        operation.get("decision") in THREAD_DECISIONS,
        f"{prefix}.decision is invalid",
    )
    validate_evidence(errors, operation.get("evidence"), f"{prefix}.evidence", occurred)
    if operation.get("decision") == "deferred/blocked":
        for key in ("blocker", "completed_work", "remaining_work", "validation_gap"):
            require_text(errors, operation, key, prefix)


def validate_thread_resolve(
    errors: list[str], operation: dict[str, Any], prefix: str, occurred: datetime | None
) -> None:
    validate_thread_reference(errors, operation.get("thread_id"), f"{prefix}.thread_id")
    require_text(errors, operation, "message", prefix)
    if "verification" not in operation:
        return
    verification = operation["verification"]
    require(
        errors,
        isinstance(verification, dict),
        f"{prefix}.verification must be a mapping",
    )
    if not isinstance(verification, dict):
        return
    reject_unknown(
        errors, verification, {"independent", "evidence"}, f"{prefix}.verification"
    )
    require(
        errors,
        verification.get("independent") is True,
        f"{prefix}.verification.independent must be true",
    )
    validate_evidence(
        errors,
        verification.get("evidence"),
        f"{prefix}.verification.evidence",
        occurred,
    )


def validate_gap_open(
    errors: list[str], operation: dict[str, Any], prefix: str, occurred: datetime | None
) -> None:
    require(
        errors,
        isinstance(operation.get("gap_id"), str)
        and bool(GAP_ID_PATTERN.fullmatch(operation["gap_id"])),
        f"{prefix}.gap_id must match G<N>",
    )
    for key in ("check", "reason"):
        require_text(errors, operation, key, prefix)
    require(
        errors,
        type(operation.get("material")) is bool,
        f"{prefix}.material must be boolean",
    )


def validate_gap_resolve(
    errors: list[str], operation: dict[str, Any], prefix: str, occurred: datetime | None
) -> None:
    require(
        errors,
        operation.get("disposition") in GAP_DISPOSITIONS,
        f"{prefix}.disposition must be one of "
        + ", ".join(sorted(GAP_DISPOSITIONS))
        + "; a gap that is still material stays open instead",
    )
    validate_gap_justification(errors, operation, prefix)
    require(
        errors,
        isinstance(operation.get("gap_id"), str)
        and bool(GAP_ID_PATTERN.fullmatch(operation["gap_id"])),
        f"{prefix}.gap_id must match G<N>",
    )
    require_text(errors, operation, "message", prefix)
    validate_evidence(errors, operation.get("evidence"), f"{prefix}.evidence", occurred)


def validate_check_record(
    errors: list[str], operation: dict[str, Any], prefix: str, occurred: datetime | None
) -> None:
    require_text(errors, operation, "check", prefix)
    require(
        errors,
        operation.get("result") in {"passed", "failed"},
        f"{prefix}.result must be passed or failed",
    )
    if "evidence" in operation:
        validate_evidence(errors, operation["evidence"], f"{prefix}.evidence", occurred)


def validate_note_attach(
    errors: list[str], operation: dict[str, Any], prefix: str, occurred: datetime | None
) -> None:
    target = operation.get("target")
    require(errors, isinstance(target, dict), f"{prefix}.target must be a mapping")
    if isinstance(target, dict):
        reject_unknown(errors, target, {"kind", "id"}, f"{prefix}.target")
        require(
            errors,
            target.get("kind") == "thread",
            f"{prefix}.target.kind must be thread in this format revision",
        )
        validate_thread_reference(errors, target.get("id"), f"{prefix}.target.id")
    require(
        errors,
        operation.get("tag") in NOTE_TAGS,
        f"{prefix}.tag must be one of {', '.join(NOTE_TAGS)}",
    )
    require_text(errors, operation, "message", prefix)


def validate_source_replace(
    errors: list[str], operation: dict[str, Any], prefix: str, occurred: datetime | None
) -> None:
    validate_snapshot(errors, operation.get("snapshot"), f"{prefix}.snapshot")
    require_text(errors, operation, "reason", prefix)


def validate_review_approve(
    errors: list[str], operation: dict[str, Any], prefix: str, occurred: datetime | None
) -> None:
    require_text(errors, operation, "decision", prefix)
    if "structure_debt" in operation:
        validate_structure_debt(
            errors, operation["structure_debt"], f"{prefix}.structure_debt"
        )


def validate_timeout_declare(
    errors: list[str], operation: dict[str, Any], prefix: str, kind: str, occurred: Any
) -> None:
    require_text(errors, operation, "reason", prefix)
    started = parse_timestamp(
        errors, operation.get("started_at"), f"{prefix}.started_at"
    )
    deadline = parse_timestamp(errors, operation.get("deadline"), f"{prefix}.deadline")
    if started and deadline:
        require(
            errors,
            deadline == started + TIMEOUT_DURATION_BY_KIND[kind],
            f"{kind}.deadline has the wrong duration",
        )
    if occurred and deadline:
        require(errors, occurred >= deadline, f"{kind} occurred before its deadline")


def validate_thread_message(
    errors: list[str], operation: dict[str, Any], prefix: str, occurred: datetime | None
) -> None:
    """Validate a comment or reopen: one thread reference and one prose body."""

    validate_thread_reference(errors, operation.get("thread_id"), f"{prefix}.thread_id")
    require_text(errors, operation, "message", prefix)


# Every validator receives the transaction's parsed `occurred_at`, because evidence is
# only valid relative to the handoff recording it; the operations that carry no evidence
# accept the argument and ignore it, so the dispatch below has one signature.
OPERATION_VALIDATORS = {
    "thread.open": validate_thread_open,
    "thread.reply": validate_thread_reply,
    "thread.comment": validate_thread_message,
    "thread.resolve": validate_thread_resolve,
    "thread.reopen": validate_thread_message,
    "gap.open": validate_gap_open,
    "gap.resolve": validate_gap_resolve,
    "check.record": validate_check_record,
    "note.attach": validate_note_attach,
    "source.replace": validate_source_replace,
    "review.approve": validate_review_approve,
}


def operations_of(event: Any) -> list[Any]:
    """Return one transaction's operations, or an empty list when it has none."""

    if not isinstance(event, dict):
        return []
    operations = event.get("operations")
    return operations if isinstance(operations, list) else []


def operation_names(operations: Any) -> list[str]:
    """Return the `op` discriminator of each entry, for counting per-kind rules."""

    if not isinstance(operations, list):
        return []
    return [
        item.get("op")
        for item in operations
        if isinstance(item, dict) and isinstance(item.get("op"), str)
    ]


def validate_operation(
    errors: list[str],
    operation: Any,
    prefix: str,
    kind: str,
    occurred: datetime | None,
) -> None:
    require(errors, isinstance(operation, dict), f"{prefix} must be a mapping")
    if not isinstance(operation, dict):
        return
    name = operation.get("op")
    known = isinstance(name, str) and name in OPERATION_FIELDS
    require(errors, known, f"{prefix}.op is not a known operation")
    if not known:
        return
    require(
        errors,
        name in OPERATIONS_BY_KIND[kind],
        f"{prefix}: {kind} does not allow {name}",
    )
    required, optional = OPERATION_FIELDS[name]
    reject_unknown(errors, operation, {"op"} | set(required) | set(optional), prefix)
    missing = sorted(required - set(operation))
    require(
        errors,
        not missing,
        f"{prefix} is missing required fields: {', '.join(missing)}",
    )
    if name == "timeout.declare":
        if kind in TIMEOUT_DURATION_BY_KIND:
            validate_timeout_declare(errors, operation, prefix, kind, occurred)
        return
    OPERATION_VALIDATORS[name](errors, operation, prefix, occurred)


def validate_failed_check_gaps(errors: list[str], operations: list[Any]) -> None:
    """A failed check must be recorded as a material gap in the same transaction."""

    material_checks = {
        item["check"]
        for item in operations
        if isinstance(item, dict)
        and item.get("op") == "gap.open"
        and item.get("material") is True
        and isinstance(item.get("check"), str)
    }
    for index, item in enumerate(operations):
        if (
            isinstance(item, dict)
            and item.get("op") == "check.record"
            and item.get("result") == "failed"
        ):
            require(
                errors,
                item.get("check") in material_checks,
                f"operations[{index}]: failed check requires a matching material "
                "validation gap",
            )


def validate_transaction_shape(
    errors: list[str], event: dict[str, Any], kind: str, operations: list[Any]
) -> None:
    """Check the operation counts each kind owes, independent of history."""

    names = operation_names(operations)
    if kind == "review":
        require(errors, "thread.open" in names, "review must open threads")
    elif kind == "source_update":
        require(
            errors,
            names.count("source.replace") == 1,
            "source_update must carry exactly one source.replace",
        )
        replacements = [
            item
            for item in operations
            if isinstance(item, dict) and item.get("op") == "source.replace"
        ]
        for index, item in enumerate(replacements):
            require(
                errors,
                item.get("snapshot") == event.get("source_snapshot"),
                f"operations: source.replace[{index}].snapshot must equal the "
                "transaction's source_snapshot",
            )
    elif kind == "owner_reply":
        require(errors, "thread.reply" in names, "owner_reply must reply to a thread")
    elif kind == "reviewer_update":
        require(
            errors,
            any(
                name in {"thread.comment", "thread.resolve", "thread.reopen"}
                for name in names
            ),
            "reviewer_update must decide every open thread",
        )
    elif kind == "final_review":
        require(
            errors,
            names.count("review.approve") == 1,
            "final_review must carry exactly one review.approve",
        )
    else:
        require(
            errors,
            len(operations) == 1 and names == ["timeout.declare"],
            f"{kind} carries exactly one timeout.declare and nothing else",
        )


def validate_owner_reply_metadata(errors: list[str], event: dict[str, Any]) -> None:
    """Validate the handoff metadata describing the whole owner transaction."""

    validate_snapshot(
        errors, event.get("starting_source_snapshot"), "starting_source_snapshot"
    )
    for key in ("source_drift_assessment", "guide_synchronization"):
        require(
            errors,
            isinstance(event.get(key), str) and bool(event[key]),
            f"{key} must be a non-empty string",
        )
    validate_string_list(
        errors, event.get("changed_files"), "changed_files", unique=True
    )
    revisions = event.get("revisions")
    validate_string_list(errors, revisions, "revisions", unique=True)
    if isinstance(revisions, list):
        for index, value in enumerate(revisions):
            require(
                errors,
                isinstance(value, str)
                and bool(REVISION_PATTERN.fullmatch(value)),
                f"revisions[{index}] must be a full Git object ID",
            )


def validate_event(event: Any) -> list[str]:
    """Return every closed-schema and semantic error in one review transaction."""
    errors: list[str] = []
    require(errors, isinstance(event, dict), "event must be a mapping")
    if not isinstance(event, dict):
        return errors
    kind = event.get("kind")
    require(errors, kind in ACTOR_BY_KIND, f"unsupported event kind: {kind}")
    if kind not in ACTOR_BY_KIND:
        return errors
    reject_unknown(
        errors,
        event,
        {"event_id", "kind", "occurred_at", "operations"}
        | set(ENVELOPE_FIELDS_BY_KIND[kind]),
        "event",
    )
    require(
        errors,
        isinstance(event.get("event_id"), str)
        and bool(EVENT_ID_PATTERN.fullmatch(event["event_id"])),
        "event_id must be 12-64 lowercase identifier characters",
    )
    occurred = parse_timestamp(errors, event.get("occurred_at"), "occurred_at")
    source_field = SOURCE_FIELD_BY_KIND.get(kind)
    if source_field:
        validate_snapshot(errors, event.get(source_field), source_field)
    if kind == "owner_reply":
        validate_owner_reply_metadata(errors, event)

    operations = event.get("operations")
    require(errors, isinstance(operations, list), "operations must be a list")
    if isinstance(operations, list):
        for index, operation in enumerate(operations):
            validate_operation(
                errors, operation, f"operations[{index}]", kind, occurred
            )
        validate_failed_check_gaps(errors, operations)
        validate_transaction_shape(errors, event, kind, operations)
    return errors
