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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from review_io import (
    atomic_bytes,
    atomic_json,
)
from review_io import load_object as load_json
from review_io import load_secure_object as load_secure_json


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


def current_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    completed = subprocess.run(
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
    if completed.returncode != 0:
        raise RuntimeError("source snapshot failed")
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
        if args.lease:
            lease = load_secure_json(Path(args.lease), "lease")
            guard = load_secure_json(Path(args.guard), "inspection guard")
            args.token = lease.get("token")
            args.expected_review_sha = guard.get("review_sha256")
            snapshot = guard.get("source_snapshot")
            if not isinstance(snapshot, dict):
                raise ValueError("inspection guard source snapshot is invalid")
            args.expected_source_fingerprint = snapshot.get("fingerprint")
            args.scope_args = guard.get("scope_args")
            if not isinstance(args.scope_args, list):
                raise ValueError("inspection guard scope is invalid")
        verify_lock(Path(args.lock_script), Path(args.repo), review, args.token)
        document = load_json(review)
        validation_errors = state.validate_document(document)
        if validation_errors:
            raise ValueError("; ".join(validation_errors))
        event = load_json(event_path)
        event_id = event.get("event_id")
        if not isinstance(event_id, str):
            raise TypeError("event_id is required")
        base_sha = sha256(review)
        if base_sha != args.expected_review_sha:
            raise ValueError("canonical SHA does not match expected review SHA")
        snapshot = current_snapshot(args)
        if snapshot.get("fingerprint") != args.expected_source_fingerprint:
            raise ValueError("source fingerprint does not match expected fingerprint")
        event_snapshot_field = state.SOURCE_FIELD_BY_KIND.get(event.get("kind"))
        if event_snapshot_field:
            event_snapshot = event.get(event_snapshot_field)
            identity_fields = (
                "revision",
                "scope",
                "exclusions",
                "additional_inputs",
                "fingerprint",
            )
            if not isinstance(event_snapshot, dict) or any(
                event_snapshot.get(key) != snapshot.get(key) for key in identity_fields
            ):
                raise ValueError("event snapshot does not match guarded source")
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
        verify_lock(Path(args.lock_script), Path(args.repo), review, args.token)
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
        release_lock(Path(args.lock_script), Path(args.repo), review, args.token)
        journal.unlink()
        if args.lease:
            Path(args.lease).unlink()
            Path(args.guard).unlink(missing_ok=True)
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
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                pass
        status = "published_cleanup_required" if committed else "precommit_failed"
        action = (
            "inspect canonical state, then run recover-publish; do not retry the old SHA"
            if committed
            else (
                "run recover-publish with the lock token to abort any prepared receipt, "
                "then reinspect before publishing"
                if journal.exists()
                else "fix the reported problem and publish the unchanged draft again"
            )
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
        if args.lease and Path(args.lease).exists():
            lease = load_secure_json(Path(args.lease), "lease")
            args.token = lease.get("token")
        receipt = load_json(journal)
        validate_receipt(receipt, state)
        event_id = receipt.get("event_id")
        if not isinstance(event_id, str):
            raise TypeError("publication journal has no event_id")
        document = load_json(review)
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
                    result(
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
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
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
    receipt_phase: str | None = None
    artifact_error: str | None = None
    if journal.exists():
        try:
            receipt = load_json(journal)
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
            draft = load_json(event)
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
    timeout_eligibility: dict[str, Any] | None = None
    latest = document["state"].get("latest_event")
    started_text: str | None = None
    kind = ""
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
    if started_text:
        started = datetime.fromisoformat(started_text.replace("Z", "+00:00"))
        deadline = started + (
            timedelta(minutes=30)
            if kind == "reviewer_timeout"
            else timedelta(hours=2)
        )
        timeout_eligibility = {
            "event_kind": kind,
            "started_at": started.isoformat(),
            "deadline": deadline.isoformat(),
            "eligible": datetime.now(timezone.utc) >= deadline,
        }
    source_drift = (
        bool(document["state"]["source_fingerprint"])
        and args.current_source_fingerprint != document["state"]["source_fingerprint"]
    )
    approval_stale = workflow["phase"] == "terminal" and source_drift

    def recommended_command(*arguments: str) -> str:
        quoted_arguments = " ".join(shlex.quote(argument) for argument in arguments)
        return f"{args.command_prefix} {quoted_arguments}"

    if status == "committed_cleanup":
        recommended = recommended_command(
            "recover-publish", args.repo, args.review_id
        )
    elif lock is not None and not args.lease_present:
        recommended = recommended_command("wait", args.repo, args.review_id, "300")
    elif not args.lease_present and status != "clean":
        recommended = recommended_command(
            "lock", "acquire", args.repo, args.review_id
        )
    elif status == "prepared_precommit":
        recommended = recommended_command(
            "recover-publish", args.repo, args.review_id
        )
    elif status == "stale_report":
        recommended = recommended_command(
            "regenerate-report", args.repo, args.review_id
        )
    elif status == "ready_to_publish":
        recommended = recommended_command("publish", args.repo, args.review_id)
    elif status in {"editing_draft", "corrupt_artifact"}:
        recommended = recommended_command("abort-draft", args.repo, args.review_id)
    elif approval_stale:
        recommended = recommended_command(
            "start-follow-up",
            args.repo,
            args.review_id,
            f"{document['name']}-follow-up",
        )
    elif workflow["phase"] == "terminal":
        recommended = "none"
    elif not args.lease_present:
        recommended = recommended_command(
            "lock", "acquire", args.repo, args.review_id
        )
    elif workflow["phase"] == "awaiting_initial_review":
        recommended = recommended_command(
            "template", args.repo, args.review_id, "review"
        )
    elif workflow["phase"] == "owner_response":
        recommended = recommended_command(
            "template", args.repo, args.review_id, "owner_reply"
        )
    elif workflow["phase"] == "reviewer_verification":
        recommended = recommended_command(
            "template", args.repo, args.review_id, "reviewer_update"
        )
    else:
        recommended = "none"
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
        "open_threads": document["state"]["threads"]["open"],
        "open_validation_gaps": document["state"]["validation_gaps"]["open"],
        "operation": operation_value,
        "timeout_eligibility": timeout_eligibility,
        "source": {
            "recorded_fingerprint": document["state"]["source_fingerprint"],
            "current_fingerprint": args.current_source_fingerprint,
            "drift": source_drift,
            "approval_stale": approval_stale,
        },
        "recommended_next_command": recommended,
    }
    if args.json:
        print(json.dumps(dashboard, indent=2))
    else:
        print(
            "\n".join(
                [
                    f"phase: {workflow['phase']}",
                    f"expected_responder: {workflow['primary_actor'] or 'none'}",
                    "allowed_events_by_actor: "
                    + json.dumps(workflow["allowed_events_by_actor"], sort_keys=True),
                    f"open_threads: {', '.join(dashboard['open_threads']) or 'none'}",
                    "open_validation_gaps: "
                    + (", ".join(dashboard["open_validation_gaps"]) or "none"),
                    f"operation_status: {status}",
                    f"lock_status: {operation_value['lock_status']}",
                    f"source_drift: {str(source_drift).lower()}",
                    f"approval_stale: {str(approval_stale).lower()}",
                    "timeout: " + json.dumps(timeout_eligibility, sort_keys=True),
                    f"recommended_next_command: {recommended}",
                ]
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
        child.add_argument("--lease")
        child.add_argument("--guard")
    publish_parser = subparsers.choices["publish"]
    publish_parser.add_argument("--snapshot-script", required=True)
    publish_parser.add_argument("--expected-review-sha")
    publish_parser.add_argument("--expected-source-fingerprint")
    publish_parser.add_argument("scope_args", nargs=argparse.REMAINDER)
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
    operation_parser.add_argument("--lease-present", action="store_true")
    operation_parser.add_argument("--json", action="store_true")
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
