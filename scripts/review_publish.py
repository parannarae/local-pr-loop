#!/usr/bin/env python3
"""Commit and recover review publications around one atomic canonical replace."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple

import review_card
import review_ledger
import review_scope
from review_contract import TIMEOUT_DURATION_BY_KIND
from review_io import atomic_bytes, atomic_json, load_object, load_secure_object


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def import_state(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location("local_pr_loop_state", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import state helper: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def canonical_has_event(document: dict[str, Any], event_id: str) -> bool:
    history = document.get("history")
    return isinstance(history, list) and any(
        isinstance(event, dict) and event.get("event_id") == event_id
        for event in history
    )


TIMEOUT_EVENT_KINDS = ("owner_timeout", "reviewer_timeout", "initial_review_timeout")


def unpublishable_draft_reason(event_path: Path) -> str | None:
    """Return why a draft can never be published as written, or None if it may retry.

    A timeout draft fixes ``occurred_at`` when it is templated. One stamped before its own
    deadline stays invalid no matter how long the caller waits, so telling the caller to
    republish it is advice that cannot succeed.

    A draft this function cannot read or parse also returns None, deliberately: the
    caller is about to report a failure whose own message names the real problem, and
    claiming the draft is unpublishable would replace that with a guess.
    """

    try:
        draft = load_object(event_path)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if draft.get("kind") not in TIMEOUT_EVENT_KINDS:
        return None
    occurred_at = draft.get("occurred_at")
    operations = draft.get("operations")
    declaration = next(
        (
            operation
            for operation in (operations if isinstance(operations, list) else [])
            if isinstance(operation, dict)
            and operation.get("op") == "timeout.declare"
        ),
        {},
    )
    deadline = declaration.get("deadline")
    if not isinstance(occurred_at, str) or not isinstance(deadline, str):
        return None
    try:
        stamped_early = datetime.fromisoformat(occurred_at) < datetime.fromisoformat(
            deadline
        )
    except ValueError:
        return None
    if not stamped_early:
        return None
    return (
        "the timeout draft is stamped before its own deadline, so republishing it can "
        "never succeed"
    )


def recorded_snapshot(document: dict[str, Any], state: Any) -> dict[str, Any] | None:
    """Return the most recent guarded snapshot recorded in canonical history."""

    for event in reversed(document.get("history") or []):
        if not isinstance(event, dict):
            continue
        field = state.source_field_for(event.get("kind"))
        value = event.get(field) if field else None
        if isinstance(value, dict):
            return value
    return None


def digest_by_path(entries: Any) -> dict[str, str]:
    if not isinstance(entries, list):
        return {}
    return {
        entry["path"]: str(entry.get("sha256", ""))
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("path"), str)
    }


def compare_digests(
    recorded: dict[str, str], current: dict[str, str], kind: str
) -> list[dict[str, str]]:
    """Report added, removed, and modified paths between two digest maps.

    `kind` is not part of the comparison: it is the label stamped on every change
    so a caller can tell which half of the snapshot the path came from.
    """
    changes = []
    for path in sorted(set(recorded) | set(current)):
        if path not in current:
            change = "removed"
        elif path not in recorded:
            change = "added"
        elif recorded[path] != current[path]:
            change = "modified"
        else:
            continue
        changes.append({"path": path, "change": change, "kind": kind})
    return changes


def source_drift_detail(
    recorded: dict[str, Any] | None, current: dict[str, Any] | None
) -> dict[str, Any]:
    """Say which parts of the guarded source moved, without exposing file contents.

    Digested entries are compared per path. Tracked changes are only available as one
    aggregate diff digest, so they are reported as an aggregate rather than guessed at.
    """

    if not isinstance(recorded, dict) or not isinstance(current, dict):
        return {
            "available": False,
            "changed_paths": [],
            "scope_changes": {},
            "revision_changed": False,
            "tracked_diff_changed": False,
        }

    changed_paths = compare_digests(
        digest_by_path(recorded.get("untracked")),
        digest_by_path(current.get("untracked")),
        "untracked",
    ) + compare_digests(
        digest_by_path(recorded.get("additional_inputs")),
        digest_by_path(current.get("additional_inputs")),
        "additional_input",
    )

    scope_changes: dict[str, Any] = {}
    for field in ("scope", "exclusions"):
        before = [value for value in (recorded.get(field) or []) if isinstance(value, str)]
        after = [value for value in (current.get(field) or []) if isinstance(value, str)]
        if before != after:
            scope_changes[field] = {
                "added": sorted(set(after) - set(before)),
                "removed": sorted(set(before) - set(after)),
            }

    return {
        "available": True,
        "changed_paths": changed_paths,
        "scope_changes": scope_changes,
        "revision_changed": recorded.get("revision") != current.get("revision"),
        "tracked_diff_changed": (
            recorded.get("staged_sha256") != current.get("staged_sha256")
            or recorded.get("unstaged_sha256") != current.get("unstaged_sha256")
        ),
    }


def terminal_outcome(review: Path) -> str | None:
    """Return the recorded terminal outcome of a review, or None while it is open."""

    try:
        document = load_object(review)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    # Read defensively: this runs inside the cleanup handler that must report commit
    # state on every path, and `publish` is the first reader of a canonical file no
    # command validated, so a hand-edited `state` must not escape as an exception.
    state = document.get("state")
    terminal = state.get("terminal") if isinstance(state, dict) else None
    if isinstance(terminal, dict) and isinstance(terminal.get("outcome"), str):
        return terminal["outcome"]
    return None


def result_with_canonical_digest(
    *,
    status: str,
    committed: bool,
    review: Path,
    event_id: str | None,
    lock_state: str,
    recovery_action: str,
    detail: str | None = None,
) -> dict[str, Any]:
    """Build one structured command result, reading and hashing the canonical file.

    `canonical_sha256` is None when that file is absent, which is a real state a
    retirement leaves behind; the caller reads `committed` to learn the outcome and
    the digest only to compare against a receipt.
    """

    value: dict[str, Any] = {
        "status": status,
        "committed": committed,
        "canonical_sha256": sha256(review) if review.is_file() else None,
        "event_id": event_id,
        "lock_state": lock_state,
        "recovery_action": recovery_action,
    }
    if detail:
        value["detail"] = detail
    return value


def release_lock(lock_script: Path, repo: Path, review: Path, token: str) -> None:
    try:
        subprocess.run(
            [
                sys.executable,
                str(lock_script),
                "release",
                "--repo",
                str(repo),
                "--review-file",
                str(review),
                "--token",
                token,
            ],
            check=True,
        )
    except subprocess.CalledProcessError:
        # CalledProcessError includes the full argv, which contains the token.
        raise RuntimeError("review lock release failed") from None


def verify_lock(lock_script: Path, repo: Path, review: Path, token: str) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(lock_script),
            "verify",
            "--repo",
            str(repo),
            "--review-file",
            str(review),
            "--token",
            token,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("review lock verification failed")


def lock_status(lock_script: Path, repo: Path, review: Path) -> str:
    completed = subprocess.run(
        [
            sys.executable,
            str(lock_script),
            "status",
            "--repo",
            str(repo),
            "--review-file",
            str(review),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("cannot determine review lock state")
    return completed.stdout.strip()


def current_snapshot(
    repo: str, snapshot_script: str, declaration: dict[str, list[str]] | None
) -> dict[str, Any]:
    """Take the source snapshot this publication is checked against."""

    if declaration is None:
        raise ValueError(
            "no scope declaration is available; supply --scope-json or publish under an "
            "inspection guard"
        )
    completed = subprocess.run(
        [
            sys.executable,
            snapshot_script,
            *review_scope.snapshot_arguments(repo, declaration),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"source snapshot failed: {completed.stderr.strip()}")
    value = json.loads(completed.stdout)
    if not isinstance(value, dict):
        raise TypeError("source snapshot must be a JSON object")
    return value


def validate_receipt(receipt: dict[str, Any], state: Any) -> None:
    allowed = {
        "format",
        "format_revision",
        "event_id",
        "draft_digest",
        "base_canonical_sha256",
        "intended_canonical_sha256",
        "source_fingerprint",
        "commit_phase",
    }
    if set(receipt) != allowed:
        raise ValueError("publication receipt fields are invalid")
    if (
        receipt["format"] != state.FORMAT
        or receipt["format_revision"] != state.FORMAT_REVISION
    ):
        raise ValueError("publication receipt format is unsupported")
    if not isinstance(
        receipt.get("event_id"), str
    ) or not state.EVENT_ID_PATTERN.fullmatch(receipt["event_id"]):
        raise ValueError("publication receipt event_id is invalid")
    for key in (
        "draft_digest",
        "base_canonical_sha256",
        "intended_canonical_sha256",
        "source_fingerprint",
    ):
        value = receipt.get(key)
        if not isinstance(value, str) or not state.SHA256_PATTERN.fullmatch(value):
            raise ValueError(f"publication receipt {key} is invalid")
    if receipt.get("commit_phase") not in {"prepared", "canonical_committed"}:
        raise ValueError("publication receipt commit_phase is invalid")


def lease_token(lease: dict[str, Any]) -> str:
    """Return the lease's lock token, refusing a lease that records none.

    The lease is a 0600 file this skill wrote, so a missing or non-string token
    means corruption; failing here names the artifact instead of surfacing as a
    lock mismatch later.
    """
    token = lease.get("token")
    if not isinstance(token, str) or not token:
        raise ValueError("lease records no usable token; re-acquire the lock")
    return token


def guard_digest(state: Any, value: Any, field: str) -> str:
    """Return one SHA-256 field of the inspection guard, refusing a corrupt one.

    The guard is a 0600 file this skill wrote, so a malformed digest means corruption.
    Naming the guard here is what keeps a later comparison from blaming canonical
    history for it, following `lease_token` and its stated reason.
    """

    if not isinstance(value, str) or not state.SHA256_PATTERN.fullmatch(value):
        raise ValueError(
            f"inspection guard {field} is not a SHA-256 digest; release the lock and "
            "re-run inspect to recreate it"
        )
    return value


# A NamedTuple rather than a frozen dataclass, deliberately: this module is loaded
# under an alias by several test harnesses, which leaves it absent from `sys.modules`,
# and `dataclasses` needs the defining module registered there to build the class.
# NamedTuple gives the same immutable named record without that lookup.
class PublicationExpectations(NamedTuple):
    """What a publication must match before it may commit.

    Every field is resolved once, from the guard when one exists and from the caller's
    own flags otherwise, so the comparison sites never have to ask which source won.
    """

    token: str
    review_sha256: str | None
    source_fingerprint: str | None
    scope_declaration: dict[str, list[str]] | None


def resolve_expectations(
    args: argparse.Namespace, state: Any
) -> PublicationExpectations:
    """Resolve one publication's expectations from its lease and guard, or its flags.

    A stored guard is authoritative wherever it exists: it recorded the canonical
    digest, the source fingerprint, and the scope the inspection actually observed.
    """

    if not args.lease:
        return PublicationExpectations(
            token=args.token,
            review_sha256=args.expected_review_sha,
            source_fingerprint=args.expected_source_fingerprint,
            scope_declaration=(
                review_scope.validate(json.loads(args.scope_json))
                if args.scope_json
                else None
            ),
        )
    lease = load_secure_object(Path(args.lease), "lease")
    guard = load_secure_object(Path(args.guard), "inspection guard")
    snapshot = guard.get("source_snapshot")
    if not isinstance(snapshot, dict):
        raise TypeError("inspection guard source snapshot is invalid")
    if "scope" not in guard:
        raise ValueError(
            "inspection guard predates this version and records no structured "
            "scope; release the lock and re-run inspect to recreate it"
        )
    return PublicationExpectations(
        token=lease_token(lease),
        review_sha256=guard_digest(state, guard.get("review_sha256"), "review_sha256"),
        source_fingerprint=guard_digest(
            state, snapshot.get("fingerprint"), "source_snapshot.fingerprint"
        ),
        scope_declaration=review_scope.validate(guard.get("scope")),
    )


def snapshot_path_list(snapshot: dict[str, Any], field: str) -> list[str]:
    """Return a snapshot's `scope` or `exclusions`, refusing a malformed value.

    Defaulting either to an empty list is not neutral for the accretion ledger: an
    empty scope matches no thread while an empty exclusion list diffs the whole
    repository, so the two halves of the flagged set would move in opposite
    directions over the same corrupt snapshot.
    """

    value = snapshot.get(field)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"guarded source snapshot {field} must be a list of strings")
    return value


def write_report(state: Any, document: dict[str, Any], report: Path) -> None:
    atomic_bytes(report, state.render_report(document).encode())


def publish(args: argparse.Namespace) -> int:
    """Append the leased draft to canonical history through one commit point.

    Every path prints one structured result whose `committed` field is the only
    safe way to read the outcome: once it is true the event is recorded and must
    never be republished, and any remaining work is cleanup `recover` finishes.
    """
    review = Path(args.review)
    event_path = Path(args.event)
    report = Path(args.report)
    journal = Path(args.journal)
    state = import_state(Path(args.state_script))
    event_id: str | None = None
    committed = False
    try:
        expectations = resolve_expectations(args, state)
        verify_lock(
            Path(args.lock_script), Path(args.repo), review, expectations.token
        )
        document = load_object(review)
        validation_errors = state.validate_document(document)
        if validation_errors:
            raise ValueError("; ".join(validation_errors))
        event = load_object(event_path)
        event_id = event.get("event_id")
        if not isinstance(event_id, str):
            raise TypeError("event_id is required")
        base_sha = sha256(review)
        if base_sha != expectations.review_sha256:
            raise ValueError("canonical SHA does not match expected review SHA")
        snapshot = current_snapshot(
            args.repo, args.snapshot_script, expectations.scope_declaration
        )
        if snapshot.get("fingerprint") != expectations.source_fingerprint:
            raise ValueError("source fingerprint does not match expected fingerprint")
        event_snapshot_field = state.source_field_for(event.get("kind"))
        if event_snapshot_field:
            event_snapshot = event.get(event_snapshot_field)
            if state.snapshot_identity(event_snapshot) != state.snapshot_identity(
                snapshot
            ):
                raise ValueError("event snapshot does not match guarded source")
        if event.get("kind") == "final_review":
            # Enforced here, where the fingerprint checks above pinned the guarded
            # tree, so the flagged set the acknowledgment is compared against is the
            # one the reviewer templated from.
            flagged = review_ledger.flagged_paths(
                args.repo,
                document,
                snapshot_path_list(snapshot, "scope"),
                snapshot_path_list(snapshot, "exclusions"),
            )
            acknowledgment = review_ledger.acknowledgment_error(
                document, event, flagged
            )
            if acknowledgment:
                raise ValueError(acknowledgment)
        draft_sha = sha256(event_path)
        updated = state.append_event(document, event)
        validation_errors = state.validate_document(updated)
        if validation_errors:
            raise ValueError("; ".join(validation_errors))
        canonical_bytes = (json.dumps(updated, indent=2) + "\n").encode()
        intended_sha = hashlib.sha256(canonical_bytes).hexdigest()
        receipt = {
            "format": state.FORMAT,
            "format_revision": state.FORMAT_REVISION,
            "event_id": event_id,
            "draft_digest": draft_sha,
            "base_canonical_sha256": base_sha,
            "intended_canonical_sha256": intended_sha,
            "source_fingerprint": updated["state"]["source_fingerprint"],
            "commit_phase": "prepared",
        }
        atomic_json(journal, receipt)

        # Recheck ownership at the transaction boundary. A direct helper call
        # and a lock change between preflight and commit are both rejected.
        verify_lock(
            Path(args.lock_script), Path(args.repo), review, expectations.token
        )
        if sha256(review) != base_sha:
            raise ValueError("canonical JSON changed before commit")
        # This replace is the one and only publication commit point.
        atomic_bytes(review, canonical_bytes)
        committed = True
        receipt["commit_phase"] = "canonical_committed"
        atomic_json(journal, receipt)
        write_report(state, updated, report)
        if event_path.is_file() and sha256(event_path) == draft_sha:
            event_path.unlink()
        release_lock(
            Path(args.lock_script), Path(args.repo), review, expectations.token
        )
        journal.unlink()
        if args.lease:
            Path(args.lease).unlink()
            Path(args.guard).unlink(missing_ok=True)
        print(
            json.dumps(
                result_with_canonical_digest(
                    status="published",
                    committed=True,
                    review=review,
                    event_id=event_id,
                    lock_state="unlocked",
                    recovery_action="none",
                ),
                sort_keys=True,
            )
        )
        return 0
    # The command boundary must report commit state even for an unexpected
    # cleanup defect; canonical history is re-read below before classification.
    except Exception as error:  # noqa: BLE001
        if event_id and review.is_file():
            try:
                committed = canonical_has_event(load_object(review), event_id)
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                pass
        status = "published_cleanup_required" if committed else "precommit_failed"
        detail = str(error)
        if committed:
            action = (
                "inspect canonical state, then run recover-publish; do not retry the old SHA"
            )
            # The commit fact has to arrive before the error, or the author reads a
            # cleanup problem as a failed publication and republishes work already recorded.
            detail = (
                f"the event is already recorded in canonical history and must not be "
                f"published again; only cleanup after the commit failed: {detail}"
            )
        elif journal.exists():
            action = (
                "run recover-publish with the lock token to abort any prepared receipt, "
                "then reinspect before publishing"
            )
        else:
            unpublishable = unpublishable_draft_reason(event_path)
            if unpublishable:
                action = (
                    "run abort-draft, then template the timeout again once its deadline "
                    "has passed"
                )
                detail = f"{unpublishable}: {detail}"
            else:
                action = "fix the reported problem and publish the unchanged draft again"
            outcome = terminal_outcome(review)
            if outcome:
                detail = (
                    f"this review is already terminal with outcome {outcome} and accepts "
                    f"no further events: {detail}"
                )
        print(
            json.dumps(
                result_with_canonical_digest(
                    status=status,
                    committed=committed,
                    review=review,
                    event_id=event_id,
                    lock_state="held_or_unknown",
                    recovery_action=action,
                    detail=detail,
                ),
                sort_keys=True,
            )
        )
        return 1


def recover_without_receipt(
    args: argparse.Namespace,
    review: Path,
    event_path: Path,
    report: Path,
    state: Any,
) -> int:
    """Describe canonical reality when no publication receipt remains.

    A receipt is scratch state that successful cleanup deletes, so its absence usually
    means the publication completed rather than that anything needs repair. Reporting a
    missing file here reads as a failure and invites a retry that cannot help.
    """

    document = load_object(review)
    errors = state.validate_document(document)
    if errors:
        raise ValueError("; ".join(errors))

    lock_state = lock_status(Path(args.lock_script), Path(args.repo), review)
    draft_present = event_path.exists()
    if draft_present or lock_state != "unlocked":
        blockers = []
        if draft_present:
            blockers.append("a draft is present")
        if lock_state != "unlocked":
            blockers.append("the review is locked")
        print(
            json.dumps(
                result_with_canonical_digest(
                    status="nothing_to_recover",
                    committed=False,
                    review=review,
                    event_id=None,
                    lock_state=lock_state,
                    recovery_action=(
                        "publish or abort the draft, and release the lock when finished"
                        if draft_present
                        else "release the lock when finished"
                    ),
                    detail="; ".join(blockers),
                ),
                sort_keys=True,
            )
        )
        return 0

    # No receipt, no draft, no lock: keep the report current and say so plainly. The
    # event that was published is deliberately not named, because without a receipt this
    # command cannot know which publication the caller meant.
    write_report(state, document, report)
    print(
        json.dumps(
            result_with_canonical_digest(
                status="already_clean",
                committed=False,
                review=review,
                event_id=None,
                lock_state=lock_state,
                recovery_action="none",
                detail="no publication receipt remains; nothing to recover",
            ),
            sort_keys=True,
        )
    )
    return 0


def recover(args: argparse.Namespace) -> int:
    """Settle whatever a publication left outstanding, without ever committing.

    A receipt whose event reached canonical history has only the report, draft,
    and receipt cleanup left, so a publication already finished settles as a
    successful no-op. A receipt still in preparation is discarded instead, which
    leaves canonical history untouched and the draft in place to publish again.
    """
    review = Path(args.review)
    event_path = Path(args.event)
    report = Path(args.report)
    journal = Path(args.journal)
    state = import_state(Path(args.state_script))
    event_id: str | None = None
    committed = False
    try:
        if args.lease and Path(args.lease).exists():
            lease = load_secure_object(Path(args.lease), "lease")
            args.token = lease_token(lease)
        if not journal.exists():
            return recover_without_receipt(args, review, event_path, report, state)
        receipt = load_object(journal)
        validate_receipt(receipt, state)
        event_id = receipt.get("event_id")
        if not isinstance(event_id, str):
            raise TypeError("publication journal has no event_id")
        document = load_object(review)
        errors = state.validate_document(document)
        if errors:
            raise ValueError("; ".join(errors))
        phase = receipt["commit_phase"]
        present = canonical_has_event(document, event_id)
        if phase == "prepared" and not present:
            if not args.token:
                raise ValueError(
                    "prepared publication requires its lock token to abort"
                )
            verify_lock(Path(args.lock_script), Path(args.repo), review, args.token)
            if sha256(review) != receipt["base_canonical_sha256"]:
                raise ValueError("canonical SHA changed after publication preparation")
            if not event_path.is_file() or event_path.is_symlink():
                raise ValueError("prepared publication draft is unavailable")
            if sha256(event_path) != receipt["draft_digest"]:
                raise ValueError("prepared publication draft digest changed")
            journal.unlink()
            print(
                json.dumps(
                    result_with_canonical_digest(
                        status="prepared_publication_aborted",
                        committed=False,
                        review=review,
                        event_id=event_id,
                        lock_state="held",
                        recovery_action="reinspect source and canonical state before publishing",
                    ),
                    sort_keys=True,
                )
            )
            return 0
        if not present:
            raise ValueError("receipt event is not committed in canonical history")
        committed = True
        intended = receipt.get("intended_canonical_sha256")
        if sha256(review) != intended:
            raise ValueError("canonical SHA does not match the publication receipt")
        if args.token:
            verify_lock(Path(args.lock_script), Path(args.repo), review, args.token)
        elif lock_status(Path(args.lock_script), Path(args.repo), review) != "unlocked":
            raise ValueError("active lock requires its token for recovery")
        write_report(state, document, report)
        draft_digest = receipt.get("draft_digest")
        if event_path.exists():
            if not event_path.is_file() or event_path.is_symlink():
                raise ValueError("draft path is not a regular file")
            if sha256(event_path) != draft_digest:
                raise ValueError("draft differs from the committed publication")
            event_path.unlink()
        lock_state = "unlocked"
        if args.token:
            release_lock(Path(args.lock_script), Path(args.repo), review, args.token)
        journal.unlink()
        if args.lease and args.token:
            Path(args.lease).unlink(missing_ok=True)
            Path(args.guard).unlink(missing_ok=True)
        print(
            json.dumps(
                result_with_canonical_digest(
                    status="recovered",
                    committed=True,
                    review=review,
                    event_id=event_id,
                    lock_state=lock_state,
                    recovery_action="none",
                ),
                sort_keys=True,
            )
        )
        return 0
    # Recovery must also return a structured, canonical-state-aware result.
    except Exception as error:  # noqa: BLE001
        if event_id and review.is_file():
            try:
                committed = canonical_has_event(load_object(review), event_id)
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                pass
        print(
            json.dumps(
                result_with_canonical_digest(
                    status=(
                        "published_cleanup_required"
                        if committed
                        else "precommit_failed"
                    ),
                    committed=committed,
                    review=review,
                    event_id=event_id,
                    lock_state="held_or_unknown",
                    recovery_action=(
                        "repair the reported cleanup issue and rerun recover-publish"
                        if committed
                        else "preserve the draft and inspect canonical history; do not edit canonical JSON"
                    ),
                    detail=str(error),
                ),
                sort_keys=True,
            )
        )
        return 1


def render_dashboard(dashboard: dict[str, Any], card: dict[str, Any]) -> str:
    """Render the inspection dashboard as the text a human reads.

    Every line is read back out of the dashboard the `--json` form prints, so the two
    views can never disagree about what the inspection observed.
    """

    workflow = dashboard["workflow"]
    source = dashboard["source"]
    accretion = dashboard["accretion"]
    lines = [
        f"phase: {workflow['phase']}",
        f"expected_responder: {workflow['primary_actor'] or 'none'}",
        "allowed_events_by_actor: "
        + json.dumps(workflow["allowed_events_by_actor"], sort_keys=True),
        f"open_threads: {', '.join(dashboard['open_threads']) or 'none'}",
        "open_validation_gaps: "
        + (", ".join(dashboard["open_validation_gaps"]) or "none"),
        f"operation_status: {dashboard['operation']['status']}",
        f"lock_status: {dashboard['operation']['lock_status']}",
    ]
    if dashboard["review_kind"] == "structure":
        lines.append(f"review_kind: {dashboard['review_kind']}")
    if accretion and accretion["flagged"]:
        lines.append(
            "accretion_flagged: "
            + ", ".join(
                f"{path} ({'+'.join(accretion['files'][path]['flags'])})"
                for path in accretion["flagged"]
            )
        )
    lines.extend(
        [
            f"source_drift: {str(source['drift']).lower()}",
            f"approval_stale: {str(source['approval_stale']).lower()}",
        ]
    )
    if source["terminal_outcome"]:
        lines.append(f"terminal_outcome: {source['terminal_outcome']}")
    if source["drift"] and source["drift_detail_available"]:
        lines.extend(
            [
                "changed_paths: "
                + (
                    ", ".join(
                        f"{item['change']} {item['path']}"
                        for item in source["changed_paths"]
                    )
                    or "none identified individually"
                ),
                "tracked_diff_changed: "
                + str(source["tracked_diff_changed"]).lower()
                + " (aggregate; individual tracked paths are not recorded)",
                "revision_changed: " + str(source["revision_changed"]).lower(),
            ]
        )
        if source["scope_changes"]:
            lines.append(
                "scope_changes: " + json.dumps(source["scope_changes"], sort_keys=True)
            )
    if source["source_moved_since_terminal"]:
        lines.append(
            "note: this loop ended without an approval; "
            "the source has moved since it was recorded"
        )
    lines.extend(
        [
            "timeout: " + json.dumps(dashboard["timeout_eligibility"], sort_keys=True),
            f"recommended_next_command: {dashboard['recommended_next_command']}",
            *review_card.render_card(card),
        ]
    )
    return "\n".join(lines)


def timeout_eligibility(document: dict[str, Any]) -> dict[str, Any] | None:
    """Report the timeout this phase structurally allows, and whether it is due yet.

    None for a phase that allows none, which is every terminal and any waiting phase
    whose anchoring event is missing.
    """

    workflow = document["state"]["workflow"]
    latest = document["state"].get("latest_event")
    if workflow["phase"] == "awaiting_initial_review":
        kind = "initial_review_timeout"
        started_text = document.get("created_at")
    elif workflow["phase"] in {"owner_response", "reviewer_verification"} and isinstance(
        latest, dict
    ):
        kind = (
            "owner_timeout"
            if workflow["phase"] == "owner_response"
            else "reviewer_timeout"
        )
        started_text = latest["occurred_at"]
    else:
        return None
    if not started_text:
        return None
    started = datetime.fromisoformat(started_text.replace("Z", "+00:00"))
    deadline = started + TIMEOUT_DURATION_BY_KIND[kind]
    return {
        "event_kind": kind,
        "started_at": started.isoformat(),
        "deadline": deadline.isoformat(),
        "eligible": datetime.now(timezone.utc) >= deadline,
    }


def operation(args: argparse.Namespace) -> int:
    """Print the dashboard an agent routes on.

    It reports artifact condition, timeout eligibility, source drift, the
    accretion ledger, and the one recommended next command. Every field is
    derived from canonical state and the current source; nothing here writes, so
    an inspection is safe to repeat.
    """
    review = Path(args.review)
    event = Path(args.event)
    report = Path(args.report)
    journal = Path(args.journal)
    state = import_state(Path(args.state_script))
    document = load_object(review)
    expected_report = state.render_report(document)
    report_matches = (
        report.is_file()
        and not report.is_symlink()
        and report.read_text() == expected_report
    )
    lock = json.loads(args.lock_json) if args.lock_json != "unlocked" else None
    receipt_phase: str | None = None
    artifact_error: str | None = None
    if journal.exists():
        try:
            receipt = load_object(journal)
            validate_receipt(receipt, state)
            receipt_phase = receipt["commit_phase"]
            present = canonical_has_event(document, receipt["event_id"])
            if receipt_phase == "prepared" and not present:
                status = "prepared_precommit"
                recovery = (
                    "run recover-publish with the lock token to abort preparation"
                )
            elif present and sha256(review) == receipt["intended_canonical_sha256"]:
                status = "committed_cleanup"
                recovery = "run recover-publish"
            else:
                raise ValueError("receipt does not match canonical history")
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            status = "corrupt_artifact"
            recovery = "preserve artifacts and repair the receipt integrity problem"
            artifact_error = str(error)
    elif event.exists():
        try:
            draft = load_object(event)
            errors = state.validate_event(draft)
            if errors:
                status = "editing_draft"
                recovery = "complete the draft while holding the lock"
            else:
                status = "ready_to_publish"
                recovery = "reinspect guards and publish while holding the lock"
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            status = "corrupt_artifact"
            recovery = "repair or recreate the draft while holding the lock"
            artifact_error = str(error)
    elif not report_matches:
        status = "stale_report"
        recovery = "regenerate the report while holding the lock"
    else:
        status = "clean"
        recovery = "none"
    workflow = document["state"]["workflow"]
    eligibility = timeout_eligibility(document)
    source_drift = (
        bool(document["state"]["source_fingerprint"])
        and args.current_source_fingerprint != document["state"]["source_fingerprint"]
    )
    terminal_state = document["state"].get("terminal")
    terminal_outcome_value = (
        terminal_state.get("outcome") if isinstance(terminal_state, dict) else None
    )
    # Only an approval can become stale. A timeout records that verification never
    # happened, so describing its drift as a stale approval would imply the recorded
    # source had been reviewed, and would steer the reader toward a successor loop as
    # though an approval had simply aged out.
    approval_stale = terminal_outcome_value == "lgtm" and source_drift
    terminal_source_moved = (
        workflow["phase"] == "terminal" and source_drift and not approval_stale
    )
    # Absent for a caller that supplies only a fingerprint; drift is then reported
    # without per-path detail rather than failing. A value that is present but will not
    # decode is a defect in our own CLI, not caller input, so the error surfaces.
    current_snapshot_value = (
        json.loads(args.current_source_json) if args.current_source_json else None
    )
    drift_detail = source_drift_detail(
        recorded_snapshot(document, state), current_snapshot_value
    )
    reviewer_may_replace_source = "source_update" in (
        workflow["allowed_events_by_actor"].get("reviewer") or []
    )
    # The only obligation the ledger feeds is the structure_debt acknowledgment a
    # correctness final_review must carry, so a caller that cannot publish one is
    # charged for it only on request. The human renderer keeps its summary line, which
    # is why the ledger is still computed for that form.
    terminal_eligible = any(
        "final_review" in kinds
        for kinds in workflow["allowed_events_by_actor"].values()
    )
    ledger_wanted = (
        args.accretion or terminal_eligible or not (args.json or args.agent)
    )
    # Derived per inspection, like operation status. Active phases show the ledger so
    # the reviewer sees the flagged set before templating final_review; a terminal is
    # judged from the recorded structure_debt instead, because the tree may have moved
    # since the loop's guarded snapshot.
    accretion: dict[str, Any] | None = None
    if (
        ledger_wanted
        and workflow["phase"] != "terminal"
        and document.get("review_kind") == "correctness"
        and document.get("structure_policy") != "off"
        and isinstance(current_snapshot_value, dict)
    ):
        accretion = review_ledger.ledger(
            args.repo,
            document,
            snapshot_path_list(current_snapshot_value, "scope"),
            snapshot_path_list(current_snapshot_value, "exclusions"),
        )
    structure_due = workflow[
        "phase"
    ] == "terminal" and review_ledger.structure_follow_up_due(document, review.parent)

    def recommended_command(*arguments: str) -> str:
        quoted_arguments = " ".join(shlex.quote(argument) for argument in arguments)
        return f"{args.command_prefix} {quoted_arguments}"

    # `action` names the goal the branch is working toward and `recommended` names the
    # single command that advances it, so a branch whose command is only the lock keeps
    # the goal visible and sets lock_first for the operating card's sequence.
    lock_first = False
    if status == "committed_cleanup":
        action = "recover_publication"
        recommended = recommended_command(
            "recover-publish", args.repo, args.review_id
        )
    elif lock is not None and not args.lease_present:
        action = "wait_for_lock"
        recommended = recommended_command("wait", args.repo, args.review_id, "300")
    elif not args.lease_present and status != "clean":
        action = review_card.ACTION_BY_OPERATION_STATUS[status]
        lock_first = True
        recommended = recommended_command(
            "lock", "acquire", args.repo, args.review_id
        )
    elif status == "prepared_precommit":
        action = "recover_publication"
        recommended = recommended_command(
            "recover-publish", args.repo, args.review_id
        )
    elif status == "stale_report":
        action = "regenerate_report"
        recommended = recommended_command(
            "regenerate-report", args.repo, args.review_id
        )
    elif status == "ready_to_publish":
        action = "publish_draft"
        recommended = recommended_command("publish", args.repo, args.review_id)
    elif status in {"editing_draft", "corrupt_artifact"}:
        action = "abort_draft"
        recommended = recommended_command("abort-draft", args.repo, args.review_id)
    elif approval_stale:
        action = "start_follow_up"
        recommended = recommended_command(
            "start-follow-up",
            args.repo,
            args.review_id,
            f"{document['name']}-follow-up",
        )
    elif structure_due:
        action = "start_structure_follow_up"
        recommended = recommended_command(
            "start-follow-up",
            args.repo,
            args.review_id,
            f"{document['name']}-structure",
            "--kind",
            "structure",
        )
    elif workflow["phase"] == "terminal":
        action = "none"
        recommended = "none"
    elif source_drift and reviewer_may_replace_source:
        # Drift is a safety stop, not a step to work around. Until a replacement basis is
        # recorded, the only correct route is a guarded source_update.
        action = "publish_source_update"
        lock_first = not args.lease_present
        recommended = (
            recommended_command("lock", "acquire", args.repo, args.review_id)
            if not args.lease_present
            else recommended_command(
                "template", args.repo, args.review_id, "source_update"
            )
        )
    elif not args.lease_present:
        action = review_card.ACTION_BY_PHASE[workflow["phase"]]
        lock_first = True
        recommended = recommended_command(
            "lock", "acquire", args.repo, args.review_id
        )
    elif workflow["phase"] == "awaiting_initial_review":
        action = "publish_initial_review"
        recommended = recommended_command(
            "template", args.repo, args.review_id, "review"
        )
    elif workflow["phase"] == "owner_response":
        action = "publish_owner_reply"
        recommended = recommended_command(
            "template", args.repo, args.review_id, "owner_reply"
        )
    elif workflow["phase"] == "reviewer_verification":
        action = "publish_reviewer_update"
        recommended = recommended_command(
            "template", args.repo, args.review_id, "reviewer_update"
        )
    else:
        action = "none"
        recommended = "none"
    # Ordered most-blocking first, so the human renderer and the card's required
    # reading both read in the order an agent should act on them.
    flag_conditions = (
        ("publication_recovery", status in {"prepared_precommit", "committed_cleanup"}),
        ("corrupt_artifact", status == "corrupt_artifact"),
        ("source_drift", source_drift),
        ("scope_undeclared", not document["state"]["source_fingerprint"]),
        ("scope_changed", bool(drift_detail["scope_changes"])),
        ("timeout_eligible", bool(eligibility and eligibility["eligible"])),
        ("open_validation_gaps", bool(document["state"]["validation_gaps"]["open"])),
        ("structure_round", document.get("review_kind") == "structure"),
        ("structure_follow_up_due", structure_due),
        # Only a final_review disposes of flagged files, so the card raises the flag
        # only where publishing one is allowed; the human summary line above stays
        # unconditional so an active loop keeps showing the flagged set.
        (
            "accretion_flagged",
            terminal_eligible and bool(accretion and accretion["flagged"]),
        ),
    )
    card = review_card.operating_card(
        workflow=workflow,
        action=action,
        next_command=recommended,
        lock_first=lock_first,
        open_threads=document["state"]["threads"]["open"],
        open_validation_gaps=document["state"]["validation_gaps"]["open"],
        flags=[flag for flag, present in flag_conditions if present],
        flagged_paths=(
            accretion["flagged"] if terminal_eligible and accretion else []
        ),
    )
    if args.agent:
        print(json.dumps(card, indent=2))
        return 0
    operation_value = {
        "status": status,
        "lock": lock,
        "lock_status": "locked" if lock is not None else "unlocked",
        "lease_present": args.lease_present,
        "draft_present": event.exists(),
        "publication_journal_present": journal.exists(),
        "receipt_phase": receipt_phase,
        "report_matches_canonical": report_matches,
        "artifact_error": artifact_error,
        "recovery_action": recovery,
    }
    dashboard = {
        "workflow": workflow,
        "review_kind": document.get("review_kind"),
        "structure_policy": document.get("structure_policy"),
        "accretion": accretion,
        "open_threads": document["state"]["threads"]["open"],
        "open_validation_gaps": document["state"]["validation_gaps"]["open"],
        "operation": operation_value,
        "timeout_eligibility": eligibility,
        "source": {
            "recorded_fingerprint": document["state"]["source_fingerprint"],
            "current_fingerprint": args.current_source_fingerprint,
            "drift": source_drift,
            "approval_stale": approval_stale,
            # Distinguishes "the approved source moved" from "a loop that never verified
            # anything ended, and the source has moved since".
            "terminal_outcome": terminal_outcome_value,
            "source_moved_since_terminal": terminal_source_moved,
            "changed_paths": drift_detail["changed_paths"],
            "scope_changes": drift_detail["scope_changes"],
            "revision_changed": drift_detail["revision_changed"],
            "tracked_diff_changed": drift_detail["tracked_diff_changed"],
            "drift_detail_available": drift_detail["available"],
        },
        "recommended_next_command": recommended,
    }
    if args.json:
        print(json.dumps(dashboard, indent=2))
    else:
        print(render_dashboard(dashboard, card))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("publish", "recover"):
        child = subparsers.add_parser(name)
        child.add_argument("--repo", required=True)
        child.add_argument("--review", required=True)
        child.add_argument("--event", required=True)
        child.add_argument("--report", required=True)
        child.add_argument("--journal", required=True)
        child.add_argument("--state-script", required=True)
        child.add_argument("--lock-script", required=True)
        child.add_argument("--token")
        child.add_argument("--lease")
        child.add_argument("--guard")
    publish_parser = subparsers.choices["publish"]
    publish_parser.add_argument("--snapshot-script", required=True)
    publish_parser.add_argument("--expected-review-sha")
    publish_parser.add_argument("--expected-source-fingerprint")
    publish_parser.add_argument(
        "--scope-json",
        help="Structured scope declaration, required when no inspection guard supplies one",
    )
    operation_parser = subparsers.add_parser("operation")
    operation_parser.add_argument("--review", required=True)
    operation_parser.add_argument("--event", required=True)
    operation_parser.add_argument("--report", required=True)
    operation_parser.add_argument("--journal", required=True)
    operation_parser.add_argument("--state-script", required=True)
    operation_parser.add_argument("--lock-json", required=True)
    operation_parser.add_argument("--repo", required=True)
    operation_parser.add_argument("--review-id", required=True)
    operation_parser.add_argument("--current-source-fingerprint", required=True)
    operation_parser.add_argument(
        "--current-source-json",
        default="",
        help="Current source snapshot, used to describe drift per path",
    )
    operation_parser.add_argument("--lease-present", action="store_true")
    operation_parser.add_argument("--json", action="store_true")
    operation_parser.add_argument(
        "--agent",
        action="store_true",
        help="Emit only the phase-scoped operating card",
    )
    operation_parser.add_argument(
        "--accretion",
        action="store_true",
        help="Include the accretion ledger in --json even when no final_review is allowed",
    )
    operation_parser.add_argument("--command-prefix", required=True)
    args = parser.parse_args()
    if args.command == "publish":
        if not args.token and not args.lease:
            parser.error("--token or --lease is required for publish")
        if args.lease and not args.guard:
            parser.error("--guard is required with --lease")
        return publish(args)
    if args.command == "recover":
        return recover(args)
    return operation(args)


if __name__ == "__main__":
    raise SystemExit(main())
