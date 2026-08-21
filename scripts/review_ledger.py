"""Derive the accretion ledger: review attention and growth one loop deposited per file.

The ledger is derived state, like operation status: recomputable from canonical history
plus Git, never written to canonical JSON. Growth is measured from the document's
`comparison_base` — the Git-side baseline recorded at init — rather than from review
artifacts, so the signal accumulates across sequential loops on one branch without any
linkage between review IDs and survives the loss of prior loops' local artifacts.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import review_scope

# A file this many raised threads name is accretion-flagged.
THREAD_FLAG_THRESHOLD = 5
# A file whose net line growth since the comparison base exceeds this ratio is
# accretion-flagged. A file absent or empty at the base is authored whole on the branch
# and is judged by the reviewer instead of auto-flagged.
GROWTH_FLAG_THRESHOLD = 0.20


def thread_counts(history: Any) -> dict[str, int]:
    """Count how many raised threads name each path in their `paths` field."""

    counts: dict[str, int] = {}
    if not isinstance(history, list):
        return counts
    for event in history:
        if not isinstance(event, dict):
            continue
        for thread in [*event.get("threads", []), *event.get("new_threads", [])]:
            if not isinstance(thread, dict):
                continue
            paths = thread.get("paths")
            if not isinstance(paths, list):
                continue
            for path in paths:
                if isinstance(path, str) and path:
                    counts[path] = counts.get(path, 0) + 1
    return counts


def _in_scope(path: str, scope: list[str], exclusions: list[str]) -> bool:
    """Report whether a repository-relative path is inside the guarded scope."""

    normalized = review_scope.normalize_path(path)
    included = any(
        review_scope.covers(review_scope.normalize_path(entry), normalized)
        for entry in scope
    )
    excluded = any(
        review_scope.covers(review_scope.normalize_path(entry), normalized)
        for entry in exclusions
    )
    return included and not excluded


def _git(repository_root: str, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    # Output stays bytes: file paths and base file content are not obliged to be
    # valid UTF-8, and a strict decode here would abort the whole signal.
    return subprocess.run(
        ["git", "-C", repository_root, *arguments],
        capture_output=True,
        check=False,
    )


def growth_by_file(
    repository_root: str,
    comparison_base: str,
    scope: list[str],
    exclusions: list[str],
) -> dict[str, dict[str, Any]]:
    """Measure per-file line growth from the comparison base to the working tree.

    Only net growth participates: a file that shrank or broke even can never cross a
    positive growth threshold, so its base line count is not probed. Returns
    `base_lines` as None for a file absent at the base. Binary files report no line
    counts and are skipped. Renames are disabled so every path names itself.
    """

    pathspecs = [
        *(f":(literal){path}" for path in scope),
        *(f":(exclude,literal){path}" for path in exclusions),
    ]
    # quotePath off keeps non-ASCII path bytes raw instead of C-quoted, so the key
    # below matches the scoped name and the `git show` spelling resolves.
    numstat = _git(
        repository_root,
        "-c",
        "core.quotePath=false",
        "diff",
        "--numstat",
        "--no-renames",
        comparison_base,
        "--",
        *pathspecs,
    )
    if numstat.returncode != 0:
        raise ValueError(
            f"cannot diff against comparison base {comparison_base}: "
            + numstat.stderr.decode(errors="replace").strip()
        )
    growth: dict[str, dict[str, Any]] = {}
    for line in numstat.stdout.splitlines():
        parts = line.split(b"\t")
        if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
            # Binary entries report "-" counts; anything else malformed is skipped
            # rather than aborting the signal.
            continue
        added, deleted = int(parts[0]), int(parts[1])
        if added <= deleted:
            continue
        # Surrogate-escaped so an undecodable path byte round-trips through the
        # argv encoding back to the identical file.
        path = os.fsdecode(parts[2])
        shown = _git(repository_root, "show", f"{comparison_base}:{path}")
        base_lines = (
            len(shown.stdout.splitlines()) if shown.returncode == 0 else None
        )
        growth[path] = {"base_lines": base_lines, "added": added, "deleted": deleted}
    return growth


def ledger(
    repository_root: str,
    document: dict[str, Any],
    scope: list[str],
    exclusions: list[str],
) -> dict[str, Any]:
    """Build the accretion ledger for one loop against the current working tree.

    Both signals are confined to the guarded scope minus exclusions: only a guarded
    file can demand a `structure_debt` acknowledgment, so a thread naming an
    out-of-scope or mistyped path is dropped here rather than flagged.
    """

    counts = {
        path: count
        for path, count in thread_counts(document.get("history")).items()
        if _in_scope(path, scope, exclusions)
    }
    comparison_base = document.get("comparison_base")
    # A base this clone cannot reach degrades to the thread signal instead of failing:
    # blocking final_review over a missing baseline commit would brick the loop on any
    # machine that lacks it, and the dashboard reports the degradation instead.
    base_reachable = isinstance(comparison_base, str) and bool(comparison_base)
    growth: dict[str, dict[str, Any]] = {}
    if base_reachable:
        try:
            growth = growth_by_file(repository_root, comparison_base, scope, exclusions)
        except ValueError:
            base_reachable = False
    files: dict[str, dict[str, Any]] = {}
    for path in sorted(set(counts) | set(growth)):
        entry: dict[str, Any] = {"threads": counts.get(path, 0), "growth": None}
        flags: list[str] = []
        if entry["threads"] >= THREAD_FLAG_THRESHOLD:
            flags.append("threads")
        record = growth.get(path)
        if record is not None:
            entry.update(record)
            base_lines = record["base_lines"]
            if isinstance(base_lines, int) and base_lines > 0:
                ratio = (record["added"] - record["deleted"]) / base_lines
                entry["growth"] = round(ratio, 4)
                if ratio > GROWTH_FLAG_THRESHOLD:
                    flags.append("growth")
        entry["flags"] = flags
        files[path] = entry
    return {
        "comparison_base": comparison_base,
        "comparison_base_reachable": base_reachable,
        "thresholds": {
            "threads": THREAD_FLAG_THRESHOLD,
            "growth": GROWTH_FLAG_THRESHOLD,
        },
        "files": files,
        "flagged": sorted(path for path, entry in files.items() if entry["flags"]),
    }


def flagged_paths(
    repository_root: str,
    document: dict[str, Any],
    scope: list[str],
    exclusions: list[str],
) -> list[str]:
    """Return the flagged set one correctness final_review must acknowledge.

    Empty for a structure round and under policy `off`, where the ledger never
    participates in the loop's obligations.
    """

    if document.get("review_kind") != "correctness":
        return []
    if document.get("structure_policy") == "off":
        return []
    return ledger(repository_root, document, scope, exclusions)["flagged"]


def acknowledgment_error(
    document: dict[str, Any], event: dict[str, Any], flagged: list[str]
) -> str | None:
    """Return why this final_review cannot publish against the flagged set, or None.

    Enforced at publish time, where the guarded tree is pinned by the source
    fingerprint; projection never re-checks it, so terminal history stays valid after
    the tree moves on.
    """

    if event.get("kind") != "final_review":
        return None
    debt = event.get("structure_debt")
    if document.get("review_kind") == "structure":
        return (
            "a structure round records no structure_debt; remove the field"
            if debt is not None
            else None
        )
    if flagged:
        if debt is None:
            return (
                "accretion-flagged files require a structure_debt acknowledgment on "
                "final_review: " + ", ".join(flagged)
            )
        recorded = sorted(debt.get("flagged_paths") or []) if isinstance(debt, dict) else []
        if recorded != sorted(flagged):
            return (
                "structure_debt.flagged_paths does not match the currently flagged "
                "set: expected " + ", ".join(sorted(flagged))
            )
        return None
    if debt is not None:
        return "structure_debt is present but no file is accretion-flagged"
    return None


def deferred_structure_debt(document: dict[str, Any]) -> dict[str, Any] | None:
    """Return the structure_debt this loop's final_review recorded as deferred."""

    history = document.get("history")
    if not isinstance(history, list):
        return None
    for event in reversed(history):
        if isinstance(event, dict) and event.get("kind") == "final_review":
            debt = event.get("structure_debt")
            if (
                isinstance(debt, dict)
                and debt.get("disposition") == "structure_deferred"
            ):
                return debt
            return None
    return None


def has_structure_successor(reviews_directory: Path, review_id: str) -> bool:
    """Report whether a structure round already follows this loop.

    One structure round consumes the flag set that triggered it, so its existence
    stops a re-inspected terminal from recommending another.
    """

    for canonical in sorted(reviews_directory.glob("*.json")):
        # Only "<id>.json" is canonical; drafts, guards, leases and receipts are not.
        if canonical.name.count(".") != 1:
            continue
        try:
            sibling = json.loads(canonical.read_text())
        except (OSError, ValueError):
            continue
        if (
            isinstance(sibling, dict)
            and sibling.get("prior_review_id") == review_id
            and sibling.get("review_kind") == "structure"
        ):
            return True
    return False


def structure_follow_up_due(document: dict[str, Any], reviews_directory: Path) -> bool:
    """Decide whether this terminal should recommend chaining a structure round."""

    if document.get("review_kind") != "correctness":
        return False
    if document.get("structure_policy") != "auto":
        return False
    if deferred_structure_debt(document) is None:
        return False
    return not has_structure_successor(reviews_directory, document.get("review_id"))
