#!/usr/bin/env python3
"""Compose review state operations and expose their command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import review_render
import review_schema
import review_templates
from review_contract import SOURCE_FIELD_BY_KIND, TIMEOUT_DURATION_BY_KIND
from review_projection import (
    default_state,
    project_history,
    validate_document,
)

ACTOR_BY_KIND = review_schema.ACTOR_BY_KIND
CREATOR_VERSION = review_schema.CREATOR_VERSION
EVIDENCE_BASES = review_schema.EVIDENCE_BASES
EVENT_ID_PATTERN = review_schema.EVENT_ID_PATTERN
FORMAT = review_schema.FORMAT
FORMAT_REVISION = review_schema.FORMAT_REVISION
SHA256_PATTERN = review_schema.SHA256_PATTERN
load_json = review_schema.load_json
reject_duplicate_keys = review_schema.reject_duplicate_keys
validate_event = review_schema.validate_event


def blank_snapshot() -> dict[str, Any]:
    return review_templates.blank_snapshot()


def blank_evidence() -> dict[str, Any]:
    return review_templates.blank_evidence()


def blank_validation() -> dict[str, list[Any]]:
    return review_templates.blank_validation()


def blank_thread() -> dict[str, Any]:
    return review_templates.blank_thread()


def new_document(
    review_id: str,
    name: str,
    prior_review_id: str | None = None,
    review_kind: str = "correctness",
    structure_policy: str = "auto",
    comparison_base: str | None = None,
) -> dict[str, Any]:
    return {
        "format": FORMAT,
        "format_revision": FORMAT_REVISION,
        "created_by": {"version": CREATOR_VERSION},
        "created_at": datetime.now(timezone.utc).isoformat(),
        "review_id": review_id,
        "prior_review_id": prior_review_id,
        "name": name,
        "review_kind": review_kind,
        "structure_policy": structure_policy,
        "comparison_base": comparison_base,
        "state": default_state(),
        "history": [],
    }


def event_template(kind: str) -> dict[str, Any]:
    return review_templates.event_template(kind)


def contextual_event_template(
    document: dict[str, Any],
    kind: str,
    guarded_snapshot: dict[str, Any],
    flagged_paths: list[str] | None = None,
) -> dict[str, Any]:
    return review_templates.contextual_event_template(
        document, kind, guarded_snapshot, flagged_paths
    )


def thread_conversations(document: dict[str, Any]) -> list[dict[str, Any]]:
    return review_render.thread_conversations(document)


def render_conversations(document: dict[str, Any]) -> str:
    return review_render.render_conversations(document)


def append_event(document: Any, event: Any) -> dict[str, Any]:
    if not isinstance(document, dict) or not isinstance(document.get("history"), list):
        raise TypeError("review document has invalid history")
    if not isinstance(event, dict):
        raise TypeError("event must be a JSON object")
    next_history = [*document["history"], event]
    errors, state, _ = project_history(next_history, document.get("created_at"))
    if errors:
        raise ValueError("; ".join(errors))
    document["history"] = next_history
    document["state"] = state
    return document


def evidence_summary(value: Any) -> str:
    return review_render.evidence_summary(value)


def render_report(document: dict[str, Any]) -> str:
    return review_render.render_report(document)


def emit_validation(errors: list[str]) -> int:
    if errors:
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("valid")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("--review-id")
    subparsers.add_parser("validate-event")
    subparsers.add_parser("report")
    subparsers.add_parser("state")
    init_parser = subparsers.add_parser("init")
    init_parser.add_argument("review_id")
    init_parser.add_argument("name")
    init_parser.add_argument("--prior-review-id")
    init_parser.add_argument(
        "--review-kind",
        choices=sorted(review_schema.REVIEW_KINDS),
        default="correctness",
    )
    init_parser.add_argument(
        "--structure-policy",
        choices=sorted(review_schema.STRUCTURE_POLICIES),
        default="auto",
    )
    init_parser.add_argument("--comparison-base")
    snapshot_parser = subparsers.add_parser("source-snapshot")
    snapshot_parser.add_argument("--kind")
    append_parser = subparsers.add_parser("append-event")
    append_parser.add_argument("event_json")
    template_parser = subparsers.add_parser("template")
    template_parser.add_argument("kind", choices=ACTOR_BY_KIND)
    contextual_parser = subparsers.add_parser("context-template")
    contextual_parser.add_argument("kind", choices=ACTOR_BY_KIND)
    contextual_parser.add_argument("snapshot_json")
    contextual_parser.add_argument(
        "--flagged-json",
        help="JSON list of accretion-flagged paths to prefill as structure_debt",
    )
    threads_parser = subparsers.add_parser("threads")
    threads_parser.add_argument("--json", action="store_true")
    evidence_parser = subparsers.add_parser("evidence-template")
    evidence_parser.add_argument("basis", choices=EVIDENCE_BASES)
    subparsers.add_parser("eligible-timeout")
    args = parser.parse_args()
    if args.command == "template":
        print(json.dumps(event_template(args.kind), indent=2))
        return 0
    if args.command == "evidence-template":
        value = blank_evidence()
        value["basis"] = args.basis
        print(json.dumps(value, indent=2))
        return 0
    if args.command == "init":
        document = new_document(
            args.review_id,
            args.name,
            args.prior_review_id,
            args.review_kind,
            args.structure_policy,
            args.comparison_base,
        )
        errors = validate_document(document)
        if errors:
            return emit_validation(errors)
        print(json.dumps(document, indent=2))
        return 0
    try:
        value = load_json()
    except (ValueError, json.JSONDecodeError) as error:
        print(f"invalid JSON: {error}", file=sys.stderr)
        return 1
    if args.command == "validate":
        errors = validate_document(value)
        if (
            args.review_id
            and isinstance(value, dict)
            and value.get("review_id") != args.review_id
        ):
            errors.append("review_id does not match the selected review")
        return emit_validation(errors)
    if args.command == "validate-event":
        return emit_validation(validate_event(value))
    if args.command == "source-snapshot":
        kind = args.kind or (value.get("kind") if isinstance(value, dict) else None)
        field = SOURCE_FIELD_BY_KIND.get(kind)
        print(
            json.dumps(value.get(field))
            if field and isinstance(value, dict)
            else "null"
        )
        return 0
    if args.command == "report":
        print(render_report(value), end="")
        return 0
    if args.command == "context-template":
        try:
            snapshot = json.loads(Path(args.snapshot_json).read_text())
            if isinstance(snapshot, dict) and isinstance(
                snapshot.get("source_snapshot"), dict
            ):
                snapshot = snapshot["source_snapshot"]
            flagged = json.loads(args.flagged_json) if args.flagged_json else None
            if flagged is not None and (
                not isinstance(flagged, list)
                or not all(isinstance(item, str) for item in flagged)
            ):
                raise ValueError("--flagged-json must be a JSON list of paths")
            print(
                json.dumps(
                    contextual_event_template(value, args.kind, snapshot, flagged),
                    indent=2,
                )
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            print(f"cannot build contextual event: {error}", file=sys.stderr)
            return 1
        return 0
    if args.command == "eligible-timeout":
        workflow = value["state"]["workflow"]
        latest = value["state"].get("latest_event")
        if workflow["phase"] == "awaiting_initial_review":
            kind = "initial_review_timeout"
            started_text = value["created_at"]
        elif workflow["phase"] in {"owner_response", "reviewer_verification"}:
            kind = (
                "owner_timeout"
                if workflow["phase"] == "owner_response"
                else "reviewer_timeout"
            )
            started_text = latest["occurred_at"]
        else:
            print("no timeout is structurally allowed", file=sys.stderr)
            return 1
        started = datetime.fromisoformat(started_text.replace("Z", "+00:00"))
        deadline = started + TIMEOUT_DURATION_BY_KIND[kind]
        if datetime.now(timezone.utc) < deadline:
            print(
                f"timeout is not eligible until {deadline.isoformat()}", file=sys.stderr
            )
            return 1
        print(kind)
        return 0
    if args.command == "threads":
        if args.json:
            print(json.dumps(thread_conversations(value), indent=2))
        else:
            print(render_conversations(value), end="")
        return 0
    if args.command == "state":
        print(json.dumps(value["state"], indent=2))
        return 0
    if args.command == "append-event":
        try:
            event = json.loads(
                Path(args.event_json).read_text(),
                object_pairs_hook=reject_duplicate_keys,
            )
            updated = append_event(value, event)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            print(f"cannot append review event: {error}", file=sys.stderr)
            return 1
        print(json.dumps(updated, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
