"""Command-line orchestration for repository-local review loops."""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import shlex
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import review_discover
import review_ledger
import review_scope
import review_state
from review_io import atomic_bytes, load_object
from review_schema import REVIEW_ID_PATTERN, REVIEW_NAME_PATTERN

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
LOCK_SCRIPT = SCRIPT_DIRECTORY / "review_lock.py"
PUBLISH_SCRIPT = SCRIPT_DIRECTORY / "review_publish.py"
SNAPSHOT_SCRIPT = SCRIPT_DIRECTORY / "source_snapshot.py"
STATE_SCRIPT = SCRIPT_DIRECTORY / "review_state.py"
WORKFLOW_SCRIPT = SCRIPT_DIRECTORY / "review_workflow.py"
REVIEW_ID_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


@dataclass(frozen=True)
class RepositoryPaths:
    """Resolved repository-local review storage."""

    root: Path
    local: Path
    reviews: Path


@dataclass(frozen=True)
class ReviewPaths:
    """All artifacts belonging to one selected review."""

    repository: RepositoryPaths
    review_id: str
    canonical: Path
    report: Path
    event: Path
    receipt: Path
    lease: Path
    guard: Path
    # Present only for a retired event-free review; it keeps the identifier claimed so a
    # later init can never reuse it.
    retired: Path


def run_helper(
    script: Path,
    arguments: Sequence[str],
    *,
    input_bytes: bytes | None = None,
    capture: bool = False,
    suppress_stderr: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    """Run one bundled Python helper without invoking a shell.

    Set `suppress_stderr` only where a nonzero result is an expected outcome the
    caller handles itself; helpers report refusals on stderr, which is otherwise
    inherited so real failures stay visible.
    """
    # Flush buffered parent output first so child output cannot precede it.
    sys.stdout.flush()
    return subprocess.run(
        [sys.executable, str(script), *arguments],
        input=input_bytes,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.DEVNULL if suppress_stderr else None,
        check=False,
    )


def captured_helper(
    script: Path,
    arguments: Sequence[str],
    *,
    input_bytes: bytes | None = None,
) -> bytes:
    """Run a helper and return stdout, raising on a nonzero result."""
    completed = run_helper(script, arguments, input_bytes=input_bytes, capture=True)
    if completed.returncode != 0:
        raise subprocess.CalledProcessError(completed.returncode, completed.args)
    return completed.stdout


def resolved_repository(value: str) -> RepositoryPaths:
    """Resolve repository-local review storage without requiring the ignore rule.

    Read-only discovery must still see loops in a repository whose ignore rule
    was removed after they were created; every mutating path goes through
    `repository_paths`, which enforces the rule.
    """
    completed = subprocess.run(
        ["git", "-C", value, "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError(f"not inside a Git worktree: {value}")
    root = Path(completed.stdout.strip()).resolve()
    local = root / ".local"
    reviews = local / "reviews"
    if local.is_symlink() or reviews.is_symlink():
        raise ValueError("review storage directories must not be symlinks")
    return RepositoryPaths(root=root, local=local, reviews=reviews)


def repository_paths(value: str) -> RepositoryPaths:
    """Resolve and validate repository-local review storage."""
    repository = resolved_repository(value)
    root = repository.root
    ignored = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "check-ignore",
            "-q",
            "--no-index",
            ".local/reviews/.review-loop-probe",
        ],
        check=False,
    )
    if ignored.returncode != 0:
        raise ValueError("REPO/.local must be ignored before creating a review loop")
    return repository


def review_paths(repo: str, review_id: str) -> ReviewPaths:
    """Resolve every artifact path for a validated review identifier."""
    if not REVIEW_ID_PATTERN.fullmatch(review_id):
        raise ValueError(f"invalid review ID: {review_id}")
    repository = repository_paths(repo)
    base = repository.reviews / review_id
    return ReviewPaths(
        repository=repository,
        review_id=review_id,
        canonical=base.with_suffix(".json"),
        report=base.with_suffix(".latest.md"),
        event=base.with_suffix(".event.json"),
        receipt=base.with_suffix(".publish.json"),
        lease=base.with_suffix(".lease.json"),
        guard=base.with_suffix(".guard.json"),
        retired=base.with_suffix(".retired.json"),
    )


def require_regular(path: Path, label: str) -> None:
    """Require an existing non-symlink regular file."""
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"{label} must be a regular non-symlink file: {path}")


def validate_review(paths: ReviewPaths) -> None:
    """Validate the selected canonical document and review identifier."""
    require_regular(paths.canonical, "review JSON")
    completed = run_helper(
        STATE_SCRIPT,
        ["validate", "--review-id", paths.review_id],
        input_bytes=paths.canonical.read_bytes(),
        capture=True,
    )
    if completed.returncode != 0:
        sys.stderr.buffer.write(completed.stdout)
        raise RuntimeError("canonical review validation failed")


def validate_event(paths: ReviewPaths) -> None:
    """Validate the selected temporary event."""
    require_regular(paths.event, "event JSON")
    completed = run_helper(
        STATE_SCRIPT,
        ["validate-event"],
        input_bytes=paths.event.read_bytes(),
        capture=True,
    )
    if completed.returncode != 0:
        sys.stderr.buffer.write(completed.stdout)
        raise RuntimeError("event validation failed")


