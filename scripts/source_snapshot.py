"""Create a deterministic fingerprint for a declared Git review scope."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path


def git(repo: Path, *args: str, text: bool = False) -> bytes | str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=text,
    )
    return result.stdout


def normalize_scope(repo: Path, values: list[str]) -> list[str]:
    normalized = []
    for value in values:
        path = Path(os.path.abspath(repo / value))
        try:
            relative = path.relative_to(repo)
        except ValueError:
            raise ValueError(f"scope is outside repository: {value}") from None
        normalized.append(relative.as_posix() or ".")
    return sorted(set(normalized))


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def untracked_manifest(repo: Path, pathspecs: list[str]) -> list[dict[str, str]]:
    output = git(
        repo,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
        "--",
        *pathspecs,
    )
    entries = []
    for raw_path in sorted(path for path in output.split(b"\0") if path):
        relative = os.fsdecode(raw_path)
        path = repo / relative
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            link_target = os.readlink(path)
            content = link_target.encode()
            kind = "symlink"
        elif stat.S_ISREG(metadata.st_mode):
            content = path.read_bytes()
            kind = "file"
        else:
            raise ValueError(f"untracked input must be a regular file or symlink: {relative}")
        entry = {
            "path": relative,
            "kind": kind,
            "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
            "sha256": digest_bytes(content),
        }
        if kind == "symlink":
            entry["link_target"] = link_target
        entries.append(entry)
    return entries


def additional_input_manifest(repo: Path, values: list[str]) -> list[dict[str, str]]:
    entries = []
    for relative in normalize_scope(repo, values):
        path = repo / relative
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            link_target = os.readlink(path)
            resolved = path.resolve(strict=True)
            if not resolved.is_file():
                raise ValueError(
                    f"additional input symlink must resolve to a regular file: {relative}"
                )
            content = resolved.read_bytes()
            kind = "symlink"
        elif stat.S_ISREG(metadata.st_mode):
            content = path.read_bytes()
            kind = "file"
        else:
            raise ValueError(f"additional input must be a file or symlink: {relative}")
        entry = {
            "path": relative,
            "kind": kind,
            "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
            "sha256": digest_bytes(content),
        }
        if kind == "symlink":
            entry["link_target"] = link_target
        entries.append(entry)
    return entries


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=".", help="Git repository root")
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Repository-relative path excluded from the review scope",
    )
    parser.add_argument(
        "--additional-input",
        action="append",
        default=[],
        help="Ignored or generated file whose digest must be guarded",
    )
    parser.add_argument(
        "scope",
        nargs="+",
        help="Repository-relative files or directories included in the review",
    )
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    try:
        top_level = Path(str(git(repo, "rev-parse", "--show-toplevel", text=True)).strip())
        repo = top_level.resolve()
        scope = normalize_scope(repo, args.scope)
        exclusions = normalize_scope(repo, args.exclude)
        pathspecs = [
            *(f":(literal){path}" for path in scope),
            *(f":(exclude,literal){path}" for path in exclusions),
        ]
        revision = str(git(repo, "rev-parse", "HEAD", text=True)).strip()
        staged = git(
            repo,
            "diff",
            "--cached",
            "--binary",
            "--full-index",
            "--no-ext-diff",
            "--",
            *pathspecs,
        )
        unstaged = git(
            repo,
            "diff",
            "--binary",
            "--full-index",
            "--no-ext-diff",
            "--",
            *pathspecs,
        )
        payload = {
            "revision": revision,
            "scope": scope,
            "exclusions": exclusions,
            "additional_inputs": additional_input_manifest(repo, args.additional_input),
            "staged_sha256": digest_bytes(staged),
            "unstaged_sha256": digest_bytes(unstaged),
            "untracked": untracked_manifest(repo, pathspecs),
        }
    except (OSError, subprocess.CalledProcessError, ValueError) as error:
        print(f"source snapshot failed: {error}", file=sys.stderr)
        return 1

    canonical = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    result = {
        **payload,
        "fingerprint": digest_bytes(canonical.encode()),
    }
    print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
