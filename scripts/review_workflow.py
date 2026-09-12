#!/usr/bin/env python3
"""High-level operational helpers for the local review workflow."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import review_scope
from review_contract import TIMEOUT_DURATION_BY_KIND
from review_io import (
    load_object,
    require_secure_regular,
    secure_json,
)


@dataclass(frozen=True)
class ReviewLeaseContext:
    """The repository, review, and lock artifacts one leased operation acts on.

    Built once from the parsed command line, so each operation below declares the
    artifacts it needs instead of reaching into a namespace for them.
    """

    repo: Path
    review: Path
    lease: Path
    guard: Path
    lock_script: Path


def lease_context(args: argparse.Namespace) -> ReviewLeaseContext:
    """Build the lease context one leased subcommand's arguments describe."""

    return ReviewLeaseContext(
        repo=Path(args.repo),
        review=Path(args.review),
        lease=Path(args.lease),
        guard=Path(args.guard),
        lock_script=Path(args.lock_script),
    )


def verify_lease(context: ReviewLeaseContext) -> dict[str, Any]:
    """Return the local lease, refusing one that does not own the active lock."""

    require_secure_regular(context.lease, "lease")
    lease = load_object(context.lease)
    if set(lease) != {"review_file", "token", "acquired_at"}:
        raise ValueError("lease fields are invalid")
    completed = subprocess.run(
        [
            sys.executable,
            str(context.lock_script),
            "verify",
            "--repo",
            str(context.repo),
            "--review-file",
            str(context.review),
            "--token",
            lease["token"],
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError(
            "local lease does not own the active review lock: "
            + completed.stderr.strip()
        )
    return lease


def acquire_lease(context: ReviewLeaseContext) -> int:
    completed = subprocess.run(
        [
            sys.executable,
            str(context.lock_script),
            "acquire",
            "--repo",
            str(context.repo),
            "--review-file",
            str(context.review),
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
        "review_file": str(context.review.resolve()),
        "token": lock["token"],
        "acquired_at": datetime.now(timezone.utc).isoformat(),
    }
    secure_json(context.lease, lease)
    print(
        json.dumps({"status": "acquired", "lease": str(context.lease)}, sort_keys=True)
    )
    return 0


def release_lease(context: ReviewLeaseContext) -> int:
    lease = verify_lease(context)
    completed = subprocess.run(
        [
            sys.executable,
            str(context.lock_script),
            "release",
            "--repo",
            str(context.repo),
            "--review-file",
            str(context.review),
            "--token",
            lease["token"],
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        print(f"review lock release failed: {completed.stderr.strip()}", file=sys.stderr)
        return completed.returncode
    context.lease.unlink()
    context.guard.unlink(missing_ok=True)
    print(json.dumps({"status": "released"}, sort_keys=True))
    return 0


def find_declared_paths(canonical: Path, guard: Path) -> list[str] | None:
    """Return the paths another review guards, or None when none can be read.

    None collapses "this loop has declared nothing yet" with "its canonical file is
    unreadable", deliberately: the only caller skips an unreadable loop anyway, and a
    loop that declares nothing cannot overlap a scope either way.

    Only the structured scope metadata is read. A guard also carries an opaque lock
    capability, which is never read here and never leaves its own file.
    """

    if guard.is_file() and not guard.is_symlink():
        try:
            stored = load_object(guard).get("scope")
            return review_scope.declared_paths(stored)
        # A guard written by an unsupported revision, truncated by a crash mid-write,
        # or left unreadable falls back to history below rather than failing this
        # scan, so one damaged loop cannot block every later inspection. A loop that
        # is guarded but has published nothing then declares nothing here.
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
        declared = find_declared_paths(canonical, guard)
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


def refresh_guard(
    context: ReviewLeaseContext, scope_json: str, snapshot_script: str
) -> int:
    """Record the inspection guard for a declared scope, under the verified lease."""

    verify_lease(context)
    repository_root = str(context.repo)
    # Canonicalized against the repository first: comparing the strings a caller typed
    # would let an alias of one path pass as two different declarations.
    declaration = review_scope.require_distinct_declarations(
        repository_root, json.loads(scope_json)
    )
    # Checked here, inside the lease-verified critical section that creates the guard, so
    # two first inspections cannot both observe no conflict and then both create one.
    conflicts = scope_conflicts(context.review, declaration, repository_root)
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
            snapshot_script,
            *review_scope.snapshot_arguments(repository_root, declaration),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if snapshot.returncode != 0:
        print(f"source snapshot failed: {snapshot.stderr.strip()}", file=sys.stderr)
        return snapshot.returncode
    snapshot_value = json.loads(snapshot.stdout)
    guard = {
        "review_sha256": hashlib.sha256(context.review.read_bytes()).hexdigest(),
        "source_snapshot": snapshot_value,
        # Stored structured so a later publication rebuilds the same arguments through the
        # same builder instead of replaying an argument tail.
        "scope": declaration,
        "inspected_at": datetime.now(timezone.utc).isoformat(),
    }
    secure_json(context.guard, guard)
    print(json.dumps(guard, sort_keys=True))
    return 0


def abort_draft(context: ReviewLeaseContext, event: Path) -> int:
    """Remove the leased draft, reporting plainly when there is none."""

    verify_lease(context)
    if not event.exists():
        print(json.dumps({"status": "no_draft"}, sort_keys=True))
        return 0
    require_secure_regular(event, "draft")
    event.unlink()
    print(json.dumps({"status": "draft_aborted"}, sort_keys=True))
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

    A handoff deadline that has already passed still reports `deadline_reached`,
    but no longer shortens the wait: the caller's bound is honored in full, so a
    counterpart that is late rather than absent can still be waited for.
    """
    if initial is None:
        initial = hashlib.sha256(review.read_bytes()).hexdigest()
    started_at = datetime.now(timezone.utc).timestamp()
    wall_deadline = started_at + timeout
    document = load_object(review)
    workflow = document["state"]["workflow"]
    latest = document["state"].get("latest_event")
    handoff_deadline: float | None = None
    started_text: str | None = None
    timeout_kind = ""
    if workflow["phase"] == "awaiting_initial_review":
        # Anchored on document creation: this handoff starts before any event.
        timeout_kind = "initial_review_timeout"
        started_text = document.get("created_at")
    elif workflow["phase"] in {"owner_response", "reviewer_verification"} and isinstance(
        latest, dict
    ):
        timeout_kind = (
            "owner_timeout"
            if workflow["phase"] == "owner_response"
            else "reviewer_timeout"
        )
        started_text = latest["occurred_at"]
    if started_text:
        started = datetime.fromisoformat(started_text.replace("Z", "+00:00"))
        handoff_deadline = (
            started + TIMEOUT_DURATION_BY_KIND[timeout_kind]
        ).timestamp()
    # A handoff deadline still ahead cuts this wait short, so the waiting actor learns
    # it may publish a timeout without sitting out the whole bound. One already behind
    # must not: the loop's deadline would then be in the past, this call would return
    # without polling once, and every later call would do the same — leaving an actor
    # whose counterpart is merely late with no way to keep waiting at all.
    deadline = wall_deadline
    if handoff_deadline is not None and handoff_deadline > started_at:
        deadline = min(wall_deadline, handoff_deadline)
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


def wait_for_change(review: Path, timeout: int) -> int:
    """Run one bounded poll and print its result.

    Exit codes: 0 when canonical state changed, 3 for every other outcome, which
    the printed `status` distinguishes.
    """
    result = poll_for_change(review, timeout)
    print(json.dumps(result))
    return 0 if result["status"] == "changed" else 3


def await_handoff(review: Path, round_seconds: int, max_rounds: int) -> int:
    """Span one handoff by re-arming bounded polls until a structured outcome.

    Exit codes: 0 for `changed` or `terminal`, 4 for `timeout_eligible`, and
    5 for `exhausted`. The round bound is mandatory so the total wait stays
    finite even when it lapses before the phase's handoff deadline.
    """
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
    for round_number in range(1, max_rounds + 1):
        result = poll_for_change(review, round_seconds, baseline)
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
        json.dumps({"status": "exhausted", "rounds_used": max_rounds}, sort_keys=True)
    )
    return 5


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("acquire", "verify", "release", "guard", "abort-draft"):
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
    wait_parser = commands.add_parser("wait")
    wait_parser.add_argument("--review", required=True)
    wait_parser.add_argument("--timeout", type=int, default=300)
    await_parser = commands.add_parser("await-handoff")
    await_parser.add_argument("--review", required=True)
    await_parser.add_argument("--round-seconds", type=int, default=300)
    await_parser.add_argument("--max-rounds", type=int, default=24)
    args = parser.parse_args()
    if args.command == "await-handoff":
        if not 1 <= args.round_seconds <= 86400:
            parser.error("--round-seconds must be between 1 and 86400 seconds")
        if args.max_rounds < 1:
            parser.error("--max-rounds must be at least 1")
        return await_handoff(Path(args.review), args.round_seconds, args.max_rounds)
    if args.command == "wait":
        if not 1 <= args.timeout <= 86400:
            parser.error("--timeout must be between 1 and 86400 seconds")
        return wait_for_change(Path(args.review), args.timeout)
    context = lease_context(args)
    if args.command == "acquire":
        return acquire_lease(context)
    if args.command == "verify":
        verify_lease(context)
        print(json.dumps({"status": "verified"}, sort_keys=True))
        return 0
    if args.command == "release":
        return release_lease(context)
    if args.command == "guard":
        return refresh_guard(context, args.scope_json, args.snapshot_script)
    if args.command == "abort-draft":
        return abort_draft(context, Path(args.event))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