def scope_declaration(args: argparse.Namespace) -> dict[str, list[str]]:
    """Build the structured scope declaration a scoped command asked for."""
    return review_scope.declaration(args.exclude, args.additional_input, args.scope)


def snapshot_arguments(args: argparse.Namespace) -> list[str]:
    """Build source-snapshot arguments from a scoped command."""
    return review_scope.snapshot_arguments(
        str(repository_paths(args.repo).root), scope_declaration(args)
    )


def resolve_comparison_base(root: Path, base_ref: str) -> str:
    """Resolve the recorded growth baseline to the merge base with HEAD.

    Resolved once at init because refs move: the ledger must measure every later
    inspection against the same commit.
    """
    completed = subprocess.run(
        ["git", "-C", str(root), "merge-base", "HEAD", base_ref],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError(
            f"cannot resolve comparison base from {base_ref}: "
            + completed.stderr.strip()
        )
    return completed.stdout.strip()


def command_discover(args: argparse.Namespace) -> int:
    repository = resolved_repository(args.repo)
    result = review_discover.discover_loops(repository.root, repository.reviews)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(review_discover.render_discovery(result), end="")
    return 0


def command_init(args: argparse.Namespace) -> int:
    repository = repository_paths(args.repo)
    if not REVIEW_NAME_PATTERN.fullmatch(args.name):
        raise ValueError(f"invalid review name: {args.name}")
    comparison_base = args.comparison_base
    if args.base_ref:
        comparison_base = resolve_comparison_base(repository.root, args.base_ref)
    repository.reviews.mkdir(parents=True, exist_ok=True)
    while True:
        review_id = "".join(
            secrets.choice(REVIEW_ID_ALPHABET) for _ in range(8)
        )
        paths = review_paths(str(repository.root), review_id)
        artifacts = (
            paths.canonical,
            paths.report,
            paths.event,
            paths.receipt,
            paths.lease,
            paths.guard,
            paths.retired,
        )
        if not any(path.exists() or path.is_symlink() for path in artifacts):
            break
    init_args = [
        "init",
        review_id,
        args.name,
        "--review-kind",
        args.review_kind,
        "--structure-policy",
        args.structure_policy,
    ]
    if args.prior_review_id:
        init_args.extend(["--prior-review-id", args.prior_review_id])
    if comparison_base:
        init_args.extend(["--comparison-base", comparison_base])
    canonical = captured_helper(STATE_SCRIPT, init_args)
    validation = run_helper(
        STATE_SCRIPT,
        ["validate", "--review-id", review_id],
        input_bytes=canonical,
        capture=True,
    )
    if validation.returncode != 0:
        sys.stderr.buffer.write(validation.stdout)
        return validation.returncode
    report = captured_helper(STATE_SCRIPT, ["report"], input_bytes=canonical)
    atomic_bytes(paths.canonical, canonical)
    atomic_bytes(paths.report, report)
    if getattr(args, "created_review_id", None) is not None:
        # A caller that must inspect the result before announcing it — the
        # chained follow-up — reads the id here and prints its own lines.
        args.created_review_id.append(review_id)
        return 0
    print(f"review_id: {review_id}")
    print(f"review_json: {paths.canonical}")
    print(f"latest_report: {paths.report}")
    return 0


def command_validate(args: argparse.Namespace) -> int:
    paths = review_paths(args.repo, args.review_id)
    require_regular(paths.canonical, "review JSON")
    return run_helper(
        STATE_SCRIPT,
        ["validate", "--review-id", paths.review_id],
        input_bytes=paths.canonical.read_bytes(),
    ).returncode


def command_validate_event(args: argparse.Namespace) -> int:
    paths = review_paths(args.repo, args.review_id)
    require_regular(paths.event, "event JSON")
    return run_helper(
        STATE_SCRIPT,
        ["validate-event"],
        input_bytes=paths.event.read_bytes(),
    ).returncode


def command_snapshot(args: argparse.Namespace) -> int:
    return run_helper(SNAPSHOT_SCRIPT, snapshot_arguments(args)).returncode


def command_scope_candidates(args: argparse.Namespace) -> int:
    """Collect the mechanical candidate set for a guarded scope.

    Emits every changed path grouped by change kind; choosing the comparison
    base and judging relevance stays with the agent.
    """
    root = str(repository_paths(args.repo).root)

    def git_lines(*arguments: str) -> list[str]:
        completed = subprocess.run(
            ["git", "-C", root, *arguments],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise ValueError(
                f"git {' '.join(arguments)} failed: {completed.stderr.strip()}"
            )
        return [line for line in completed.stdout.splitlines() if line]

    merge_base = None
    candidates: dict[str, list[str]] = {}
    if args.base_ref:
        merge_base = git_lines("merge-base", "HEAD", args.base_ref)[0]
        candidates["merge_base_diff"] = git_lines(
            "diff", "--name-only", f"{merge_base}..HEAD"
        )
    candidates["staged"] = git_lines("diff", "--name-only", "--cached")
    candidates["unstaged"] = git_lines("diff", "--name-only")
    candidates["untracked"] = git_lines("ls-files", "--others", "--exclude-standard")
    union = sorted({path for group in candidates.values() for path in group})
    print(
        json.dumps(
            {"merge_base": merge_base, "candidates": candidates, "union": union},
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def command_inspect(args: argparse.Namespace) -> int:
    paths = review_paths(args.repo, args.review_id)
    validate_review(paths)
    lock_json = captured_helper(
        LOCK_SCRIPT,
        review_discover.lock_status_arguments(paths.repository.root, paths.canonical),
    )
    # One declaration serves both branches. Transporting it as structured data keeps the
    # guarded and unguarded snapshots identical for an identical request, which an argument
    # tail re-parsed by a second process cannot guarantee.
    declaration = scope_declaration(args)
    lease_present = paths.lease.is_file() and not paths.lease.is_symlink()
    if lease_present:
        snapshot_json = captured_helper(
            WORKFLOW_SCRIPT,
            [
                "guard",
                "--repo",
                str(paths.repository.root),
                "--review",
                str(paths.canonical),
                "--lease",
                str(paths.lease),
                "--guard",
                str(paths.guard),
                "--lock-script",
                str(LOCK_SCRIPT),
                "--snapshot-script",
                str(SNAPSHOT_SCRIPT),
                "--scope-json",
                json.dumps(declaration, sort_keys=True),
            ],
        )
    else:
        snapshot_json = captured_helper(
            SNAPSHOT_SCRIPT,
            review_scope.snapshot_arguments(str(paths.repository.root), declaration),
        )
    snapshot_value = json.loads(snapshot_json)
    source = snapshot_value.get("source_snapshot", snapshot_value)
    # Both machine views replace the human report rather than decorating it, so neither
    # carries the state dump, the artifact paths, or the raw snapshot.
    compact = args.json or args.agent
    if not compact:
        print("workflow:")
        completed = run_helper(
            STATE_SCRIPT,
            ["state"],
            input_bytes=paths.canonical.read_bytes(),
        )
        if completed.returncode != 0:
            return completed.returncode
        print("operation:")
    command_prefix = (
        f"python3 {shlex.quote(str(SCRIPT_DIRECTORY / 'review_cli.py'))}"
    )
    operation_args = [
        "operation",
        "--review",
        str(paths.canonical),
        "--event",
        str(paths.event),
        "--report",
        str(paths.report),
        "--journal",
        str(paths.receipt),
        "--state-script",
        str(STATE_SCRIPT),
        "--lock-json",
        lock_json.decode().strip(),
        "--repo",
        str(paths.repository.root),
        "--review-id",
        paths.review_id,
        "--current-source-fingerprint",
        source["fingerprint"],
        # The whole snapshot, so drift can be reported per path instead of as one
        # changed fingerprint the reader has to investigate by hand.
        "--current-source-json",
        json.dumps(source, sort_keys=True),
        "--command-prefix",
        command_prefix,
    ]
    if lease_present:
        operation_args.append("--lease-present")
    if args.json:
        operation_args.append("--json")
    if args.agent:
        operation_args.append("--agent")
    if args.accretion:
        operation_args.append("--accretion")
    result = run_helper(PUBLISH_SCRIPT, operation_args)
    if result.returncode != 0 or compact:
        return result.returncode
    print(f"review_json: {paths.canonical}")
    print(f"latest_report: {paths.report}")
    print(f"event_json: {paths.event}")
    print(f"journal_json: {paths.receipt}")
    digest = hashlib.sha256(paths.canonical.read_bytes()).hexdigest()
    print(f"review_sha256: {digest}")
    print("source_snapshot:")
    sys.stdout.buffer.write(snapshot_json)
    return 0


def command_template(args: argparse.Namespace) -> int:
    paths = review_paths(args.repo, args.review_id)
    validate_review(paths)
    verified = run_helper(
        WORKFLOW_SCRIPT,
        [
            "verify",
            "--repo",
            str(paths.repository.root),
            "--review",
            str(paths.canonical),
            "--lease",
            str(paths.lease),
            "--guard",
            str(paths.guard),
            "--lock-script",
            str(LOCK_SCRIPT),
        ],
        capture=True,
    )
    if verified.returncode != 0:
        return verified.returncode
    require_regular(paths.guard, "guard")
    if paths.event.exists() or paths.event.is_symlink():
        # Naming the remedy matters: templating again is the natural retry, and without
        # this the caller keeps the stale draft and repeats whatever rejected it.
        raise ValueError(
            f"a draft already exists at {paths.event}; run abort-draft first if you "
            "need to replace it, because templating will not overwrite it"
        )
    if paths.receipt.exists() or paths.receipt.is_symlink():
        raise ValueError(f"publication recovery is required first: {paths.receipt}")
    flagged_arguments: list[str] = []
    if args.kind == "final_review":
        # Prefill the acknowledgment the publish gate will demand, computed from the
        # same guarded tree the fingerprint pins.
        document = load_object(paths.canonical)
        snapshot = load_object(paths.guard).get("source_snapshot") or {}
        flagged = review_ledger.flagged_paths(
            str(paths.repository.root),
            document,
            snapshot.get("scope") or [],
            snapshot.get("exclusions") or [],
        )
        if flagged:
            flagged_arguments = ["--flagged-json", json.dumps(flagged)]
    event = captured_helper(
        STATE_SCRIPT,
        ["context-template", args.kind, str(paths.guard), *flagged_arguments],
        input_bytes=paths.canonical.read_bytes(),
    )
    atomic_bytes(paths.event, event, mode=0o600)
    print(paths.event)
    return 0


def workflow_arguments(paths: ReviewPaths) -> list[str]:
    return [
        "--repo",
        str(paths.repository.root),
        "--review",
        str(paths.canonical),
        "--lease",
        str(paths.lease),
        "--guard",
        str(paths.guard),
        "--lock-script",
        str(LOCK_SCRIPT),
    ]


def command_lock(args: argparse.Namespace) -> int:
    paths = review_paths(args.repo, args.review_id)
    if args.action == "status":
        return run_helper(
            LOCK_SCRIPT,
            review_discover.lock_status_arguments(
                paths.repository.root, paths.canonical
            ),
        ).returncode
    if args.action == "acquire":
        validate_review(paths)
    return run_helper(
        WORKFLOW_SCRIPT,
        [args.action, *workflow_arguments(paths)],
    ).returncode


def publication_arguments(paths: ReviewPaths) -> list[str]:
    return [
        "--repo",
        str(paths.repository.root),
        "--review",
        str(paths.canonical),
        "--event",
        str(paths.event),
        "--report",
        str(paths.report),
        "--journal",
        str(paths.receipt),
        "--state-script",
        str(STATE_SCRIPT),
        "--lock-script",
        str(LOCK_SCRIPT),
        "--lease",
        str(paths.lease),
        "--guard",
        str(paths.guard),
    ]


def command_publish(args: argparse.Namespace) -> int:
    paths = review_paths(args.repo, args.review_id)
    return run_helper(
        PUBLISH_SCRIPT,
        [
            "publish",
            *publication_arguments(paths),
            "--snapshot-script",
            str(SNAPSHOT_SCRIPT),
        ],
    ).returncode


def command_recover(args: argparse.Namespace) -> int:
    paths = review_paths(args.repo, args.review_id)
    return run_helper(
        PUBLISH_SCRIPT,
        ["recover", *publication_arguments(paths)],
    ).returncode


def command_threads(args: argparse.Namespace) -> int:
    paths = review_paths(args.repo, args.review_id)
    validate_review(paths)
    state_args = ["threads"]
    if args.json:
        state_args.append("--json")
    if args.summary:
        state_args.append("--summary")
    if args.open_only:
        state_args.append("--open")
    return run_helper(
        STATE_SCRIPT,
        state_args,
        input_bytes=paths.canonical.read_bytes(),
    ).returncode


def command_abort_draft(args: argparse.Namespace) -> int:
    paths = review_paths(args.repo, args.review_id)
    return run_helper(
        WORKFLOW_SCRIPT,
        ["abort-draft", *workflow_arguments(paths), "--event", str(paths.event)],
    ).returncode


def command_add_check(args: argparse.Namespace) -> int:
    paths = review_paths(args.repo, args.review_id)
    values = [
        "add-check",
        *workflow_arguments(paths),
        "--event",
        str(paths.event),
        "--result",
        args.result,
        "--check",
        args.check,
    ]
    evidence_values = (args.basis, args.provenance, args.sanitized_result)
    if any(value is not None for value in evidence_values):
        if not all(value is not None for value in evidence_values):
            raise ValueError(
                "basis, provenance, and sanitized result must be provided together"
            )
        values.extend(
            [
                "--basis",
                args.basis,
                "--provenance",
                args.provenance,
                "--sanitized-result",
                args.sanitized_result,
            ]
        )
    if args.artifact_digest:
        values.extend(["--artifact-digest", args.artifact_digest])
    return run_helper(WORKFLOW_SCRIPT, values).returncode


def command_add_gap(args: argparse.Namespace) -> int:
    paths = review_paths(args.repo, args.review_id)
    values = [
        "add-gap",
        *workflow_arguments(paths),
        "--event",
        str(paths.event),
        "--check",
        args.check,
        "--reason",
        args.reason,
    ]
    if args.material:
        values.append("--material")
    return run_helper(WORKFLOW_SCRIPT, values).returncode


def command_add_note(args: argparse.Namespace) -> int:
    paths = review_paths(args.repo, args.review_id)
    values = [
        "add-note",
        *workflow_arguments(paths),
        "--event",
        str(paths.event),
        "--thread",
        args.thread_id,
        "--note",
        args.note,
    ]
    if args.tag:
        values.extend(["--tag", args.tag])
    return run_helper(WORKFLOW_SCRIPT, values).returncode


def command_evidence_template(args: argparse.Namespace) -> int:
    return run_helper(STATE_SCRIPT, ["evidence-template", args.basis]).returncode


def command_regenerate_report(args: argparse.Namespace) -> int:
    paths = review_paths(args.repo, args.review_id)
    verified = run_helper(
        WORKFLOW_SCRIPT,
        ["verify", *workflow_arguments(paths)],
        capture=True,
    )
    if verified.returncode != 0:
        return verified.returncode
    report = captured_helper(
        STATE_SCRIPT,
        ["report"],
        input_bytes=paths.canonical.read_bytes(),
    )
    atomic_bytes(paths.report, report)
    print('{"status":"report_regenerated"}')
    return 0


def command_wait(args: argparse.Namespace) -> int:
    paths = review_paths(args.repo, args.review_id)
    return run_helper(
        WORKFLOW_SCRIPT,
        ["wait", "--review", str(paths.canonical), "--timeout", str(args.seconds)],
    ).returncode


def command_await_handoff(args: argparse.Namespace) -> int:
    paths = review_paths(args.repo, args.review_id)
    validate_review(paths)
    return run_helper(
        WORKFLOW_SCRIPT,
        [
            "await-handoff",
            "--review",
            str(paths.canonical),
            "--round-seconds",
            str(args.round_seconds),
            "--max-rounds",
            str(args.max_rounds),
        ],
    ).returncode


def command_publish_timeout(args: argparse.Namespace) -> int:
    paths = review_paths(args.repo, args.review_id)
    kind = captured_helper(
        STATE_SCRIPT,
        ["eligible-timeout"],
        input_bytes=paths.canonical.read_bytes(),
    ).decode().strip()
    template_args = argparse.Namespace(
        repo=str(paths.repository.root),
        review_id=paths.review_id,
        kind=kind,
    )
    result = command_template(template_args)
    if result != 0:
        return result
    validate_event(paths)
    return command_publish(template_args)


def command_retire(args: argparse.Namespace) -> int:
    """Retire a review that has published nothing, so it stops blocking the next loop.

    A review with no history carries no findings, no decisions, and no approval, so there
    is nothing for a timeout clock to protect. Any review that has published even one
    event keeps the ordinary timeout and terminal paths instead.
    """

    paths = review_paths(args.repo, args.review_id)
    validate_review(paths)
    require_retirable(paths, load_object(paths.canonical), include_lease=True)

    # Take the cooperative lock and re-check inside it. Guard creation verifies a lease
    # and publication verifies the lock, so holding it is what stops either from starting
    # between the checks and the removal of canonical state.
    # Convergence races another agent for this same duplicate and handles the
    # loss itself, so its diagnostics would only make a successful run look
    # broken. Every other caller reports the refusal.
    expects_contention = getattr(args, "quiet", False)
    acquired = run_helper(
        WORKFLOW_SCRIPT,
        ["acquire", *workflow_arguments(paths)],
        capture=True,
        suppress_stderr=expects_contention,
    )
    if acquired.returncode != 0:
        if not expects_contention:
            sys.stderr.buffer.write(acquired.stdout)
        raise RuntimeError(
            f"review {paths.review_id} could not be locked for retirement; another "
            "process holds it"
        )
    try:
        validate_review(paths)
        document = load_object(paths.canonical)
        # The lease is ours now, so it is not evidence of another holder.
        require_retirable(paths, document, include_lease=False)
        retire_locked_review(paths, document, args.reason)
    finally:
        run_helper(WORKFLOW_SCRIPT, ["release", *workflow_arguments(paths)], capture=True)
    print(json.dumps({"status": "retired", "review_id": paths.review_id}, sort_keys=True))
    return 0


def require_retirable(
    paths: ReviewPaths, document: dict, *, include_lease: bool
) -> None:
    """Refuse to retire a review that carries history or in-flight work."""

    if document.get("history"):
        raise ValueError(
            f"review {paths.review_id} has published events and cannot be retired; "
            "let it reach a terminal event or an eligible timeout instead"
        )
    artifacts = [
        (paths.event, "a draft"),
        (paths.receipt, "a publication receipt"),
        (paths.guard, "an inspection guard"),
    ]
    if include_lease:
        artifacts.append((paths.lease, "a lock lease"))
    for path, label in artifacts:
        if path.exists() or path.is_symlink():
            raise ValueError(
                f"review {paths.review_id} still has {label} at {path}; resolve it "
                "before retiring the review"
            )


def retire_locked_review(paths: ReviewPaths, document: dict, reason: str) -> None:
    """Record the disposal and remove active canonical state, under the held lock."""

    disposal = {
        "review_id": paths.review_id,
        "name": document.get("name"),
        "created_at": document.get("created_at"),
        "prior_review_id": document.get("prior_review_id"),
        "retired_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
    }
    # The tombstone lands before canonical state goes, so a crash between the two leaves
    # the identifier claimed rather than silently reusable.
    atomic_bytes(
        paths.retired,
        (json.dumps(disposal, indent=2, sort_keys=True) + "\n").encode(),
        mode=0o600,
    )
    paths.canonical.unlink()
    paths.report.unlink(missing_ok=True)


def live_successors(
    reviews: Path, prior_review_id: str, review_kind: str
) -> list[dict[str, Any]]:
    """Every live successor chained from a prior review, in tie-break order.

    Ordered by creation time then review id, so two agents reading the same
    canonical set independently agree on which one is the winner.
    """
    found = review_discover.all_live_successors(
        reviews, prior_review_id, review_kind
    )
    return sorted(found, key=lambda item: (item["created_at"], item["review_id"]))


def select_successor(
    reviews: Path, prior_review_id: str, review_kind: str
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Choose the live successor that wins, and the duplicates it displaces.

    Reads canonical documents and decides; it retires nothing. Keeping the
    decision free of side effects is what lets a caller reject the request —
    a follow-up naming a different round — before any loop is touched.

    A successor that published anything holds review history and wins outright.
    Creation order decides only among loops that published nothing, so the
    tie-break can never discard history to satisfy an ordering.

    Raises:
        ValueError: Two successors have published events, so neither can be
            discarded without destroying review history. A person chooses.
    """
    live = live_successors(reviews, prior_review_id, review_kind)
    if not live:
        return None, []
    published = [item for item in live if item["has_events"]]
    if len(published) > 1:
        raise ValueError(
            "more than one live successor has published events for this prior "
            "review and kind: "
            + ", ".join(item["review_id"] for item in published)
            + ". Continue the one you meant and retire or supersede the other."
        )
    winner = published[0] if published else live[0]
    # Every duplicate is eventless here: at most one successor has history, and
    # that one is the winner. Retirability is what the rule protects.
    duplicates = [item for item in live if item["review_id"] != winner["review_id"]]
    return winner, duplicates


def retire_displaced_successors(
    repo: str,
    reviews: Path,
    prior_review_id: str,
    review_kind: str,
    winner: dict[str, Any],
    duplicates: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Retire the displaced duplicates and return the successor that survived.

    Two agents can pass the scan in the same instant and both create, so rather
    than hold a lock across creation this settles the outcome afterwards from
    the canonical documents themselves. Both agents select the same winner, and
    the loser retires its own loop — which is exactly what retirement is for,
    since a just-created loop has published nothing.

    Settling afterwards also repairs a duplicate left by a process that died
    between creating and checking, which a lock could not do.

    Raises:
        RuntimeError: Duplicates outlived the attempt to retire them, so the
            caller cannot be handed the one live successor it asked for.
    """
    failures: list[str] = []
    for duplicate in duplicates:
        retire_arguments = argparse.Namespace(
            repo=repo,
            review_id=duplicate["review_id"],
            quiet=True,
            reason=(
                "duplicate chained follow-up: another successor for the same "
                f"prior review and kind won the tie-break ({winner['review_id']})"
            ),
        )
        try:
            command_retire(retire_arguments)
        except Exception as error:
            # Contention with another agent converging on the same duplicate and
            # a genuine validation, permission, or I/O failure arrive here as the
            # same exception types, so the error cannot tell them apart. Whether
            # it mattered is settled below by whether the duplicate is gone.
            failures.append(f"{duplicate['review_id']}: {error}")

    remaining = live_successors(reviews, prior_review_id, review_kind)
    if len(remaining) > 1 and failures:
        # An agent retiring the same duplicate is briefly mid-flight; give it the
        # window creation already uses before treating leftovers as a real failure.
        time.sleep(SUCCESSOR_SETTLE_SECONDS)
        remaining = live_successors(reviews, prior_review_id, review_kind)
    if len(remaining) > 1:
        raise RuntimeError(
            "chained successors did not converge to one: "
            + ", ".join(item["review_id"] for item in remaining)
            + ". Retirement did not take effect ("
            + ("; ".join(failures) if failures else "no failure was reported")
            + "). Retire the duplicate yourself, then run this command again."
        )
    return remaining[0] if remaining else None


def converge_successors(
    repo: str, reviews: Path, prior_review_id: str, review_kind: str
) -> dict[str, Any] | None:
    """Select the winning successor and retire the duplicates it displaces.

    For a caller that has already created its own loop, where settling cannot be
    withheld pending a name check: the creation has happened either way, so the
    duplicate it made must be resolved. Such a caller can still refuse the
    request afterwards, when the winner turns out to carry a different name.
    """
    winner, duplicates = select_successor(reviews, prior_review_id, review_kind)
    if winner is None:
        return None
    return retire_displaced_successors(
        repo, reviews, prior_review_id, review_kind, winner, duplicates
    )


def report_existing_successor(
    repo: str, successor: dict[str, object], requested_name: str
) -> int:
    """Report the live successor a chained follow-up resolves to.

    A different requested name means the caller wanted a different round than
    the one already running, so it stops rather than quietly handing back
    someone else's loop and its threads.
    """
    review_id = str(successor["review_id"])
    if successor.get("name") != requested_name:
        print(
            f"a live successor already exists for this prior review and kind, "
            f"under a different name: {review_id} is "
            f"{successor.get('name')!r}, you asked for {requested_name!r}. "
            "Join that review if it is the round you meant, or start an "
            "independent review with init.",
            file=sys.stderr,
        )
        return 1
    paths = review_paths(repo, review_id)
    print("status: existing")
    for field in (
        "review_id",
        "name",
        "review_kind",
        "prior_review_id",
        "phase",
        "primary_actor",
    ):
        print(f"{field}: {successor.get(field)}")
    # Reported as the paths themselves, not as a flag: this output is a text
    # channel, and the caller's next act is to adopt this scope verbatim.
    scope = successor.get("scope")
    if not isinstance(scope, dict):
        print("scope: not yet declared")
    else:
        print(f"scope: {' '.join(scope['scope'])}")
        if scope["exclusions"]:
            print(f"exclusions: {' '.join(scope['exclusions'])}")
        if scope["additional_inputs"]:
            print(f"additional_inputs: {' '.join(scope['additional_inputs'])}")
    print(f"review_json: {paths.canonical}")
    print(f"latest_report: {paths.report}")
    return 0


# How long a freshly created successor waits before deciding which loop won.
# Covers the skew between two agents writing their canonical files; it is not a
# correctness bound, and a competitor slower than this still converges on the
# next command that scans successors.
SUCCESSOR_SETTLE_SECONDS = 0.4


def command_start_follow_up(args: argparse.Namespace) -> int:
    prior = review_paths(args.repo, args.prior_review_id)
    validate_review(prior)
    document = load_object(prior.canonical)
    if document["state"]["workflow"]["phase"] != "terminal":
        raise ValueError("follow-up requires a terminal prior review")
    repo = str(prior.repository.root)
    reviews = prior.repository.reviews

    # Chaining is idempotent on the prior review and the kind of round: the
    # terminal dashboard recommends this command to whichever role runs it, so
    # both roles may reach it and only one successor may be live.
    #
    # Selection is read-only, so a request naming a different round is refused
    # while the loops are still untouched. A refusal must leave the repository
    # exactly as it found it; only an accepted request settles duplicates.
    existing, displaced = select_successor(
        reviews, args.prior_review_id, args.review_kind
    )
    if existing is not None:
        if existing.get("name") != args.name:
            return report_existing_successor(repo, existing, args.name)
        settled = retire_displaced_successors(
            repo, reviews, args.prior_review_id, args.review_kind, existing, displaced
        )
        # A competitor can retire the selected loop between the two steps, which
        # leaves nothing to join; falling through creates the round as asked.
        if settled is not None:
            return report_existing_successor(repo, settled, args.name)

    # The growth baseline and chaining policy carry over unless overridden, so a
    # successor keeps measuring the same branch the same way.
    created: list[str] = []
    init_args = argparse.Namespace(
        repo=repo,
        name=args.name,
        prior_review_id=args.prior_review_id,
        review_kind=args.review_kind,
        structure_policy=args.structure_policy or document.get("structure_policy"),
        comparison_base=document.get("comparison_base"),
        base_ref=args.base_ref,
        created_review_id=created,
    )
    result = command_init(init_args)
    if result != 0 or not created:
        return result

    # Check again now the loop exists: a second agent may have created one in
    # the same instant. Whoever loses the selection has its own loop retired
    # here and reports the winner instead of a second round nobody wanted.
    #
    # The no-side-effect promise made above does not reach this path and cannot:
    # this call has already created a loop, so every outcome from here changes
    # state, and undoing the creation would itself be a retirement. A loser
    # whose winner carries a different name therefore settles first and refuses
    # after, which is the one rejection that leaves a retired duplicate behind.
    #
    # Settle first. Convergence can only weigh the successors it can see, so a
    # caller that scans before a competitor's file lands would announce an ID
    # that competitor is about to retire. Both callers reach this point within
    # milliseconds of each other, so pausing longer than that skew makes each
    # one visible to the other before either decides.
    time.sleep(SUCCESSOR_SETTLE_SECONDS)
    winner = converge_successors(repo, reviews, args.prior_review_id, args.review_kind)
    if winner is not None and winner["review_id"] != created[0]:
        return report_existing_successor(repo, winner, args.name)

    paths = review_paths(repo, created[0])
    print("status: created")
    print(f"review_id: {created[0]}")
    print(f"review_json: {paths.canonical}")
    print(f"latest_report: {paths.report}")
    return 0


def add_scope_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--exclude", action="append", default=[])
    parser.add_argument("--additional-input", action="append", default=[])
    parser.add_argument("scope", nargs="+")


def add_review_selection(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("repo")
    parser.add_argument("review_id")


def build_parser() -> argparse.ArgumentParser:
    """Build the complete public command model."""
    parser = argparse.ArgumentParser(prog="review_cli.py")
    commands = parser.add_subparsers(dest="command", required=True)

    discover = commands.add_parser("discover")
    discover.add_argument("repo")
    discover.add_argument("--json", action="store_true")
    discover.set_defaults(handler=command_discover)

    init = commands.add_parser("init")
    init.add_argument("repo")
    init.add_argument("name")
    init.add_argument(
        "--kind",
        dest="review_kind",
        choices=("correctness", "structure"),
        default="correctness",
    )
    init.add_argument(
        "--structure",
        dest="structure_policy",
        choices=("auto", "defer", "off"),
        default="auto",
    )
    init.add_argument(
        "--base-ref",
        dest="base_ref",
        help="Comparison base ref; its merge base with HEAD anchors the growth signal",
    )
    init.set_defaults(handler=command_init, prior_review_id=None, comparison_base=None)

    validate = commands.add_parser("validate")
    add_review_selection(validate)
    validate.set_defaults(handler=command_validate)

    validate_event_parser = commands.add_parser("validate-event")
    add_review_selection(validate_event_parser)
    validate_event_parser.set_defaults(handler=command_validate_event)

    inspect = commands.add_parser("inspect")
    add_review_selection(inspect)
    view = inspect.add_mutually_exclusive_group()
    view.add_argument("--json", action="store_true")
    view.add_argument(
        "--agent",
        action="store_true",
        help="Print only the phase-scoped operating card",
    )
    inspect.add_argument(
        "--accretion",
        action="store_true",
        help="Include the accretion ledger even when no final_review is allowed",
    )
    add_scope_arguments(inspect)
    inspect.set_defaults(handler=command_inspect)

    template = commands.add_parser("template")
    add_review_selection(template)
    template.add_argument("kind", choices=review_state.ACTOR_BY_KIND)
    template.set_defaults(handler=command_template)

    threads = commands.add_parser("threads")
    add_review_selection(threads)
    threads.add_argument("--json", action="store_true")
    threads.add_argument(
        "--summary",
        action="store_true",
        help="Return identity, priority, status, title, and paths without bodies",
    )
    threads.add_argument(
        "--open",
        dest="open_only",
        action="store_true",
        help="Restrict the output to threads that are currently open",
    )
    threads.set_defaults(handler=command_threads)

    add_check = commands.add_parser("add-check")
    add_review_selection(add_check)
    add_check.add_argument("result", choices=("passed", "failed"))
    add_check.add_argument("check")
    add_check.add_argument("basis", nargs="?")
    add_check.add_argument("provenance", nargs="?")
    add_check.add_argument("sanitized_result", nargs="?")
    add_check.add_argument("artifact_digest", nargs="?")
    add_check.set_defaults(handler=command_add_check)

    add_gap = commands.add_parser("add-gap")
    add_review_selection(add_gap)
    add_gap.add_argument("check")
    add_gap.add_argument("reason")
    add_gap.add_argument("--material", action="store_true")
    add_gap.set_defaults(handler=command_add_gap)

    add_note = commands.add_parser("add-note")
    add_review_selection(add_note)
    add_note.add_argument("thread_id")
    add_note.add_argument("note")
    add_note.add_argument(
        "--tag", choices=("action-required", "follow-up", "decision")
    )
    add_note.set_defaults(handler=command_add_note)

    evidence = commands.add_parser("evidence-template")
    evidence.add_argument("basis", choices=review_state.EVIDENCE_BASES)
    evidence.set_defaults(handler=command_evidence_template)

    snapshot = commands.add_parser("snapshot")
    snapshot.add_argument("repo")
    add_scope_arguments(snapshot)
    snapshot.set_defaults(handler=command_snapshot)

    scope_candidates = commands.add_parser("scope-candidates")
    scope_candidates.add_argument("repo")
    scope_candidates.add_argument("base_ref", nargs="?")
    scope_candidates.set_defaults(handler=command_scope_candidates)

    lock = commands.add_parser("lock")
    lock.add_argument("action", choices=("acquire", "status", "release"))
    add_review_selection(lock)
    lock.set_defaults(handler=command_lock)

    for name, handler in (
        ("publish", command_publish),
        ("recover-publish", command_recover),
        ("abort-draft", command_abort_draft),
        ("regenerate-report", command_regenerate_report),
    ):
        child = commands.add_parser(name)
        add_review_selection(child)
        child.set_defaults(handler=handler)

    wait = commands.add_parser("wait")
    add_review_selection(wait)
    wait.add_argument("seconds", nargs="?", type=int, default=300)
    wait.set_defaults(handler=command_wait)

    await_handoff = commands.add_parser("await-handoff")
    add_review_selection(await_handoff)
    await_handoff.add_argument("--round-seconds", type=int, default=300)
    await_handoff.add_argument("--max-rounds", type=int, default=24)
    await_handoff.set_defaults(handler=command_await_handoff)

    timeout = commands.add_parser("publish-timeout")
    add_review_selection(timeout)
    timeout.add_argument("--if-eligible", action="store_true", required=True)
    timeout.set_defaults(handler=command_publish_timeout)

    follow_up = commands.add_parser("start-follow-up")
    follow_up.add_argument("repo")
    follow_up.add_argument("prior_review_id")
    follow_up.add_argument("name")
    follow_up.add_argument(
        "--kind",
        dest="review_kind",
        choices=("correctness", "structure"),
        default="correctness",
    )
    follow_up.add_argument(
        "--structure",
        dest="structure_policy",
        choices=("auto", "defer", "off"),
        help="Chaining policy; inherited from the prior review when omitted",
    )
    follow_up.add_argument(
        "--base-ref",
        dest="base_ref",
        help="Override the inherited comparison base with this ref's merge base",
    )
    follow_up.set_defaults(handler=command_start_follow_up)

    retire = commands.add_parser("retire")
    add_review_selection(retire)
    retire.add_argument(
        "--reason",
        default="",
        help="Why this event-free review is being retired",
    )
    retire.set_defaults(handler=command_retire)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.handler(args)
    except (OSError, TypeError, ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as error:
        return error.returncode


if __name__ == "__main__":
    raise SystemExit(main())
