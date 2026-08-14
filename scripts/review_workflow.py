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

from review_io import (
    load_object,
    require_secure_regular,
    secure_json,
)


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


def refresh_guard(args: argparse.Namespace) -> int:
    verify_lease(args)
    snapshot = subprocess.run(
        [
            sys.executable,
            args.snapshot_script,
            "--repo",
            args.repo,
            *args.scope_args,
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
        "scope_args": args.scope_args,
        "inspected_at": datetime.now(timezone.utc).isoformat(),
    }
    secure_json(Path(args.guard), guard)
    print(json.dumps(guard, sort_keys=True))
    return 0


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
    secure_json(event_path, event)
    print(
        json.dumps(
            {"status": "gap_added", "gap_id": gap_id, "draft": str(event_path)},
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
    if workflow["phase"] in {"owner_response", "reviewer_verification"} and isinstance(
        latest, dict
    ):
        started = datetime.fromisoformat(latest["occurred_at"].replace("Z", "+00:00"))
        seconds = 7200 if workflow["phase"] == "owner_response" else 1800
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
    5 for `exhausted`. The round bound is mandatory: `awaiting_initial_review`
    has no handoff deadline, so an unbounded wait there would never return.
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
    deadline_note = (
        "a handoff deadline applies"
        if workflow["phase"] in {"owner_response", "reviewer_verification"}
        else "no handoff deadline in this phase"
    )
    print(
        f"waiting_for: {workflow['primary_actor']}"
        f" to {workflow['primary_action']['kind']} ({deadline_note})",
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
    ):
        child = commands.add_parser(name)
        child.add_argument("--repo", required=True)
        child.add_argument("--review", required=True)
        child.add_argument("--lease", required=True)
        child.add_argument("--guard", required=True)
        child.add_argument("--lock-script", required=True)
        if name == "guard":
            child.add_argument("--snapshot-script", required=True)
            child.add_argument("scope_args", nargs=argparse.REMAINDER)
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
