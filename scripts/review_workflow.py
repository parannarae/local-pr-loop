#!/usr/bin/env python3
"""High-level operational helpers for the local review workflow."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise TypeError(f"{path.name} must contain a JSON object")
    return value


def secure_write(path: Path, value: dict[str, Any]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f"{path.name}.tmp.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            json.dump(value, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def require_secure_regular(path: Path, label: str) -> None:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"{label} must be a regular non-symlink file")
    if path.stat().st_mode & 0o077:
        raise ValueError(f"{label} permissions must be 0600")


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
    secure_write(Path(args.lease), lease)
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
    secure_write(Path(args.guard), guard)
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
    secure_write(event_path, event)
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
    secure_write(event_path, event)
    print(
        json.dumps(
            {"status": "gap_added", "gap_id": gap_id, "draft": str(event_path)},
            sort_keys=True,
        )
    )
    return 0


def wait_for_change(args: argparse.Namespace) -> int:
    review = Path(args.review)
    initial = hashlib.sha256(review.read_bytes()).hexdigest()
    wall_deadline = datetime.now(timezone.utc).timestamp() + args.timeout
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
            print(json.dumps({"status": "changed", "canonical_sha256": current}))
            return 0
    status = (
        "deadline_reached"
        if handoff_deadline is not None and handoff_deadline <= wall_deadline
        else "timeout"
    )
    print(json.dumps({"status": status, "canonical_sha256": initial}))
    return 3


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
    if not 1 <= args.timeout <= 86400:
        parser.error("--timeout must be between 1 and 86400 seconds")
    return wait_for_change(args)


if __name__ == "__main__":
    raise SystemExit(main())
