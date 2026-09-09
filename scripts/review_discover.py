"""Resumable-loop discovery over a repository's canonical review artifacts."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import review_scope
from review_contract import SOURCE_FIELD_BY_KIND
from review_projection import validate_document
from review_schema import REVIEW_ID_PATTERN, reject_duplicate_keys

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
LOCK_SCRIPT = SCRIPT_DIRECTORY / "review_lock.py"
SNAPSHOT_SCRIPT = SCRIPT_DIRECTORY / "source_snapshot.py"

DISCOVERY_GUIDANCE = {
    "selected": (
        "resume the selected loop: inspect its routing state and act only if "
        "your role's action is currently allowed"
    ),
    "ambiguous": (
        "multiple resumable loops exist: ask the user to choose a review ID; "
        "never select one automatically, newest included"
    ),
    "none": (
        "no resumable loop exists: do not initialize one from this result "
        "alone; the owner initializes when the source is ready for review, and "
        "a reviewer only on the user's explicit request for a new review"
    ),
}


def lock_status_arguments(root: Path, canonical: Path) -> list[str]:
    """Build the token-free lock status argument vector for the lock script."""
    return ["status", "--repo", str(root), "--review-file", str(canonical)]


def load_canonical_candidate(
    path: Path, review_id: str
) -> tuple[dict[str, Any] | None, list[str]]:
    """Load one canonical review file; any returned error disqualifies it."""
    if not path.is_file() or path.is_symlink():
        return None, ["canonical review must be a regular non-symlink file"]
    try:
        document = json.loads(
            path.read_text(),
            object_pairs_hook=reject_duplicate_keys,
        )
    except (OSError, ValueError) as error:
        return None, [f"unreadable canonical JSON: {error}"]
    errors = validate_document(document)
    if isinstance(document, dict) and document.get("review_id") != review_id:
        errors.append("review_id does not match the artifact file name")
    if errors:
        return None, errors
    return document, []


def latest_guarded_snapshot(document: dict[str, Any]) -> dict[str, Any] | None:
    """Return the snapshot guarding the loop's current source, or None before any."""
    snapshot = None
    for event in document["history"]:
        if not isinstance(event, dict):
            continue
        field = SOURCE_FIELD_BY_KIND.get(event.get("kind"))
        value = event.get(field) if field else None
        if isinstance(value, dict):
            snapshot = value
    return snapshot


def scope_summary(snapshot: dict[str, Any] | None) -> dict[str, list[str]] | None:
    """Summarize the guarded scope recorded by the latest snapshot."""
    if snapshot is None:
        return None
    return {
        "scope": list(snapshot.get("scope") or []),
        "exclusions": list(snapshot.get("exclusions") or []),
        "additional_inputs": [
            entry["path"]
            for entry in snapshot.get("additional_inputs") or []
            if isinstance(entry, dict) and isinstance(entry.get("path"), str)
        ],
    }


