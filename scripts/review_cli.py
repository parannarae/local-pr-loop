"""Command-line orchestration for repository-local review loops."""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import shlex
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

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


def run_helper(
    script: Path,
    arguments: Sequence[str],
    *,
    input_bytes: bytes | None = None,
    capture: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    """Run one bundled Python helper without invoking a shell."""
    # Flush buffered parent output first so child output cannot precede it.
    sys.stdout.flush()
    return subprocess.run(
        [sys.executable, str(script), *arguments],
        input=input_bytes,
        stdout=subprocess.PIPE if capture else None,
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


def repository_paths(value: str) -> RepositoryPaths:
    """Resolve and validate repository-local review storage."""
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
    return RepositoryPaths(root=root, local=local, reviews=reviews)


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


def snapshot_arguments(args: argparse.Namespace) -> list[str]:
    """Build source-snapshot arguments from a scoped command."""
    values: list[str] = ["--repo", str(repository_paths(args.repo).root)]
    for exclusion in args.exclude:
        values.extend(["--exclude", exclusion])
    for additional_input in args.additional_input:
        values.extend(["--additional-input", additional_input])
    values.extend(args.scope)
    return values


def command_init(args: argparse.Namespace) -> int:
    repository = repository_paths(args.repo)
    if not REVIEW_NAME_PATTERN.fullmatch(args.name):
        raise ValueError(f"invalid review name: {args.name}")
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
        )
        if not any(path.exists() or path.is_symlink() for path in artifacts):
            break
    init_args = ["init", review_id, args.name]
    if args.prior_review_id:
        init_args.extend(["--prior-review-id", args.prior_review_id])
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
        [
            "status",
            "--repo",
            str(paths.repository.root),
            "--review-file",
            str(paths.canonical),
        ],
    )
    scope_args = snapshot_arguments(args)[2:]
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
                "--",
                *scope_args,
            ],
        )
    else:
        snapshot_json = captured_helper(
            SNAPSHOT_SCRIPT,
            ["--repo", str(paths.repository.root), *scope_args],
        )
    snapshot_value = json.loads(snapshot_json)
    source = snapshot_value.get("source_snapshot", snapshot_value)
    if not args.json:
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
        "--command-prefix",
        command_prefix,
    ]
    if lease_present:
        operation_args.append("--lease-present")
    if args.json:
        operation_args.append("--json")
    result = run_helper(PUBLISH_SCRIPT, operation_args)
    if result.returncode != 0 or args.json:
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
        raise ValueError(f"event file already exists: {paths.event}")
    if paths.receipt.exists() or paths.receipt.is_symlink():
        raise ValueError(f"publication recovery is required first: {paths.receipt}")
    event = captured_helper(
        STATE_SCRIPT,
        ["context-template", args.kind, str(paths.guard)],
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
            [
                "status",
                "--repo",
                str(paths.repository.root),
                "--review-file",
                str(paths.canonical),
            ],
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


def command_start_follow_up(args: argparse.Namespace) -> int:
    prior = review_paths(args.repo, args.prior_review_id)
    validate_review(prior)
    document = load_object(prior.canonical)
    if document["state"]["workflow"]["phase"] != "terminal":
        raise ValueError("follow-up requires a terminal prior review")
    init_args = argparse.Namespace(
        repo=str(prior.repository.root),
        name=args.name,
        prior_review_id=args.prior_review_id,
    )
    return command_init(init_args)


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

    init = commands.add_parser("init")
    init.add_argument("repo")
    init.add_argument("name")
    init.set_defaults(handler=command_init, prior_review_id=None)

    validate = commands.add_parser("validate")
    add_review_selection(validate)
    validate.set_defaults(handler=command_validate)

    validate_event_parser = commands.add_parser("validate-event")
    add_review_selection(validate_event_parser)
    validate_event_parser.set_defaults(handler=command_validate_event)

    inspect = commands.add_parser("inspect")
    add_review_selection(inspect)
    inspect.add_argument("--json", action="store_true")
    add_scope_arguments(inspect)
    inspect.set_defaults(handler=command_inspect)

    template = commands.add_parser("template")
    add_review_selection(template)
    template.add_argument("kind", choices=review_state.ACTOR_BY_KIND)
    template.set_defaults(handler=command_template)

    threads = commands.add_parser("threads")
    add_review_selection(threads)
    threads.add_argument("--json", action="store_true")
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
    follow_up.set_defaults(handler=command_start_follow_up)
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
