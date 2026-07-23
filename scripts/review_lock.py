#!/usr/bin/env python3
"""Acquire or release the cooperative lock for a local review file."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def repository_root(value: str) -> Path:
    result = subprocess.run(
        ["git", "-C", value, "rev-parse", "--show-toplevel"],
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(result.stdout.strip()).resolve()


def lock_directory(repo: Path, review_file: str) -> Path:
    # Keep lock identity stable even if a damaged review path becomes a symlink.
    identity = os.path.abspath(review_file).encode()
    suffix = hashlib.sha256(identity).hexdigest()[:16]
    git_dir = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--git-path", "codex-review-locks"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    base = Path(git_dir)
    if not base.is_absolute():
        base = repo / base
    return base / suffix


def read_owner(path: Path) -> dict[str, object]:
    owner = json.loads((path / "owner.json").read_text())
    if not isinstance(owner, dict):
        raise ValueError("review lock holder record must be a JSON object")
    if not isinstance(owner.get("token"), str) or not owner["token"]:
        raise ValueError("review lock holder token is missing")
    return owner


def public_owner(owner: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in owner.items() if key != "token"}


def acquire(path: Path, review_file: str) -> int:
    token = secrets.token_hex(16)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.mkdir()
    except FileExistsError:
        try:
            owner = json.dumps(public_owner(read_owner(path)), indent=2)
        except (FileNotFoundError, json.JSONDecodeError, ValueError):
            owner = "holder unavailable"
        print(f"review lock is already held: {path}\n{owner}", file=sys.stderr)
        return 1

    owner = {
        "token": token,
        "review_file": str(Path(review_file).resolve()),
        "acquired_at": datetime.now(timezone.utc).isoformat(),
        "pid": os.getpid(),
    }
    owner_path = path / "owner.json"
    temporary = path / "owner.json.tmp"
    try:
        temporary.write_text(json.dumps(owner, indent=2) + "\n")
        os.replace(temporary, owner_path)
    except OSError:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        try:
            path.rmdir()
        except OSError:
            pass
        raise
    print(json.dumps({"lock": str(path), "token": token}, sort_keys=True))
    return 0


def release(path: Path, token: str) -> int:
    owner_path = path / "owner.json"
    try:
        owner = read_owner(path)
    except (FileNotFoundError, json.JSONDecodeError) as error:
        print(f"cannot read review lock holder: {error}", file=sys.stderr)
        return 1
    if not secrets.compare_digest(owner.get("token", ""), token):
        print("review lock token does not match", file=sys.stderr)
        return 1
    owner_path.unlink()
    path.rmdir()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("acquire", "release", "status", "verify"))
    parser.add_argument("--repo", default=".")
    parser.add_argument("--review-file", required=True)
    parser.add_argument("--token")
    args = parser.parse_args()

    try:
        repo = repository_root(args.repo)
        path = lock_directory(repo, args.review_file)
        if args.action == "acquire":
            return acquire(path, args.review_file)
        if args.action == "release":
            if not args.token:
                parser.error("--token is required for release")
            return release(path, args.token)
        if args.action == "verify":
            if not args.token:
                parser.error("--token is required for verify")
            owner = read_owner(path)
            if not secrets.compare_digest(owner.get("token", ""), args.token):
                print("review lock token does not match", file=sys.stderr)
                return 1
            print("lock verified")
            return 0
        if path.exists():
            print(json.dumps(public_owner(read_owner(path)), indent=2))
            return 0
        print("unlocked")
        return 0
    except (OSError, ValueError, json.JSONDecodeError, subprocess.CalledProcessError) as error:
        print(f"review lock operation failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
