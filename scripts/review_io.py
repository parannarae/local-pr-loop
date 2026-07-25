"""Durable and permission-aware local JSON file operations."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def load_object(path: Path) -> dict[str, Any]:
    """Load a JSON object, rejecting every other top-level shape."""
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise TypeError(f"{path} must contain a JSON object")
    return value


def require_secure_regular(path: Path, label: str) -> None:
    """Require a non-symlink regular file inaccessible to group and others."""
    if not path.is_file() or path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError(f"{label} must be a regular 0600 file")


def load_secure_object(path: Path, label: str) -> dict[str, Any]:
    """Load a JSON object from a permission-restricted regular file."""
    require_secure_regular(path, label)
    return load_object(path)


def atomic_bytes(path: Path, content: bytes, *, mode: int | None = None) -> None:
    """Replace a file atomically and durably within its parent directory."""
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f"{path.name}.tmp.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        if mode is not None:
            os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def atomic_json(
    path: Path, value: dict[str, Any], *, mode: int | None = None
) -> None:
    """Serialize and atomically replace a JSON object."""
    atomic_bytes(
        path,
        (json.dumps(value, indent=2) + "\n").encode(),
        mode=mode,
    )


def secure_json(path: Path, value: dict[str, Any]) -> None:
    """Atomically write a JSON object with mode 0600."""
    atomic_json(path, value, mode=0o600)
