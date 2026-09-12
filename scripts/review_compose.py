"""Compose typed operations into the leased transaction draft.

Every structural act an agent performs is one subcommand here. The command's
arguments are the operation's fields, a frozen constructor assembles it, and
anything the format would reject is refused at entry rather than left for
`validate-event` to find after the draft was already written. Prose arrives as
text or from a file; identifiers, timestamps, and envelopes are never typed by
an agent.

Composing the same act twice corrects it in place. Taking one back is `drop`,
which also removes whatever that strands and renumbers what the draft mints, so
a correction cannot leave behind an operation the publish would refuse.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import review_schema
import review_templates
import review_workflow
from review_io import load_object, require_secure_regular, secure_json
from review_notes import NOTE_TAGS

# Operations that name the thread they act on, and so make it a legal note target.
THREAD_ACT_OPS = ("thread.reply", "thread.comment", "thread.resolve", "thread.reopen")

# The field naming what an operation occupies at most one of per transaction.
SLOT_FIELD = {
    "thread.reply": "thread_id",
    "thread.comment": "thread_id",
    "thread.resolve": "thread_id",
    "thread.reopen": "thread_id",
    "gap.open": "gap_id",
    "gap.resolve": "gap_id",
    "check.record": "check",
}

# Operations a transaction carries exactly one of, whatever they name.
SINGLETON_OPS = ("source.replace", "review.approve")

# Operations carrying what templating derived from the guard rather than from an
# agent's flags: the accretion ledger's flagged set and the guarded snapshot.
# Dropping one would discard that derivation, and composing it again would find
# nothing to carry, so `drop` refuses them and correction goes through the
# composer, which replaces them in place.
PREFILLED_OPS = ("source.replace", "review.approve", "timeout.declare")


# --- typed constructors ---


def as_value(item: Any) -> Any:
    """Serialize one frozen constructor, dropping every absent optional field."""

    if is_dataclass(item) and not isinstance(item, type):
        return {
            field.name: as_value(getattr(item, field.name))
            for field in fields(item)
            if getattr(item, field.name) is not None
        }
    if isinstance(item, tuple):
        return [as_value(entry) for entry in item]
    return item


class Constructor:
    """Mixin turning one frozen operation record into its JSON operation."""

    OP = ""

    def as_operation(self) -> dict[str, Any]:
        return {"op": self.OP, **as_value(self)}


@dataclass(frozen=True)
class Evidence:
    basis: str
    provenance: str
    observed_at: str
    sanitized_result: str
    artifact_digest: str | None = None


@dataclass(frozen=True)
class Verification:
    independent: bool
    evidence: Evidence


@dataclass(frozen=True)
class Anchor:
    path: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class Justification:
    unperformed_check: str
    fail_closed_behavior: str


@dataclass(frozen=True)
class StructureDebt:
    disposition: str
    flagged_paths: tuple
    message: str


@dataclass(frozen=True)
class NoteTarget:
    kind: str
    id: str


@dataclass(frozen=True)
class ThreadOpen(Constructor):
    OP = "thread.open"

    id: str
    priority: str
    contract: str
    title: str
    risk: str
    required_behavior: str
    paths: tuple
    evidence: Evidence
    message: str | None = None
    anchors: tuple | None = None


@dataclass(frozen=True)
class ThreadReply(Constructor):
    OP = "thread.reply"

    thread_id: str
    decision: str
    message: str
    evidence: Evidence
    blocker: str | None = None
    completed_work: str | None = None
    remaining_work: str | None = None
    validation_gap: str | None = None


@dataclass(frozen=True)
class ThreadComment(Constructor):
    OP = "thread.comment"

    thread_id: str
    message: str


@dataclass(frozen=True)
class ThreadResolve(Constructor):
    OP = "thread.resolve"

    thread_id: str
    message: str
    verification: Verification | None = None


@dataclass(frozen=True)
class ThreadReopen(Constructor):
    OP = "thread.reopen"

    thread_id: str
    message: str


@dataclass(frozen=True)
class GapOpen(Constructor):
    OP = "gap.open"

    gap_id: str
    check: str
    reason: str
    material: bool


@dataclass(frozen=True)
class GapResolve(Constructor):
    OP = "gap.resolve"

    gap_id: str
    disposition: str
    message: str
    evidence: Evidence
    justification: Justification | None = None


@dataclass(frozen=True)
class CheckRecord(Constructor):
    OP = "check.record"

    check: str
    result: str
    evidence: Evidence | None = None


@dataclass(frozen=True)
class NoteAttach(Constructor):
    OP = "note.attach"

    target: NoteTarget
    tag: str
    message: str


@dataclass(frozen=True)
class SourceReplace(Constructor):
    OP = "source.replace"

    snapshot: dict
    reason: str


@dataclass(frozen=True)
class ReviewApprove(Constructor):
    OP = "review.approve"

    decision: str
    structure_debt: StructureDebt | None = None


# --- draft access ---


@dataclass(frozen=True)
class Draft:
    """One leased draft, the canonical sets it composes against, and its instant."""

    path: Path
    value: dict[str, Any]
    kind: str
    operations: list[Any]
    document: dict[str, Any]
    open_threads: tuple
    resolved_threads: tuple
    open_gaps: tuple
    recorded_gaps: tuple
    stamped: datetime


@dataclass(frozen=True)
class Composition:
    """What one composer contributed to the draft, and how to name it afterwards.

    `target` is the identifier `show` prints beside this act's first operation, so the
    acknowledgment can be passed straight back to `drop`; it is None for an act that
    names nothing, such as an approval. `gap_id` names a gap the composer opened
    alongside its own operation, which `record-check` does for a failed check.
    """

    operations: list[dict[str, Any]]
    target: str | None
    gap_id: str | None = None


def identifier_list(state: dict[str, Any], group: str, status: str) -> tuple:
    """Return one canonical identifier set, refusing a projection it cannot read.

    `next_identifier` mints from these sets, so a malformed group read as empty would
    restart at `T1` and mint an identifier that collides with history — caught only at
    publish, after the whole draft was composed against it.

    Raises:
        TypeError: The group is absent or is not a mapping. That is a shape failure,
            reported the way `open_draft` reports the same failure one level up.
        ValueError: The group is a mapping, but its status list is not a list of
            identifier strings.
    """

    group_value = state.get(group)
    if not isinstance(group_value, dict):
        raise TypeError(
            f"canonical state records no {group} mapping; the projection is unusable"
        )
    items = group_value.get(status)
    if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
        raise ValueError(
            f"canonical state {group}.{status} must be a list of identifier strings"
        )
    return tuple(items)


def open_draft(args: argparse.Namespace) -> Draft:
    """Verify the lease and load the draft it protects, with its canonical document."""

    review_workflow.verify_lease(review_workflow.lease_context(args))
    path = Path(args.event)
    require_secure_regular(path, "draft")
    value = load_object(path)
    kind = value.get("kind")
    if kind not in review_schema.ACTOR_BY_KIND:
        raise ValueError(
            "draft does not name a supported transaction kind; template it again"
        )
    operations = value.get("operations")
    if not isinstance(operations, list):
        raise TypeError("draft does not carry an operation list; template it again")
    document = load_object(Path(args.review))
    boundary = review_schema.unsupported_revision_error(document)
    if boundary is not None:
        raise ValueError(boundary)
    state = document.get("state")
    if not isinstance(state, dict):
        raise TypeError("the canonical review document records no state")
    return Draft(
        path=path,
        value=value,
        kind=kind,
        operations=operations,
        document=document,
        open_threads=identifier_list(state, "threads", "open"),
        resolved_threads=identifier_list(state, "threads", "resolved"),
        open_gaps=identifier_list(state, "validation_gaps", "open"),
        recorded_gaps=identifier_list(state, "validation_gaps", "open")
        + identifier_list(state, "validation_gaps", "resolved"),
        stamped=datetime.now(timezone.utc),
    )


def slot_of(operation: dict[str, Any]) -> tuple[str, str] | None:
    """Return what this operation occupies at most one of, or None if it may repeat.

    Thread lifecycle acts share one slot per thread, so composing a resolve after
    a comment on the same thread corrects the earlier act instead of recording
    two conflicting answers in one handoff.
    """

    name = operation.get("op")
    if not isinstance(name, str):
        return None
    if name in SINGLETON_OPS:
        return (name, "")
    field = SLOT_FIELD.get(name)
    if field is None:
        return None
    value = operation.get(field)
    if not isinstance(value, str):
        return None
    return ("thread.act" if name in THREAD_ACT_OPS else name, value)


def place(operations: list[Any], operation: dict[str, Any]) -> bool:
    """Add the operation, replacing the one it corrects; report whether it replaced.

    Replacing in place preserves the order `template` established, which is what
    assigns `T<N>` and `G<N>` when the transaction is projected.
    """

    slot = slot_of(operation)
    if slot is not None:
        for index, existing in enumerate(operations):
            if isinstance(existing, dict) and slot_of(existing) == slot:
                operations[index] = operation
                return True
    operations.append(operation)
    return False


def acted_thread_ids(operations: list[Any]) -> set[str]:
    """Return every thread the draft opens or acts on."""

    acted: set[str] = set()
    for operation in operations:
        if not isinstance(operation, dict):
            continue
        name = operation.get("op")
        if name == "thread.open":
            identifier = operation.get("id")
        elif name in THREAD_ACT_OPS:
            identifier = operation.get("thread_id")
        else:
            continue
        if isinstance(identifier, str):
            acted.add(identifier)
    return acted


def next_identifier(prefix: str, recorded: list[str], drafted: list[Any]) -> str:
    """Return the next free `T<N>` or `G<N>` across canonical state and the draft."""

    numbers = [
        int(value[1:])
        for value in [*recorded, *drafted]
        if isinstance(value, str)
        and value.startswith(prefix)
        and value[1:].isdigit()
    ]
    return f"{prefix}{max(numbers, default=0) + 1}"


# --- removal ---


def operation_identity(operation: dict[str, Any]) -> str | None:
    """Return what one drafted operation acts on, or None if it names nothing."""

    target = operation.get("target")
    return next(
        (
            str(operation[field])
            for field in ("thread_id", "id", "gap_id", "check")
            if isinstance(operation.get(field), str)
        ),
        target.get("id") if isinstance(target, dict) else None,
    )


def operation_summary(operation: Any) -> str:
    """Name one drafted operation and what it acts on, in one short line.

    This line is also how `drop` names an operation, so what `show` prints can be
    passed straight back.
    """

    if not isinstance(operation, dict):
        return "invalid operation"
    name = str(operation.get("op"))
    identifier = operation_identity(operation)
    return f"{name} {identifier}" if identifier else name


def passed_checks(operations: list[Any]) -> set[str]:
    """Return every check the draft records as passing."""

    return {
        operation["check"]
        for operation in operations
        if isinstance(operation, dict)
        and operation.get("op") == "check.record"
        and operation.get("result") == "passed"
        and isinstance(operation.get("check"), str)
    }


def unsupported(operations: list[Any]) -> set[int]:
    """Return the operations the rest of the draft no longer supports.

    An act can be made stale by another act rather than by its own fields: a
    note is publishable only while the transaction still touches the thread it
    annotates, and a gap says a check could not be validated, which a later
    record of that same check passing contradicts. Both survive `validate-event`
    and are refused at publish, so they are dropped here instead.
    """

    acted = acted_thread_ids(operations)
    passed = passed_checks(operations)
    doomed: set[int] = set()
    for index, operation in enumerate(operations):
        if not isinstance(operation, dict):
            continue
        name = operation.get("op")
        target = operation.get("target")
        if name == "note.attach" and isinstance(target, dict):
            if isinstance(target.get("id"), str) and target["id"] not in acted:
                doomed.add(index)
        elif name == "gap.open" and operation.get("check") in passed:
            doomed.add(index)
    return doomed


def renumber(draft: Draft) -> None:
    """Close the holes a removal left in the `T<N>` and `G<N>` the draft mints.

    Projection requires each identifier a transaction opens to continue the
    canonical sequence without a hole, so a removal that left one would be
    refused at publish over an identifier no agent ever typed. Notes follow the
    thread they annotate to its new identifier.
    """

    renamed: dict[str, str] = {}
    for prefix, field, name, recorded in (
        ("T", "id", "thread.open", [*draft.open_threads, *draft.resolved_threads]),
        ("G", "gap_id", "gap.open", list(draft.recorded_gaps)),
    ):
        number = int(next_identifier(prefix, recorded, [])[1:])
        for operation in draft.operations:
            if not isinstance(operation, dict) or operation.get("op") != name:
                continue
            current, replacement = operation.get(field), f"{prefix}{number}"
            number += 1
            if isinstance(current, str) and current != replacement:
                renamed[current] = replacement
            operation[field] = replacement
    for operation in draft.operations:
        target = operation.get("target") if isinstance(operation, dict) else None
        if isinstance(target, dict) and target.get("id") in renamed:
            target["id"] = renamed[target["id"]]


def remove(draft: Draft, doomed: set[int]) -> list[str]:
    """Remove the named operations along with everything they leave unsupported.

    This is the one act that reaches operations the agent did not name, so every
    composer path runs it: an act that strands another must not depend on the
    agent noticing.
    """

    removed: list[str] = []
    while doomed:
        removed.extend(
            operation_summary(draft.operations[index]) for index in sorted(doomed)
        )
        draft.operations[:] = [
            operation
            for index, operation in enumerate(draft.operations)
            if index not in doomed
        ]
        doomed = unsupported(draft.operations)
    if removed:
        renumber(draft)
    return removed


# --- entry-time refusal ---


def refuse(errors: list[str]) -> None:
    if errors:
        raise ValueError("; ".join(errors))


def require_valid(
    operations: list[dict[str, Any]], kind: str, occurred: datetime
) -> None:
    """Refuse an operation the format would reject, naming the field at fault."""

    errors: list[str] = []
    for operation in operations:
        review_schema.validate_operation(
            errors, operation, str(operation["op"]), kind, occurred
        )
    refuse(errors)


def read_prose(inline: str | None, path: str | None) -> str | None:
    """Return prose given inline, read from a file, or read from stdin for `-`."""

    if inline is not None:
        return inline
    if path is None:
        return None
    if path == "-":
        return sys.stdin.read().rstrip("\n")
    return Path(path).read_text().rstrip("\n")


def demand_message(args: argparse.Namespace) -> str:
    message = read_prose(args.message, args.message_file)
    if message is None:
        raise ValueError("a message is required: pass --message or --message-file")
    return message


def observation_time(args: argparse.Namespace, stamped: datetime) -> datetime:
    """Return when the evidence was observed, refusing a time after this handoff."""

    if not args.observed_at:
        return stamped
    try:
        observed = datetime.fromisoformat(args.observed_at.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("--observed-at must be an ISO 8601 timestamp") from None
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise ValueError("--observed-at must include a timezone")
    if observed > stamped:
        raise ValueError(
            "--observed-at postdates this transaction; evidence cannot be observed "
            "after the handoff that records it"
        )
    return observed


def build_evidence(
    args: argparse.Namespace, stamped: datetime
) -> Evidence | None:
    """Assemble evidence from typed flags, or None when none was supplied."""

    sanitized = read_prose(args.sanitized_result, args.sanitized_result_file)
    basis, provenance = args.basis, args.provenance
    supplied = (basis, provenance, sanitized)
    if not any(value is not None for value in supplied):
        return None
    if basis is None or provenance is None or sanitized is None:
        raise ValueError(
            "evidence needs --basis, --provenance, and --sanitized-result together"
        )
    return Evidence(
        basis=basis,
        provenance=provenance,
        observed_at=observation_time(args, stamped).isoformat(),
        sanitized_result=sanitized,
        artifact_digest=args.artifact_digest,
    )


def demand_evidence(args: argparse.Namespace, stamped: datetime, label: str) -> Evidence:
    evidence = build_evidence(args, stamped)
    if evidence is None:
        raise ValueError(
            f"{label} requires evidence: pass --basis, --provenance, and "
            "--sanitized-result"
        )
    return evidence


def parse_anchors(values: list[str], paths: tuple) -> tuple:
    """Parse `PATH:START-END` anchors, refusing a path the thread does not name."""

    anchors: list[Anchor] = []
    for value in values:
        path, separator, span = value.rpartition(":")
        start, dash, end = span.partition("-")
        if not separator or not dash or not start.isdigit() or not end.isdigit():
            raise ValueError(f"--anchor must be PATH:START-END, not {value!r}")
        if path not in paths:
            raise ValueError(f"--anchor names {path}, which is not in --paths")
        anchors.append(Anchor(path=path, start_line=int(start), end_line=int(end)))
    return tuple(anchors)


def require_open_thread(draft: Draft, thread_id: str) -> None:
    if thread_id not in draft.open_threads:
        raise ValueError(
            f"{thread_id} is not an open thread; open threads are "
            + (", ".join(draft.open_threads) or "none")
        )


# --- composition commands ---


def compose_open_thread(draft: Draft, args: argparse.Namespace) -> Composition:
    if not args.paths:
        raise ValueError("a thread must name the files it concerns: pass --paths")
    paths = tuple(args.paths)
    thread_id = next_identifier(
        "T",
        [*draft.open_threads, *draft.resolved_threads],
        [
            item.get("id")
            for item in draft.operations
            if isinstance(item, dict) and item.get("op") == "thread.open"
        ],
    )
    operation = ThreadOpen(
        id=thread_id,
        priority=args.priority,
        contract=args.contract,
        title=args.title,
        risk=args.risk,
        required_behavior=args.required_behavior,
        paths=paths,
        evidence=demand_evidence(args, draft.stamped, "a thread"),
        message=read_prose(args.message, args.message_file),
        anchors=parse_anchors(args.anchor, paths) or None,
    )
    return Composition(operations=[operation.as_operation()], target=thread_id)


def compose_reply(draft: Draft, args: argparse.Namespace) -> Composition:
    require_open_thread(draft, args.thread_id)
    blocked = args.decision == "deferred/blocked"
    for name, value in (
        ("--blocker", args.blocker),
        ("--completed-work", args.completed_work),
        ("--remaining-work", args.remaining_work),
        ("--validation-gap", args.validation_gap),
    ):
        if blocked and not value:
            raise ValueError(
                f"a deferred/blocked reply must state {name}, along with the other "
                "three blocked-work fields"
            )
        if not blocked and value:
            raise ValueError(f"{name} applies only to a deferred/blocked reply")
    operation = ThreadReply(
        thread_id=args.thread_id,
        decision=args.decision,
        message=demand_message(args),
        evidence=demand_evidence(args, draft.stamped, "a reply"),
        blocker=args.blocker,
        completed_work=args.completed_work,
        remaining_work=args.remaining_work,
        validation_gap=args.validation_gap,
    )
    return Composition(operations=[operation.as_operation()], target=args.thread_id)


def compose_comment(draft: Draft, args: argparse.Namespace) -> Composition:
    require_open_thread(draft, args.thread_id)
    operation = ThreadComment(
        thread_id=args.thread_id, message=demand_message(args)
    )
    return Composition(operations=[operation.as_operation()], target=args.thread_id)


def compose_reopen(draft: Draft, args: argparse.Namespace) -> Composition:
    if args.thread_id not in draft.resolved_threads:
        raise ValueError(
            f"{args.thread_id} is not a resolved thread; resolved threads are "
            + (", ".join(draft.resolved_threads) or "none")
        )
    operation = ThreadReopen(
        thread_id=args.thread_id, message=demand_message(args)
    )
    return Composition(operations=[operation.as_operation()], target=args.thread_id)


def remaining_open_after_resolve(draft: Draft, thread_id: str) -> set[str]:
    """Return the threads still open once this resolve joins the draft.

    A thread this draft opens counts as open. Projection applies the whole
    transaction before asking whether a `reviewer_update` left anything open, so
    a round that raises a new finding may also resolve the last thread it
    inherited.
    """

    opened: set[str] = set()
    resolved = {thread_id}
    reopened: set[str] = set()
    for item in draft.operations:
        if not isinstance(item, dict):
            continue
        name = item.get("op")
        if name == "thread.open":
            if isinstance(item.get("id"), str):
                opened.add(item["id"])
            continue
        if not isinstance(item.get("thread_id"), str):
            continue
        if name == "thread.resolve":
            resolved.add(item["thread_id"])
        elif name == "thread.reopen":
            reopened.add(item["thread_id"])
    return ((set(draft.open_threads) | opened) - resolved) | reopened


def compose_resolve(draft: Draft, args: argparse.Namespace) -> Composition:
    require_open_thread(draft, args.thread_id)
    if draft.kind == "reviewer_update" and not remaining_open_after_resolve(
        draft, args.thread_id
    ):
        raise ValueError(
            f"resolving {args.thread_id} would leave no thread open, which a "
            "reviewer_update may not do; open this round's thread first and "
            "resolve after, or abort the draft and template final_review instead"
        )
    declined = (
        review_templates.latest_owner_replies(draft.document)
        .get(args.thread_id, {})
        .get("decision")
        == "declined"
    )
    if declined and not args.verified:
        raise ValueError(
            f"{args.thread_id} was declined by the owner, so resolving it requires "
            "independent verification: pass --verified with its evidence"
        )
    if args.verified:
        verification = Verification(
            independent=True,
            evidence=demand_evidence(args, draft.stamped, "--verified"),
        )
    else:
        if build_evidence(args, draft.stamped) is not None:
            raise ValueError("evidence on a resolve belongs to --verified; pass it too")
        verification = None
    operation = ThreadResolve(
        thread_id=args.thread_id,
        message=demand_message(args),
        verification=verification,
    )
    return Composition(operations=[operation.as_operation()], target=args.thread_id)


def compose_open_gap(draft: Draft, args: argparse.Namespace) -> Composition:
    if args.check in passed_checks(draft.operations):
        raise ValueError(
            f"the draft records {args.check} as passed, so it cannot also record "
            "that the check went unvalidated; correct the record with record-check"
        )
    # The gap is this act's own operation, so it is the drop target rather than a
    # second identifier reported beside one.
    operation = gap_open_for(draft, args.check, args.reason, args.material)
    return Composition(operations=[operation.as_operation()], target=operation.gap_id)


def gap_open_for(draft: Draft, check: str, reason: str, material: bool) -> GapOpen:
    """Open a gap for one check, reusing the identifier this draft already gave it.

    Composing the same gap twice is a correction, so it keeps its identifier and
    replaces the earlier entry. Minting a second one would leave a hole in the
    `G<N>` sequence that projection assigns, and the transaction would be
    rejected at publish for a mistake made while typing.
    """

    drafted = [
        item
        for item in draft.operations
        if isinstance(item, dict) and item.get("op") == "gap.open"
    ]
    existing = next(
        (
            item["gap_id"]
            for item in drafted
            if item.get("check") == check and isinstance(item.get("gap_id"), str)
        ),
        None,
    )
    gap_id = existing or next_identifier(
        "G", list(draft.recorded_gaps), [item.get("gap_id") for item in drafted]
    )
    return GapOpen(gap_id=gap_id, check=check, reason=reason, material=material)


def compose_resolve_gap(draft: Draft, args: argparse.Namespace) -> Composition:
    if args.gap_id not in draft.open_gaps:
        raise ValueError(
            f"{args.gap_id} is not an open validation gap; open gaps are "
            + (", ".join(draft.open_gaps) or "none")
        )
    unavailable = args.disposition == "unavailable_non_material"
    unperformed, fail_closed = args.unperformed_check, args.fail_closed_behavior
    if unavailable and not (unperformed and fail_closed):
        raise ValueError(
            "an unavailable_non_material disposition must name --unperformed-check "
            "and --fail-closed-behavior; a gap that is still material stays open"
        )
    if not unavailable and (unperformed or fail_closed):
        raise ValueError(
            "--unperformed-check and --fail-closed-behavior apply only to an "
            "unavailable_non_material disposition"
        )
    operation = GapResolve(
        gap_id=args.gap_id,
        disposition=args.disposition,
        message=demand_message(args),
        evidence=demand_evidence(args, draft.stamped, "a gap resolution"),
        justification=(
            Justification(
                unperformed_check=unperformed, fail_closed_behavior=fail_closed
            )
            if unavailable and unperformed and fail_closed
            else None
        ),
    )
    return Composition(operations=[operation.as_operation()], target=args.gap_id)


def compose_record_check(draft: Draft, args: argparse.Namespace) -> Composition:
    """Record one check, opening the material gap a failure owes in the same act.

    The act is named by the check, not by the gap it may have opened: `show` prints and
    `drop` accepts `check.record <check>`, so reporting the gap ID as the target would
    print an acknowledgment no `drop` could act on. The gap is reported as `gap_id`.
    """

    reason = args.gap_reason
    failed = args.result == "failed"
    if failed and not reason:
        raise ValueError(
            "a failed check must open the material validation gap it produced: pass "
            "--gap-reason to record both in one act"
        )
    if not failed and reason:
        raise ValueError("--gap-reason applies only to a failed check")
    check = CheckRecord(
        check=args.check,
        result=args.result,
        evidence=build_evidence(args, draft.stamped),
    )
    if not failed:
        return Composition(operations=[check.as_operation()], target=args.check)
    # The guards above make a failed result and a gap reason inseparable.
    gap = gap_open_for(draft, args.check, reason, True)
    return Composition(
        operations=[check.as_operation(), gap.as_operation()],
        target=args.check,
        gap_id=gap.gap_id,
    )


def compose_note(draft: Draft, args: argparse.Namespace) -> Composition:
    if args.thread_id not in acted_thread_ids(draft.operations):
        raise ValueError(
            f"draft has no entry for thread {args.thread_id}; compose the act this "
            "note annotates first"
        )
    operation = NoteAttach(
        target=NoteTarget(kind="thread", id=args.thread_id),
        tag=args.tag,
        message=demand_message(args),
    )
    return Composition(operations=[operation.as_operation()], target=args.thread_id)


def compose_replace_source(draft: Draft, args: argparse.Namespace) -> Composition:
    snapshot = draft.value.get("source_snapshot")
    if not isinstance(snapshot, dict):
        raise TypeError(
            "the draft carries no guarded source snapshot; template it again under "
            "the lease"
        )
    # The replacement basis is the snapshot the guard pinned, never one an agent
    # supplies, so the transaction cannot claim a basis the tools did not observe.
    operation = SourceReplace(snapshot=snapshot, reason=args.reason)
    return Composition(operations=[operation.as_operation()], target=None)


def compose_approve(draft: Draft, args: argparse.Namespace) -> Composition:
    prefilled = next(
        (
            item.get("structure_debt")
            for item in draft.operations
            if isinstance(item, dict) and item.get("op") == "review.approve"
        ),
        None,
    )
    flagged = prefilled.get("flagged_paths") if isinstance(prefilled, dict) else None
    disposition, structure_message = args.structure_disposition, args.structure_message
    if flagged:
        if not (disposition and structure_message):
            raise ValueError(
                "the accretion ledger flagged "
                + ", ".join(flagged)
                + "; the approval must carry --structure-disposition and "
                "--structure-message"
            )
        debt = StructureDebt(
            disposition=disposition,
            flagged_paths=tuple(sorted(flagged)),
            message=structure_message,
        )
    else:
        if disposition or structure_message:
            raise ValueError(
                "no guarded file is accretion-flagged, so this approval records no "
                "structure debt"
            )
        debt = None
    operation = ReviewApprove(decision=args.decision, structure_debt=debt)
    return Composition(operations=[operation.as_operation()], target=None)


COMPOSERS = {
    "open-thread": compose_open_thread,
    "reply": compose_reply,
    "comment": compose_comment,
    "resolve": compose_resolve,
    "reopen": compose_reopen,
    "open-gap": compose_open_gap,
    "resolve-gap": compose_resolve_gap,
    "record-check": compose_record_check,
    "note": compose_note,
    "replace-source": compose_replace_source,
    "approve": compose_approve,
}


# --- envelope metadata and inspection ---


def git_lines(repo: str, *arguments: str) -> list[str]:
    completed = subprocess.run(
        ["git", "-C", repo, *arguments],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError(
            f"git {' '.join(arguments)} failed: {completed.stderr.strip()}"
        )
    return [line for line in completed.stdout.splitlines() if line]


def reply_context(args: argparse.Namespace) -> int:
    """Record the owner's two prose assessments and derive the work they describe.

    `changed_files` and `revisions` are read from the repository rather than
    retyped: they are facts about the tree between the starting snapshot and now,
    and an agent transcribing them can only get them wrong.
    """

    draft = open_draft(args)
    if draft.kind != "owner_reply":
        raise ValueError("reply context belongs to an owner_reply transaction")
    starting = draft.value.get("starting_source_snapshot")
    revision = starting.get("revision") if isinstance(starting, dict) else None
    if not isinstance(revision, str) or not revision:
        raise ValueError(
            "the draft records no starting revision; template it again under the lease"
        )
    drift = read_prose(args.drift, args.drift_file)
    guide = read_prose(args.guide, args.guide_file)
    if not drift or not guide:
        raise ValueError(
            "an owner reply states both assessments: pass --drift and --guide, or "
            "their file forms"
        )
    changed = {
        *git_lines(args.repo, "diff", "--name-only", f"{revision}..HEAD"),
        *git_lines(args.repo, "diff", "--name-only", "--cached"),
        *git_lines(args.repo, "diff", "--name-only"),
        *git_lines(args.repo, "ls-files", "--others", "--exclude-standard"),
    }
    draft.value["source_drift_assessment"] = drift
    draft.value["guide_synchronization"] = guide
    draft.value["changed_files"] = sorted(changed)
    draft.value["revisions"] = git_lines(args.repo, "rev-list", f"{revision}..HEAD")
    record(draft)
    print(
        json.dumps(
            {
                "changed_files": len(draft.value["changed_files"]),
                "outstanding": len(review_schema.validate_event(draft.value)),
                "revisions": len(draft.value["revisions"]),
                "status": "recorded",
            },
            sort_keys=True,
        )
    )
    return 0


def show(args: argparse.Namespace) -> int:
    """Report what the draft carries and what it still owes, without dumping it.

    `outstanding` and `schema_valid` cover the event format alone. The
    transaction rules that read canonical history — one reply per open thread,
    identifier continuity — belong to the projection, which the composer does
    not import, so `publish` is where a draft that is well-formed but does not
    answer its history is refused.
    """

    draft = open_draft(args)
    outstanding = review_schema.validate_event(draft.value)
    print(
        json.dumps(
            {
                "kind": draft.kind,
                "operations": [
                    operation_summary(item) for item in draft.operations
                ],
                "outstanding": outstanding,
                "schema_valid": not outstanding,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def composed_operations(draft: Draft, args: argparse.Namespace) -> Composition:
    """Build and check the operations one subcommand contributes to the draft.

    Everything a command refuses is refused here, before the draft is touched, so
    a rejected act leaves the draft exactly as it was.
    """

    composition = COMPOSERS[args.command](draft, args)
    require_valid(composition.operations, draft.kind, draft.stamped)
    return composition


def selection(draft: Draft, name: str, target: str) -> set[int]:
    """Return the operations `OP TARGET` names, refusing what may not be dropped."""

    if name in PREFILLED_OPS:
        raise ValueError(
            f"{name} carries what templating derived from the guard, which composing "
            "it again would not restore; correct it with its own subcommand instead"
        )
    chosen = {
        index
        for index, operation in enumerate(draft.operations)
        if isinstance(operation, dict)
        and operation.get("op") == name
        and operation_identity(operation) == target
    }
    if not chosen:
        carried = ", ".join(
            operation_summary(operation) for operation in draft.operations
        )
        raise ValueError(
            f"the draft carries no {name} {target} to drop; it carries "
            + (carried or "no operations")
        )
    return chosen


def acknowledge(
    draft: Draft,
    name: str,
    target: str | None,
    status: str,
    dropped: int,
    gap_id: str | None = None,
) -> None:
    """Print what one act changed, in the fixed shape every subcommand reports.

    `op` and `target` together are what `drop` accepts, so an acknowledgment can be
    handed straight back. `gap_id` is null unless the act also opened a gap.
    """

    print(
        json.dumps(
            {
                "dropped": dropped,
                "gap_id": gap_id,
                "op": name,
                "outstanding": len(review_schema.validate_event(draft.value)),
                "status": status,
                "target": target,
            },
            sort_keys=True,
        )
    )


def record(draft: Draft) -> None:
    """Write the draft back under the instant the act it now carries was composed."""

    # The draft's own timestamp advances with the act it now records, so evidence
    # observed while composing can never postdate the transaction carrying it.
    draft.value["occurred_at"] = draft.stamped.isoformat()
    secure_json(draft.path, draft.value)


def drop(args: argparse.Namespace) -> int:
    """Remove one drafted act under the lease, with whatever it leaves stranded."""

    draft = open_draft(args)
    removed = remove(draft, selection(draft, args.op, args.target))
    record(draft)
    acknowledge(draft, args.op, args.target, "dropped", len(removed))
    return 0


def compose(args: argparse.Namespace) -> int:
    """Compose one act into the draft under the lease and acknowledge it compactly."""

    draft = open_draft(args)
    composition = composed_operations(draft, args)
    replaced = False
    for operation in composition.operations:
        replaced = place(draft.operations, operation) or replaced
    removed = remove(draft, unsupported(draft.operations))
    record(draft)
    acknowledge(
        draft,
        composition.operations[0]["op"],
        composition.target,
        "replaced" if replaced else "recorded",
        len(removed),
        gap_id=composition.gap_id,
    )
    return 0


# --- command model ---


def add_message_arguments(parser: argparse.ArgumentParser, *, required: bool) -> None:
    group = parser.add_mutually_exclusive_group(required=required)
    group.add_argument("--message", help="Prose body, given inline")
    group.add_argument(
        "--message-file", help="Prose body read from this file, or from stdin for -"
    )


def add_evidence_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--basis", choices=sorted(review_schema.EVIDENCE_BASES))
    parser.add_argument("--provenance", help="What was examined, named exactly")
    parser.add_argument("--sanitized-result")
    parser.add_argument("--sanitized-result-file")
    parser.add_argument("--artifact-digest", help="Lowercase SHA-256 of the artifact")
    parser.add_argument(
        "--observed-at",
        help="When the evidence was observed; defaults to now and may not postdate it",
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the composer's complete command model."""

    parser = argparse.ArgumentParser(prog="review_compose.py")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in (*COMPOSERS, "drop", "reply-context", "show"):
        child = commands.add_parser(name)
        child.add_argument("--repo", required=True)
        child.add_argument("--review", required=True)
        child.add_argument("--event", required=True)
        child.add_argument("--lease", required=True)
        child.add_argument("--guard", required=True)
        child.add_argument("--lock-script", required=True)

    open_thread = commands.choices["open-thread"]
    open_thread.add_argument(
        "--priority", choices=sorted(review_schema.THREAD_PRIORITIES), default="P1"
    )
    open_thread.add_argument(
        "--contract", choices=("internal", "external"), default="internal"
    )
    open_thread.add_argument("--title", required=True)
    open_thread.add_argument("--risk", required=True)
    open_thread.add_argument("--required-behavior", required=True)
    open_thread.add_argument("--paths", nargs="+", required=True)
    open_thread.add_argument(
        "--anchor",
        action="append",
        default=[],
        help="Line range against the guarded revision, as PATH:START-END",
    )
    add_message_arguments(open_thread, required=False)
    add_evidence_arguments(open_thread)

    reply = commands.choices["reply"]
    reply.add_argument("thread_id")
    reply.add_argument(
        "--decision", choices=sorted(review_schema.THREAD_DECISIONS), required=True
    )
    reply.add_argument("--blocker")
    reply.add_argument("--completed-work")
    reply.add_argument("--remaining-work")
    reply.add_argument("--validation-gap")
    add_message_arguments(reply, required=True)
    add_evidence_arguments(reply)

    for name in ("comment", "reopen"):
        child = commands.choices[name]
        child.add_argument("thread_id")
        add_message_arguments(child, required=True)

    resolve = commands.choices["resolve"]
    resolve.add_argument("thread_id")
    resolve.add_argument(
        "--verified",
        action="store_true",
        help="Record independent verification; requires its own evidence",
    )
    add_message_arguments(resolve, required=True)
    add_evidence_arguments(resolve)

    open_gap = commands.choices["open-gap"]
    open_gap.add_argument("--check", required=True)
    open_gap.add_argument("--reason", required=True)
    open_gap.add_argument("--material", action="store_true")

    resolve_gap = commands.choices["resolve-gap"]
    resolve_gap.add_argument("gap_id")
    resolve_gap.add_argument(
        "--disposition",
        choices=sorted(review_schema.GAP_DISPOSITIONS),
        required=True,
    )
    resolve_gap.add_argument("--unperformed-check")
    resolve_gap.add_argument("--fail-closed-behavior")
    add_message_arguments(resolve_gap, required=True)
    add_evidence_arguments(resolve_gap)

    record_check = commands.choices["record-check"]
    record_check.add_argument("--check", required=True)
    record_check.add_argument("--result", choices=("passed", "failed"), required=True)
    record_check.add_argument(
        "--gap-reason",
        help="Why the failed check leaves a material gap; required when it failed",
    )
    add_evidence_arguments(record_check)

    note = commands.choices["note"]
    note.add_argument("thread_id")
    note.add_argument("--tag", choices=NOTE_TAGS, required=True)
    add_message_arguments(note, required=True)

    commands.choices["replace-source"].add_argument("--reason", required=True)

    approve = commands.choices["approve"]
    approve.add_argument("--decision", required=True)
    approve.add_argument(
        "--structure-disposition",
        choices=sorted(review_schema.STRUCTURE_DEBT_DISPOSITIONS),
    )
    approve.add_argument("--structure-message")

    removal = commands.choices["drop"]
    removal.add_argument("op", help="Operation name, exactly as `show` prints it")
    removal.add_argument(
        "target", help="What that operation acts on, as `show` prints it"
    )

    context = commands.choices["reply-context"]
    context.add_argument("--drift", help="How the guarded source moved during the reply")
    context.add_argument("--drift-file")
    context.add_argument("--guide", help="How project guides were kept in step")
    context.add_argument("--guide-file")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "show":
            return show(args)
        if args.command == "drop":
            return drop(args)
        if args.command == "reply-context":
            return reply_context(args)
        return compose(args)
    except (OSError, TypeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
