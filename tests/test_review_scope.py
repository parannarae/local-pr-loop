"""Scope declaration tests, including guarded and unguarded snapshot equivalence."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import review_scope

CLI = ROOT / "scripts" / "review_cli.py"
SNAPSHOT = ROOT / "scripts" / "source_snapshot.py"


def run(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*args],
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


# --- review_scope.validate ---


class ReviewScopeDeclarationTest(unittest.TestCase):
    def test_builds_separate_fields_for_options_and_paths(self) -> None:
        declaration = review_scope.declaration([], ["notes.md"], ["src/app.py"])

        self.assertEqual(declaration["additional_input"], ["notes.md"])
        self.assertEqual(declaration["scope"], ["src/app.py"])

    def test_rejects_a_declaration_without_a_reviewed_path(self) -> None:
        with self.assertRaises(ValueError):
            review_scope.validate({"exclude": [], "additional_input": [], "scope": []})

    def test_rejects_a_path_declared_as_both_scope_and_additional_input(self) -> None:
        with self.assertRaises(ValueError) as caught:
            review_scope.validate(
                {
                    "exclude": [],
                    "additional_input": ["notes.md"],
                    "scope": ["notes.md"],
                }
            )

        self.assertIn("notes.md", str(caught.exception))

    def test_rejects_a_non_object_declaration(self) -> None:
        with self.assertRaises(ValueError):
            review_scope.validate(["src/app.py"])


# --- canonical path identity ---


class CanonicalPathTest(unittest.TestCase):
    """One repository path must have one identity, however a caller spells it.

    Comparing raw declaration strings lets an alias evade both the duplicate check and
    overlap detection, so two loops could guard the same file.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name).resolve()
        (self.repo / "src").mkdir()
        (self.repo / "shared").mkdir()
        (self.repo / "shared" / "data.txt").write_text("value\n")
        (self.repo / "linkdir").symlink_to("shared")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def identity(self, path: str) -> str:
        return review_scope.canonical_path(str(self.repo), path)

    def test_dot_dot_segments_collapse(self) -> None:
        self.assertEqual(self.identity("src/../shared"), "shared")

    def test_an_absolute_path_inside_the_repository_collapses(self) -> None:
        self.assertEqual(self.identity(str(self.repo / "shared")), "shared")

    def test_a_symlink_resolves_to_its_target(self) -> None:
        self.assertEqual(self.identity("linkdir/data.txt"), "shared/data.txt")

    def test_a_path_outside_the_repository_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.identity("../elsewhere.txt")

    def test_aliases_overlap_once_canonicalized(self) -> None:
        self.assertEqual(
            review_scope.overlapping_paths(
                ["src/../shared"], ["linkdir"], str(self.repo)
            ),
            ["shared"],
        )

    def test_a_missing_declared_path_is_rejected(self) -> None:
        """A path with no content would contribute an unbacked name to the fingerprint.

        The guard would then appear to cover a file it never read.
        """

        with self.assertRaises(ValueError) as caught:
            review_scope.require_distinct_declarations(
                str(self.repo),
                {"exclude": [], "additional_input": [], "scope": ["absent.py"]},
            )

        self.assertIn("does not exist", str(caught.exception))

    def test_a_missing_additional_input_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            review_scope.require_distinct_declarations(
                str(self.repo),
                {
                    "exclude": [],
                    "additional_input": ["absent.md"],
                    "scope": ["shared/data.txt"],
                },
            )

    def test_an_exclusion_need_not_exist(self) -> None:
        # Naming an absent path is a legitimate way to exclude it.
        declared = review_scope.require_distinct_declarations(
            str(self.repo),
            {
                "exclude": ["never-created"],
                "additional_input": [],
                "scope": ["shared/data.txt"],
            },
        )

        self.assertEqual(declared["exclude"], ["never-created"])

    def test_an_alias_cannot_hold_both_declaration_roles(self) -> None:
        with self.assertRaises(ValueError):
            review_scope.require_distinct_declarations(
                str(self.repo),
                {
                    "exclude": [],
                    "additional_input": ["shared/data.txt"],
                    "scope": ["linkdir/data.txt"],
                },
            )


