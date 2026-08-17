"""Scope declaration shared by inspection, guard creation, and publication.

One declaration builds every source-snapshot argument vector in this package. A locked
inspection, an unlocked inspection, and a publication that replays a stored guard therefore
cannot disagree about what was declared, because none of them re-parses an argument vector
built by another process.

Transporting the declaration as structured data rather than as an argument tail is what makes
that guarantee hold: an option name such as ``--additional-input`` stays in its own field and
can never arrive as a positional scope path.
"""

from __future__ import annotations

from pathlib import Path

DECLARATION_FIELDS = ("exclude", "additional_input", "scope")


def declaration(
    exclude: list[str] | tuple[str, ...],
    additional_input: list[str] | tuple[str, ...],
    scope: list[str] | tuple[str, ...],
) -> dict[str, list[str]]:
    """Build a scope declaration from separately supplied option and path values."""

    return {
        "exclude": [str(value) for value in exclude],
        "additional_input": [str(value) for value in additional_input],
        "scope": [str(value) for value in scope],
    }


def validate(value: object) -> dict[str, list[str]]:
    """Return a declaration read from transported or stored data.

    Raises:
        ValueError: The value is not a declaration this package can act on.
    """

    if not isinstance(value, dict):
        raise ValueError("scope declaration must be a JSON object")

    result: dict[str, list[str]] = {}
    for field in DECLARATION_FIELDS:
        items = value.get(field, [])
        if not isinstance(items, list) or not all(
            isinstance(item, str) for item in items
        ):
            raise ValueError(
                f"scope declaration field {field!r} must be a list of strings"
            )
        result[field] = list(items)

    if not result["scope"]:
        raise ValueError("scope declaration requires at least one reviewed path")

    duplicated = sorted(set(result["scope"]) & set(result["additional_input"]))
    if duplicated:
        raise ValueError(
            "scope declaration lists the same path as both a reviewed path and an "
            f"additional input: {', '.join(duplicated)}"
        )
    return result


def declared_paths(value: object) -> list[str]:
    """Return every repository-relative path a declaration guards."""

    declared = validate(value)
    return [*declared["scope"], *declared["additional_input"]]


def normalize_path(path: str) -> str:
    """Return a lexically comparable repository-relative path.

    This only trims separators. It cannot see through ``..`` segments or symlinks, so it
    is never sufficient on its own for a safety decision; use `canonical_path` wherever a
    repository root is available.
    """

    return path.strip("/")


def canonical_path(repository_root: str, path: str) -> str:
    """Return one repository-relative identity for a declared path.

    Two spellings of the same file, whether through ``.``, ``..``, an absolute path, or a
    symlink, must collapse to the same identity. Duplicate and overlap decisions compare
    these identities, because comparing the strings a caller happened to type lets an
    alias slip past both checks.

    Raises:
        ValueError: The path resolves outside the repository worktree.
    """

    root = Path(repository_root).resolve()
    candidate = Path(path)
    resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if resolved == root:
        return "."
    try:
        return str(resolved.relative_to(root))
    except ValueError as error:
        raise ValueError(
            f"declared path resolves outside the repository: {path}"
        ) from error


def canonical_paths(repository_root: str, paths: list[str]) -> list[str]:
    """Return the repository-relative identities of several declared paths."""

    return [canonical_path(repository_root, path) for path in paths]


def require_distinct_declarations(
    repository_root: str, value: object
) -> dict[str, list[str]]:
    """Validate a declaration and reject aliases of one path in two roles.

    Raises:
        ValueError: A path is declared as both reviewed scope and an additional input, or
            resolves outside the repository.
    """

    declared = validate(value)
    scope_identities = {
        canonical_path(repository_root, path): path for path in declared["scope"]
    }
    duplicated = []
    for path in declared["additional_input"]:
        identity = canonical_path(repository_root, path)
        if identity in scope_identities:
            duplicated.append(f"{scope_identities[identity]} and {path}")
    if duplicated:
        raise ValueError(
            "scope declaration lists the same repository path as both a reviewed path "
            "and an additional input: " + "; ".join(sorted(duplicated))
        )
    require_existing_paths(repository_root, declared)
    return declared


def require_existing_paths(repository_root: str, declared: dict[str, list[str]]) -> None:
    """Reject a declared path that has no content to hash.

    A path that does not exist contributes its name to the fingerprint with nothing
    behind it, so the guard would appear to cover a file it never read. Exclusions are
    exempt: naming a path that is absent is a legitimate way to exclude it.

    Raises:
        ValueError: A reviewed path or additional input is missing from the worktree.
    """

    root = Path(repository_root).resolve()
    missing = [
        path
        for field in ("scope", "additional_input")
        for path in declared[field]
        if not (root / canonical_path(repository_root, path)).exists()
    ]
    if missing:
        raise ValueError(
            "declared path does not exist in the worktree, so it would contribute a "
            "name with no content to the fingerprint: " + ", ".join(sorted(missing))
        )


def covers(container: str, candidate: str) -> bool:
    """Report whether a declared directory contains a candidate path."""

    return candidate == container or candidate.startswith(f"{container}/")


def overlapping_paths(
    first: list[str], second: list[str], repository_root: str | None = None
) -> list[str]:
    """Return the paths two scopes share, treating a directory as its whole subtree.

    Two loops that guard ``src`` and ``src/app.py`` are reviewing the same file, so a
    plain set intersection would miss the conflict that matters.

    Supply ``repository_root`` for any safety decision. Without it the comparison is
    lexical, so ``src/../shared`` and ``shared`` look unrelated even though they name one
    file. A path that resolves outside the repository is skipped rather than compared,
    because it can never be part of a guarded scope.
    """

    def identities(paths: list[str]) -> list[str]:
        if repository_root is None:
            return [normalize_path(path) for path in paths]
        resolved = []
        for path in paths:
            try:
                resolved.append(canonical_path(repository_root, path))
            except ValueError:
                continue
        return resolved

    shared: set[str] = set()
    for left in identities(first):
        for right in identities(second):
            if covers(left, right):
                shared.add(right)
            elif covers(right, left):
                shared.add(left)
    return sorted(shared)


def snapshot_arguments(repository_root: str, value: object) -> list[str]:
    """Build the source-snapshot argument vector for a declaration.

    Options are emitted from their own fields and the reviewed paths follow a ``--``
    separator, so a path is never read as an option and an option is never recorded as a path.
    """

    declared = validate(value)
    arguments = ["--repo", str(repository_root)]
    for exclusion in declared["exclude"]:
        arguments.extend(["--exclude", exclusion])
    for additional_input in declared["additional_input"]:
        arguments.extend(["--additional-input", additional_input])
    arguments.append("--")
    arguments.extend(declared["scope"])
    return arguments
