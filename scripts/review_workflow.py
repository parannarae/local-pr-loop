#!/usr/bin/env python3
"""High-level operational helpers for the local review workflow."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import review_scope
from review_io import (
    load_object,
    require_secure_regular,
    secure_json,
)
from review_notes import NOTE_MARKER

# Per-thread action entries a note joins first; threads raised in the draft
# itself (review threads, new_threads) carry their own message as a fallback.
NOTE_ENTRY_FIELD_BY_KIND = {
    "owner_reply": "replies",
    "reviewer_update": "decisions",
    "final_review": "resolutions",
    "source_update": "thread_impacts",
}


def verify_lease(args: argparse.Namespace) -> dict[str, Any]:
    lease_path = Path(args.lease)
    require_secure_regular(lease_path, "lease")
    lease = load_object(lease_path)
    if set(lease) != {"review_file", "token", "acquired_at"}:
        raise ValueError("lease fields are invalid")
    completed = subprocess.run(
        [
            sys.executable,
            args.lock_script,
            "verify",
            "--repo",
            args.repo,
            "--review-file",
            args.review,
            "--token",
            lease["token"],
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError("local lease does not own the active review lock")
    return lease


def acquire_lease(args: argparse.Namespace) -> int:
    completed = subprocess.run(
        [
            sys.executable,
            args.lock_script,
            "acquire",
            "--repo",
            args.repo,
            "--review-file",
            args.review,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        print(completed.stderr, end="", file=sys.stderr)
        return completed.returncode
    lock = json.loads(completed.stdout)
    lease = {
        "review_file": str(Path(args.review).resolve()),
        "token": lock["token"],
        "acquired_at": datetime.now(timezone.utc).isoformat(),
    }
    secure_json(Path(args.lease), lease)
    print(json.dumps({"status": "acquired", "lease": args.lease}, sort_keys=True))
    return 0


def release_lease(args: argparse.Namespace) -> int:
    lease = verify_lease(args)
    completed = subprocess.run(
        [
            sys.executable,
            args.lock_script,
            "release",
            "--repo",
            args.repo,
            "--review-file",
            args.review,
            "--token",
            lease["token"],
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        print("review lock release failed", file=sys.stderr)
        return 1
    Path(args.lease).unlink()
    Path(args.guard).unlink(missing_ok=True)
    print(json.dumps({"status": "released"}, sort_keys=True))
    return 0


def candidate_declared_paths(canonical: Path, guard: Path) -> list[str] | None:
    """Return the paths another review guards, or None when it declares none yet.

    Only normalized scope metadata is read. A guard also carries an opaque lock
    capability, which is never read here and never leaves its own file.
    """

    if guard.is_file() and not guard.is_symlink():
        try:
            stored = load_object(guard).get("scope")
            return review_scope.declared_paths(stored)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass
    try:
        document = load_object(canonical)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    for event in reversed(document.get("history") or []):
        if not isinstance(event, dict):
            continue
        for field in ("completed_source_snapshot", "source_snapshot"):
            snapshot = event.get(field)
            if isinstance(snapshot, dict):
                paths = [
                    value
                    for value in (snapshot.get("scope") or [])
                    if isinstance(value, str)
                ]
                paths.extend(
                    entry["path"]
                    for entry in (snapshot.get("additional_inputs") or [])
                    if isinstance(entry, dict) and isinstance(entry.get("path"), str)
                )
                return paths
    return None


def scope_conflicts(
    review: Path, declaration: dict[str, list[str]], repository_root: str
) -> list[dict]:
    """Find non-terminal reviews in this worktree whose declared paths overlap."""

    reviews = review.parent
    proposed = review_scope.declared_paths(declaration)
    conflicts: list[dict] = []
    for canonical in sorted(reviews.glob("*.json")):
        # Only "<id>.json" is canonical; drafts, guards, leases and receipts are not.
        if canonical.name.count(".") != 1 or canonical == review:
            continue
        try:
            document = load_object(canonical)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
        state = document.get("state")
        if not isinstance(state, dict):
            continue
        workflow = state.get("workflow") or {}
        if workflow.get("phase") == "terminal":
            continue
        guard = canonical.with_suffix(".guard.json")
        declared = candidate_declared_paths(canonical, guard)
        if not declared:
            continue
        shared = review_scope.overlapping_paths(proposed, declared, repository_root)
        if shared:
            conflicts.append(
                {
                    "review_id": canonical.stem,
                    "name": document.get("name"),
                    "phase": workflow.get("phase"),
                    "overlapping_paths": shared,
                }
            )
    return conflicts


def refresh_guard(args: argparse.Namespace) -> int:
    verify_lease(args)
    # Canonicalized against the repository first: comparing the strings a caller typed
    # would let an alias of one path pass as two different declarations.
    declaration = review_scope.require_distinct_declarations(
        args.repo, json.loads(args.scope_json)
    )
    # Checked here, inside the lease-verified critical section that creates the guard, so
    # two first inspections cannot both observe no conflict and then both create one.
    conflicts = scope_conflicts(Path(args.review), declaration, args.repo)
    if conflicts:
        described = "; ".join(
            f"{conflict['review_id']} ({conflict['phase']}) over "
            + ", ".join(conflict["overlapping_paths"])
            for conflict in conflicts
        )
        print(
            "scope overlaps an active review: "
            + described
            + ". Finish that review, retire it if it has published no events, or use "
            "start-follow-up to supersede it; alternatively make the scopes disjoint.",
            file=sys.stderr,
        )
        return 1
    snapshot = subprocess.run(
        [
            sys.executable,
            args.snapshot_script,
            *review_scope.snapshot_arguments(args.repo, declaration),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if snapshot.returncode != 0:
        print("source snapshot failed", file=sys.stderr)
        return 1
    snapshot_value = json.loads(snapshot.stdout)
    guard = {
        "review_sha256": hashlib.sha256(Path(args.review).read_bytes()).hexdigest(),
        "source_snapshot": snapshot_value,
        # Stored structured so a later publication rebuilds the same arguments through the
        # same builder instead of replaying an argument tail.
        "scope": declaration,
        "inspected_at": datetime.now(timezone.utc).isoformat(),
    }
    secure_json(Path(args.guard), guard)
    print(json.dumps(guard, sort_keys=True))
    return 0


def refresh_draft_timestamp(event: dict[str, Any]) -> None:
    """Restamp the draft's occurred_at at helper-write time.

    Templates stamp occurred_at at creation, while helpers add evidence
    observed later; without this refresh every helper-touched draft fails
    validation because evidence must not postdate its event.
    """
    event["occurred_at"] = datetime.now(timezone.utc).isoformat()


def abort_draft(args: argparse.Namespace) -> int:
    verify_lease(args)
    event = Path(args.event)
    if not event.exists():
        print(json.dumps({"status": "no_draft"}, sort_keys=True))
        return 0
    require_secure_regular(event, "draft")
    event.unlink()
    print(json.dumps({"status": "draft_aborted"}, sort_keys=True))
    return 0


def add_check(args: argparse.Namespace) -> int:
    verify_lease(args)
    event_path = Path(args.event)
    require_secure_regular(event_path, "draft")
    event = load_object(event_path)
    validation = event.get("validation")
    if not isinstance(validation, dict) or not isinstance(
        validation.get("performed"), list
    ):
        raise TypeError("draft does not support validation checks")
    check: dict[str, Any] = {"check": args.check, "result": args.result}
    if args.basis:
        check["evidence"] = {
            "basis": args.basis,
            "provenance": args.provenance,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "sanitized_result": args.sanitized_result,
        }
        if args.artifact_digest:
            check["evidence"]["artifact_digest"] = args.artifact_digest
    validation["performed"].append(check)
    refresh_draft_timestamp(event)
    secure_json(event_path, event)
    print(
        json.dumps({"status": "check_added", "draft": str(event_path)}, sort_keys=True)
    )
    return 0


def add_gap(args: argparse.Namespace) -> int:
    verify_lease(args)
    event_path = Path(args.event)
    require_secure_regular(event_path, "draft")
    event = load_object(event_path)
    review = load_object(Path(args.review))
    validation = event.get("validation")
    if not isinstance(validation, dict) or not isinstance(validation.get("gaps"), list):
        raise TypeError("draft does not support validation gaps")
    identifiers = [
        *review["state"]["validation_gaps"]["open"],
        *review["state"]["validation_gaps"]["resolved"],
        *[item.get("gap_id") for item in validation["gaps"] if isinstance(item, dict)],
    ]
    numbers = [
        int(value[1:])
        for value in identifiers
        if isinstance(value, str) and value.startswith("G") and value[1:].isdigit()
    ]
    gap_id = f"G{max(numbers, default=0) + 1}"
    validation["gaps"].append(
        {
            "gap_id": gap_id,
            "check": args.check,
            "reason": args.reason,
            "material": args.material,
        }
    )
    refresh_draft_timestamp(event)
    secure_json(event_path, event)
    print(
        json.dumps(
            {"status": "gap_added", "gap_id": gap_id, "draft": str(event_path)},
            sort_keys=True,
        )
    )
    return 0


def add_note(args: argparse.Namespace) -> int:
    """Append a machine-formatted `Note to user:` line to a draft entry."""
    verify_lease(args)
    event_path = Path(args.event)
    require_secure_regular(event_path, "draft")
    event = load_object(event_path)
    field = NOTE_ENTRY_FIELD_BY_KIND.get(event.get("kind"))
    entries = event.get(field, []) if field else []
    entry = next(
        (
            item
            for item in entries
            if isinstance(item, dict) and item.get("thread_id") == args.thread
        ),
        None,
    )
    if entry is None:
        entry = next(
            (
                thread
                for thread in [*event.get("threads", []), *event.get("new_threads", [])]
                if isinstance(thread, dict) and thread.get("id") == args.thread
            ),
            None,
        )
    if entry is None:
        raise ValueError(f"draft has no entry for thread {args.thread}")
    tag_prefix = f"[{args.tag}] " if args.tag else ""
    note_line = f"{NOTE_MARKER} {tag_prefix}{args.note}"
    message = entry.get("message", "")
    entry["message"] = f"{message}\n{note_line}" if message else note_line
    refresh_draft_timestamp(event)
    secure_json(event_path, event)
    print(
        json.dumps(
            {"status": "note_added", "thread_id": args.thread, "draft": str(event_path)},
            sort_keys=True,
        )
    )
    return 0


def poll_for_change(
    review: Path, timeout: int, initial: str | None = None
) -> dict[str, str]:
    """Poll canonical JSON until it changes, the active handoff deadline
    passes, or the bounded timeout lapses; return the structured result.

    `initial` is the expected canonical SHA-256. Callers that wait across
    several polls must capture it once and pass the same value to every poll,
    or a change landing between polls is absorbed into the next baseline and
    reported as no change.
    """
    if initial is None:
        initial = hashlib.sha256(review.read_bytes()).hexdigest()
    wall_deadline = datetime.now(timezone.utc).timestamp() + timeout
    document = load_object(review)
    workflow = document["state"]["workflow"]
    latest = document["state"].get("latest_event")
    handoff_deadline: float | None = None
    started_text: str | None = None
    seconds = 7200
    if workflow["phase"] == "awaiting_initial_review":
        # Anchored on document creation: this handoff starts before any event.
        started_text = document.get("created_at")
    elif workflow["phase"] in {"owner_response", "reviewer_verification"} and isinstance(
        latest, dict
    ):
        started_text = latest["occurred_at"]
        seconds = 7200 if workflow["phase"] == "owner_response" else 1800
    if started_text:
        started = datetime.fromisoformat(started_text.replace("Z", "+00:00"))
        handoff_deadline = started.timestamp() + seconds
    deadline = min(
        value for value in (wall_deadline, handoff_deadline) if value is not None
    )
    while datetime.now(timezone.utc).timestamp() < deadline:
        remaining = deadline - datetime.now(timezone.utc).timestamp()
        time.sleep(min(2.0, max(0.0, remaining)))
        current = hashlib.sha256(review.read_bytes()).hexdigest()
        if current != initial:
            return {"status": "changed", "canonical_sha256": current}
    # Final comparison closes the boundary race: a change that landed before
    # this poll started, or in its last instant, must win over a timeout.
    current = hashlib.sha256(review.read_bytes()).hexdigest()
    if current != initial:
        return {"status": "changed", "canonical_sha256": current}
    status = (
        "deadline_reached"
        if handoff_deadline is not None and handoff_deadline <= wall_deadline
        else "timeout"
    )
    return {"status": status, "canonical_sha256": initial}


def wait_for_change(args: argparse.Namespace) -> int:
    result = poll_for_change(Path(args.review), args.timeout)
    print(json.dumps(result))
    return 0 if result["status"] == "changed" else 3


def await_handoff(args: argparse.Namespace) -> int:
    """Span one handoff by re-arming bounded polls until a structured outcome.

    Exit codes: 0 for `changed` or `terminal`, 4 for `timeout_eligible`, and
    5 for `exhausted`. The round bound is mandatory so the total wait stays
    finite even when it lapses before the phase's handoff deadline.
    """
    review = Path(args.review)
    # Capture the baseline before reading the workflow: a publication landing
    # between these two statements then reports as changed on the first poll
    # instead of being absorbed into a later baseline.
    baseline = hashlib.sha256(review.read_bytes()).hexdigest()
    workflow = load_object(review)["state"]["workflow"]
    if workflow["phase"] == "terminal":
        print(json.dumps({"status": "terminal", "rounds_used": 0}, sort_keys=True))
        return 0
    print(
        f"waiting_for: {workflow['primary_actor']}"
        f" to {workflow['primary_action']['kind']} (a handoff deadline applies)",
        flush=True,
    )
    for round_number in range(1, args.max_rounds + 1):
        result = poll_for_change(review, args.round_seconds, baseline)
        outcome = {
            "rounds_used": round_number,
            "canonical_sha256": result["canonical_sha256"],
        }
        if result["status"] == "changed":
            phase = load_object(review)["state"]["workflow"]["phase"]
            outcome["status"] = "terminal" if phase == "terminal" else "changed"
            print(json.dumps(outcome, sort_keys=True))
            return 0
        if result["status"] == "deadline_reached":
            outcome["status"] = "timeout_eligible"
            print(json.dumps(outcome, sort_keys=True))
            return 4
    print(
        json.dumps(
            {"status": "exhausted", "rounds_used": args.max_rounds}, sort_keys=True
        )
    )
    return 5


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    for name in (
        "acquire",
        "verify",
        "release",
        "guard",
        "abort-draft",
        "add-check",
        "add-gap",
        "add-note",
    ):
        child = commands.add_parser(name)
        child.add_argument("--repo", required=True)
        child.add_argument("--review", required=True)
        child.add_argument("--lease", required=True)
        child.add_argument("--guard", required=True)
        child.add_argument("--lock-script", required=True)
        if name == "guard":
            child.add_argument("--snapshot-script", required=True)
            child.add_argument("--scope-json", required=True)
        if name == "abort-draft":
            child.add_argument("--event", required=True)
        if name == "add-check":
            child.add_argument("--event", required=True)
            child.add_argument("--result", choices=("passed", "failed"), required=True)
            child.add_argument("--check", required=True)
            child.add_argument("--basis")
            child.add_argument("--provenance", default="")
            child.add_argument("--sanitized-result", default="")
            child.add_argument("--artifact-digest")
        if name == "add-gap":
            child.add_argument("--event", required=True)
            child.add_argument("--check", required=True)
            child.add_argument("--reason", required=True)
            child.add_argument("--material", action="store_true")
        if name == "add-note":
            child.add_argument("--event", required=True)
            child.add_argument("--thread", required=True)
            child.add_argument("--note", required=True)
            child.add_argument("--tag")
    wait_parser = commands.add_parser("wait")
    wait_parser.add_argument("--review", required=True)
    wait_parser.add_argument("--timeout", type=int, default=300)
    await_parser = commands.add_parser("await-handoff")
    await_parser.add_argument("--review", required=True)
    await_parser.add_argument("--round-seconds", type=int, default=300)
    await_parser.add_argument("--max-rounds", type=int, default=24)
    args = parser.parse_args()
    if args.command == "acquire":
        return acquire_lease(args)
    if args.command == "verify":
        verify_lease(args)
        print(json.dumps({"status": "verified"}, sort_keys=True))
        return 0
    if args.command == "release":
        return release_lease(args)
    if args.command == "guard":
        return refresh_guard(args)
    if args.command == "abort-draft":
        return abort_draft(args)
    if args.command == "add-check":
        return add_check(args)
    if args.command == "add-gap":
        return add_gap(args)
    if args.command == "add-note":
        return add_note(args)
    if args.command == "await-handoff":
        if not 1 <= args.round_seconds <= 86400:
            parser.error("--round-seconds must be between 1 and 86400 seconds")
        if args.max_rounds < 1:
            parser.error("--max-rounds must be at least 1")
        return await_handoff(args)
    if not 1 <= args.timeout <= 86400:
        parser.error("--timeout must be between 1 and 86400 seconds")
    return wait_for_change(args)


if __name__ == "__main__":
    raise SystemExit(main())