# --- review_scope.snapshot_arguments ---


class ReviewScopeArgumentsTest(unittest.TestCase):
    def test_option_name_is_never_emitted_as_a_reviewed_path(self) -> None:
        arguments = review_scope.snapshot_arguments(
            "/repo",
            review_scope.declaration([], ["notes.md"], ["src/app.py"]),
        )

        separator = arguments.index("--")
        self.assertEqual(arguments[separator + 1 :], ["src/app.py"])
        self.assertEqual(
            arguments[:separator],
            ["--repo", "/repo", "--additional-input", "notes.md"],
        )

    def test_reviewed_paths_follow_the_separator_so_they_are_never_read_as_options(
        self,
    ) -> None:
        arguments = review_scope.snapshot_arguments(
            "/repo",
            review_scope.declaration(["build"], [], ["--odd-name.py"]),
        )

        self.assertEqual(arguments[-2:], ["--", "--odd-name.py"])


# --- guarded and unguarded snapshot equivalence ---


class GuardedSnapshotEquivalenceTest(unittest.TestCase):
    """A locked inspection must record what an unlocked inspection computes.

    When these disagree, every event records the guarded value while the ordinary
    inspection that follows records the other, so an approval is stale the moment it
    is published.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        run("git", "init", "-q", cwd=self.repo)
        run("git", "config", "user.email", "review@example.com", cwd=self.repo)
        run("git", "config", "user.name", "Review Test", cwd=self.repo)
        (self.repo / ".gitignore").write_text(".local/\n")
        (self.repo / "example.txt").write_text("before\n")
        run("git", "add", ".gitignore", "example.txt", cwd=self.repo)
        run("git", "commit", "-qm", "initial", cwd=self.repo)
        (self.repo / "example.txt").write_text("after\n")
        # An ignored file is exactly the case that must travel as an additional input.
        (self.repo / ".local").mkdir(exist_ok=True)
        (self.repo / ".local" / "context.md").write_text("design notes\n")

        output = run(
            sys.executable, str(CLI), "init", str(self.repo), "review", cwd=self.repo
        ).stdout
        self.review_id = next(
            line.split(": ", 1)[1]
            for line in output.splitlines()
            if line.startswith("review_id: ")
        )
        self.guard = (
            self.repo / ".local" / "reviews" / f"{self.review_id}.guard.json"
        )
        self.declaration = review_scope.declaration(
            [], [".local/context.md"], ["example.txt"]
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_locked_and_unlocked_snapshots_are_identical(self) -> None:
        unlocked = json.loads(
            run(
                sys.executable,
                str(SNAPSHOT),
                *review_scope.snapshot_arguments(str(self.repo), self.declaration),
                cwd=self.repo,
            ).stdout
        )

        run(
            sys.executable,
            str(CLI),
            "lock",
            "acquire",
            str(self.repo),
            self.review_id,
            cwd=self.repo,
        )
        run(
            sys.executable,
            str(CLI),
            "inspect",
            str(self.repo),
            self.review_id,
            "--additional-input",
            ".local/context.md",
            "example.txt",
            cwd=self.repo,
        )
        guarded = json.loads(self.guard.read_text())["source_snapshot"]

        self.assertEqual(guarded, unlocked)

    def test_guard_records_an_ignored_input_as_an_additional_input(self) -> None:
        run(
            sys.executable,
            str(CLI),
            "lock",
            "acquire",
            str(self.repo),
            self.review_id,
            cwd=self.repo,
        )
        run(
            sys.executable,
            str(CLI),
            "inspect",
            str(self.repo),
            self.review_id,
            "--additional-input",
            ".local/context.md",
            "example.txt",
            cwd=self.repo,
        )
        snapshot = json.loads(self.guard.read_text())["source_snapshot"]

        self.assertEqual(snapshot["scope"], ["example.txt"])
        self.assertEqual(
            [entry["path"] for entry in snapshot["additional_inputs"]],
            [".local/context.md"],
        )
        # The option token must never be recorded as a reviewed path.
        self.assertNotIn("--additional-input", snapshot["scope"])


if __name__ == "__main__":
    unittest.main()