def snapshot_drift(root: Path, snapshot: dict[str, Any] | None) -> str:
    """Compare the recorded guarded scope against the worktree's current content."""
    if snapshot is None:
        return "no_snapshot"
    summary = scope_summary(snapshot)
    declared = review_scope.declaration(
        summary["exclusions"], summary["additional_inputs"], summary["scope"]
    )
    try:
        arguments = review_scope.snapshot_arguments(str(root), declared)
    except ValueError:
        return "unavailable"
    completed = subprocess.run(
        [sys.executable, str(SNAPSHOT_SCRIPT), *arguments],
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        return "unavailable"
    try:
        fingerprint = json.loads(completed.stdout).get("fingerprint")
    except (ValueError, AttributeError):
        return "unavailable"
    return "clean" if fingerprint == snapshot.get("fingerprint") else "drifted"


def lock_summary(root: Path, canonical: Path) -> dict[str, Any]:
    """Report the cooperative lock holder's public record, never its token."""
    completed = subprocess.run(
        [sys.executable, str(LOCK_SCRIPT), *lock_status_arguments(root, canonical)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return {"held": None, "detail": "lock status unavailable"}
    text = completed.stdout.strip()
    if text == "unlocked":
        return {"held": False}
    try:
        owner = json.loads(text)
    except ValueError:
        return {"held": None, "detail": "lock status unavailable"}
    return {"held": True, "acquired_at": owner.get("acquired_at")}


def candidate_summary(
    root: Path,
    canonical: Path,
    review_id: str,
    document: dict[str, Any],
) -> dict[str, Any]:
    """Build the compact resume summary for one valid non-terminal loop."""
    state = document["state"]
    workflow = state["workflow"]
    snapshot = latest_guarded_snapshot(document)
    return {
        "review_id": review_id,
        "name": document["name"],
        "review_kind": document["review_kind"],
        "phase": workflow["phase"],
        "primary_actor": workflow["primary_actor"],
        "created_at": document["created_at"],
        "latest_event": state["latest_event"],
        "open_threads": len(state["threads"]["open"]),
        "resolved_threads": len(state["threads"]["resolved"]),
        "scope": scope_summary(snapshot),
        "source_drift": snapshot_drift(root, snapshot),
        "lock": lock_summary(root, canonical),
    }


def declared_scope(canonical: Path, document: dict[str, Any]) -> dict[str, Any] | None:
    """Return the scope a loop currently declares, or None when it declares none.

    The guard holds the live declaration and is checked first; a loop can be
    guarded before it has published anything, and reading only its history
    would report such a loop as undeclared. The latest snapshot is the fallback
    for a loop whose guard has been released.
    """
    guard = canonical.with_suffix(".guard.json")
    if guard.is_file() and not guard.is_symlink():
        try:
            stored = load_object_safely(guard)
        except (OSError, ValueError):
            stored = None
        scope = stored.get("scope") if isinstance(stored, dict) else None
        if isinstance(scope, dict):
            return {
                "scope": list(scope.get("scope") or []),
                "exclusions": list(scope.get("exclude") or []),
                "additional_inputs": list(scope.get("additional_input") or []),
            }
    return scope_summary(latest_guarded_snapshot(document))


def load_object_safely(path: Path) -> Any:
    """Read one JSON object, rejecting duplicate keys as the schema requires."""
    return json.loads(path.read_text(), object_pairs_hook=reject_duplicate_keys)


def all_live_successors(
    reviews: Path, prior_review_id: str, review_kind: str
) -> list[dict[str, Any]]:
    """Every live successor chained from a prior review of one kind.

    Normally there is at most one. More than one means two agents created a
    successor in the same instant, or a process died between creating one and
    noticing another; the caller settles that from this list rather than by
    holding a lock across creation.
    """
    found: list[dict[str, Any]] = []
    entries = sorted(reviews.iterdir()) if reviews.is_dir() else []
    for path in entries:
        if not path.name.endswith(".json"):
            continue
        review_id = path.name[: -len(".json")]
        if not REVIEW_ID_PATTERN.fullmatch(review_id):
            continue
        document, errors = load_canonical_candidate(path, review_id)
        if errors:
            continue
        if document["state"]["workflow"]["phase"] == "terminal":
            continue
        if document.get("prior_review_id") != prior_review_id:
            continue
        if document.get("review_kind") != review_kind:
            continue
        workflow = document["state"]["workflow"]
        found.append(
            {
                "review_id": review_id,
                "name": document.get("name"),
                "review_kind": document.get("review_kind"),
                "prior_review_id": document.get("prior_review_id"),
                "phase": workflow.get("phase"),
                "primary_actor": workflow.get("primary_actor"),
                "created_at": document.get("created_at") or "",
                # A loop that published nothing may be retired; one that
                # published anything holds review history and may not.
                "has_events": bool(document.get("history")),
                "scope": declared_scope(path, document),
            }
        )
    return found


def find_live_successor(
    reviews: Path, prior_review_id: str, review_kind: str
) -> dict[str, Any] | None:
    """Return the live successor already chained from a prior review, if one exists.

    A chained follow-up is identified by the prior review it continues and the
    kind of round it runs, both of which every canonical document records. At
    most one such successor may be live at a time: two would mean two agents
    doing the same round with separate thread histories.

    Terminal successors are ignored, so a later round of the same kind from the
    same prior is still possible once the first one closes.
    """
    entries = sorted(reviews.iterdir()) if reviews.is_dir() else []
    for path in entries:
        if not path.name.endswith(".json"):
            continue
        review_id = path.name[: -len(".json")]
        if not REVIEW_ID_PATTERN.fullmatch(review_id):
            continue
        document, errors = load_canonical_candidate(path, review_id)
        if errors:
            continue
        if document["state"]["workflow"]["phase"] == "terminal":
            continue
        if document.get("prior_review_id") != prior_review_id:
            continue
        if document.get("review_kind") != review_kind:
            continue
        workflow = document["state"]["workflow"]
        return {
            "review_id": review_id,
            "name": document.get("name"),
            "review_kind": document.get("review_kind"),
            "prior_review_id": document.get("prior_review_id"),
            "phase": workflow.get("phase"),
            "primary_actor": workflow.get("primary_actor"),
            # A successor that has neither a guard nor an event declares no
            # scope yet, so whoever inspects it first decides what it covers.
            # The caller is given the scope itself, which is what it must adopt,
            # rather than a flag it would have to act on indirectly.
            "scope": declared_scope(path, document),
        }
    return None


def discover_loops(root: Path, reviews: Path) -> dict[str, Any]:
    """Classify every canonical loop and select the only resumable one, if any.

    Only `REVIEW_ID.json` files are loop state; auxiliary artifacts such as
    `.event.json`, `.latest.md`, `.lease.json`, `.guard.json`, `.publish.json`,
    and `.retired.json` never make a loop discoverable. Selection reads
    canonical content only, never file modification time, and an artifact that
    fails validation is reported instead of selected.
    """
    candidates: list[dict[str, Any]] = []
    terminal: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    entries = sorted(reviews.iterdir()) if reviews.is_dir() else []
    for path in entries:
        if not path.name.endswith(".json"):
            continue
        review_id = path.name[: -len(".json")]
        if not REVIEW_ID_PATTERN.fullmatch(review_id):
            continue
        document, errors = load_canonical_candidate(path, review_id)
        if errors:
            invalid.append(
                {"review_id": review_id, "path": str(path), "errors": errors}
            )
            continue
        if document["state"]["workflow"]["phase"] == "terminal":
            terminal.append(
                {
                    "review_id": review_id,
                    "name": document["name"],
                    "outcome": document["state"]["terminal"]["outcome"],
                    "occurred_at": document["state"]["terminal"]["occurred_at"],
                }
            )
            continue
        candidates.append(candidate_summary(root, path, review_id, document))
    candidates.sort(key=lambda item: (item["created_at"], item["review_id"]))
    if len(candidates) == 1:
        status = "selected"
        selected = candidates[0]["review_id"]
    elif candidates:
        status = "ambiguous"
        selected = None
    else:
        status = "none"
        selected = None
    return {
        "status": status,
        "selected_review_id": selected,
        "candidates": candidates,
        "terminal": terminal,
        "invalid": invalid,
        "guidance": DISCOVERY_GUIDANCE[status],
    }


def render_candidate(candidate: dict[str, Any]) -> list[str]:
    """Render one candidate as the compact two-line human summary."""
    latest = candidate["latest_event"]
    when = (
        f"{latest['kind']} at {latest['occurred_at']}"
        if latest
        else f"created at {candidate['created_at']}"
    )
    scope = candidate["scope"]
    if scope is None:
        scope_text = "no guarded scope yet"
    else:
        paths = scope["scope"]
        scope_text = ", ".join(paths[:3])
        if len(paths) > 3:
            scope_text += f" (+{len(paths) - 3} more)"
        if scope["additional_inputs"]:
            scope_text += f" (+{len(scope['additional_inputs'])} additional inputs)"
    held = candidate["lock"]["held"]
    lock_text = {True: "held", False: "unlocked", None: "unknown"}[held]
    return [
        (
            f"- {candidate['review_id']} {candidate['name']} "
            f"({candidate['review_kind']}) — phase {candidate['phase']}, "
            f"actor {candidate['primary_actor']}, threads "
            f"{candidate['open_threads']} open/"
            f"{candidate['resolved_threads']} resolved"
        ),
        (
            f"  scope: {scope_text} | {when} | "
            f"drift {candidate['source_drift']} | lock {lock_text}"
        ),
    ]


def render_discovery(result: dict[str, Any]) -> str:
    """Render the discovery result for a human skim."""
    headline = f"discovery: {result['status']}"
    if result["selected_review_id"]:
        headline += f" {result['selected_review_id']}"
    lines = [headline, f"guidance: {result['guidance']}"]
    for candidate in result["candidates"]:
        lines.extend(render_candidate(candidate))
    for entry in result["terminal"]:
        lines.append(
            f"terminal: {entry['review_id']} {entry['name']} "
            f"({entry['outcome']} at {entry['occurred_at']})"
        )
    for entry in result["invalid"]:
        detail = entry["errors"][0]
        if len(entry["errors"]) > 1:
            detail += f" (+{len(entry['errors']) - 1} more)"
        lines.append(f"invalid: {entry['review_id']} — {detail}")
    return "\n".join(lines) + "\n"
