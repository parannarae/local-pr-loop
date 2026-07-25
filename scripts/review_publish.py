#!/usr/bin/env python3
"""Commit and recover review publications around one atomic canonical replace."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise TypeError(f"{path} must contain a JSON object")
    return value


def atomic_bytes(path: Path, content: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f"{path.name}.tmp.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    atomic_bytes(path, (json.dumps(value, indent=2) + "\n").encode())


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


def result(
    *,
    status: str,
    committed: bool,
    review: Path,
    event_id: str | None,
    lock_state: str,
    recovery_action: str,
    detail: str | None = None,
) -> dict[str, Any]:
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


def write_report(state: Any, document: dict[str, Any], report: Path) -> None:
    atomic_bytes(report, state.render_report(document).encode())


def publish(args: argparse.Namespace) -> int:
    review = Path(args.review)
    event_path = Path(args.event)
    report = Path(args.report)
    journal = Path(args.journal)
    state = import_state(Path(args.state_script))
    event_id: str | None = None
    committed = False
    try:
        document = load_json(review)
        event = load_json(event_path)
        event_id = event.get("event_id")
        if not isinstance(event_id, str):
            raise TypeError("event_id is required")
        base_sha = sha256(review)
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

        # This replace is the one and only publication commit point.
        atomic_bytes(review, canonical_bytes)
        committed = True
        receipt["commit_phase"] = "canonical_committed"
        atomic_json(journal, receipt)
        write_report(state, updated, report)
        if event_path.is_file() and sha256(event_path) == draft_sha:
            event_path.unlink()
        release_lock(Path(args.lock_script), Path(args.repo), review, args.token)
        journal.unlink()
        print(
            json.dumps(
                result(
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
                committed = canonical_has_event(load_json(review), event_id)
            except (OSError, ValueError, json.JSONDecodeError):
                pass
        status = "published_cleanup_required" if committed else "precommit_failed"
        if not committed:
            journal.unlink(missing_ok=True)
        action = (
            "inspect canonical state, then run recover-publish; do not retry the old SHA"
            if committed
            else "fix the reported problem and publish the unchanged draft again"
        )
        print(
            json.dumps(
                result(
                    status=status,
                    committed=committed,
                    review=review,
                    event_id=event_id,
                    lock_state="held_or_unknown",
                    recovery_action=action,
                    detail=str(error),
                ),
                sort_keys=True,
            )
        )
        return 1


def recover(args: argparse.Namespace) -> int:
    review = Path(args.review)
    event_path = Path(args.event)
    report = Path(args.report)
    journal = Path(args.journal)
    state = import_state(Path(args.state_script))
    event_id: str | None = None
    committed = False
    try:
        receipt = load_json(journal)
        event_id = receipt.get("event_id")
        if not isinstance(event_id, str):
            raise TypeError("publication journal has no event_id")
        document = load_json(review)
        errors = state.validate_document(document)
        if errors:
            raise ValueError("; ".join(errors))
        if not canonical_has_event(document, event_id):
            raise ValueError("journal event is not committed in canonical history")
        committed = True
        intended = receipt.get("intended_canonical_sha256")
        if sha256(review) != intended:
            raise ValueError("canonical SHA does not match the publication receipt")
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
        else:
            status = subprocess.run(
                [
                    sys.executable,
                    args.lock_script,
                    "status",
                    "--repo",
                    args.repo,
                    "--review-file",
                    str(review),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if status.returncode != 0 or status.stdout.strip() != "unlocked":
                raise ValueError("active lock requires its token for recovery")
        journal.unlink()
        print(
            json.dumps(
                result(
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
                committed = canonical_has_event(load_json(review), event_id)
            except (OSError, ValueError, json.JSONDecodeError):
                pass
        print(
            json.dumps(
                result(
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


def operation(args: argparse.Namespace) -> int:
    review = Path(args.review)
    event = Path(args.event)
    report = Path(args.report)
    journal = Path(args.journal)
    state = import_state(Path(args.state_script))
    document = load_json(review)
    expected_report = state.render_report(document)
    report_matches = (
        report.is_file()
        and not report.is_symlink()
        and report.read_text() == expected_report
    )
    lock = json.loads(args.lock_json) if args.lock_json != "unlocked" else None
    if journal.exists():
        status = "cleanup_required"
        recovery = "run recover-publish"
    elif lock is not None:
        status = "locked"
        recovery = "wait for the holder or use the matching token"
    elif event.exists():
        status = "editing_draft"
        recovery = "complete and publish or remove the draft while holding the lock"
    elif not report_matches:
        status = "publication_committed"
        recovery = "regenerate the report while holding the lock"
    else:
        status = "clean"
        recovery = "none"
    workflow = document["state"]["workflow"]
    timeout_eligibility: dict[str, Any] | None = None
    latest = document["state"].get("latest_event")
    if workflow["phase"] in {"owner_response", "reviewer_verification"} and isinstance(
        latest, dict
    ):
        started = datetime.fromisoformat(latest["occurred_at"].replace("Z", "+00:00"))
        kind = (
            "owner_timeout"
            if workflow["phase"] == "owner_response"
            else "reviewer_timeout"
        )
        deadline = started + (
            timedelta(hours=2) if kind == "owner_timeout" else timedelta(minutes=30)
        )
        timeout_eligibility = {
            "event_kind": kind,
            "started_at": started.isoformat(),
            "deadline": deadline.isoformat(),
            "eligible": datetime.now(timezone.utc) >= deadline,
        }
    print(
        json.dumps(
            {
                "status": status,
                "lock": lock,
                "draft_present": event.exists(),
                "publication_journal_present": journal.exists(),
                "report_matches_canonical": report_matches,
                "timeout_eligibility": timeout_eligibility,
                "recovery_action": recovery,
            },
            indent=2,
        )
    )
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
    operation_parser = subparsers.add_parser("operation")
    operation_parser.add_argument("--review", required=True)
    operation_parser.add_argument("--event", required=True)
    operation_parser.add_argument("--report", required=True)
    operation_parser.add_argument("--journal", required=True)
    operation_parser.add_argument("--state-script", required=True)
    operation_parser.add_argument("--lock-json", required=True)
    args = parser.parse_args()
    if args.command == "publish":
        if not args.token:
            parser.error("--token is required for publish")
        return publish(args)
    if args.command == "recover":
        return recover(args)
    return operation(args)


if __name__ == "__main__":
    raise SystemExit(main())
