"""Overlap detection between concurrently active reviews in one worktree."""

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
import review_state

CLI = ROOT / "scripts" / "review_cli.py"


def run(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*args],
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


# --- review_scope.overlapping_paths ---


class OverlappingPathsTest(unittest.TestCase):
    def test_a_directory_overlaps_a_file_inside_it(self) -> None:
        self.assertEqual(
            review_scope.overlapping_paths(["src"], ["src/app.py"]), ["src/app.py"]
        )

    def test_overlap_is_detected_in_either_direction(self) -> None:
        self.assertEqual(
            review_scope.overlapping_paths(["src/app.py"], ["src"]), ["src/app.py"]
        )

    def test_sibling_paths_do_not_overlap(self) -> None:
        self.assertEqual(review_scope.overlapping_paths(["src"], ["docs"]), [])

    def test_a_shared_prefix_that_is_not_a_directory_boundary_does_not_overlap(
        self,
    ) -> None:
        self.assertEqual(review_scope.overlapping_paths(["src"], ["srcextra"]), [])

    def test_additional_inputs_participate_in_the_comparison(self) -> None:
        declaration = review_scope.declaration([], ["notes.md"], ["src"])

        self.assertEqual(
            sorted(review_scope.declared_paths(declaration)), ["notes.md", "src"]
        )


# --- guard creation refuses an overlapping active review ---


class GuardOverlapTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        run("git", "init", "-q", cwd=self.repo)
        run("git", "config", "user.email", "review@example.com", cwd=self.repo)
        run("git", "config", "user.name", "Review Test", cwd=self.repo)
        (self.repo / ".gitignore").write_text(".local/\n")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "app.py").write_text("before\n")
        (self.repo / "other.txt").write_text("before\n")
        run("git", "add", "-A", cwd=self.repo)
        run("git", "commit", "-qm", "initial", cwd=self.repo)
        (self.repo / "src" / "app.py").write_text("after\n")
        (self.repo / "other.txt").write_text("after\n")
        self.first = self.init("first")
        self.second = self.init("second")
        self.reviews = self.repo / ".local" / "reviews"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def init(self, name: str) -> str:
        output = run(
            sys.executable, str(CLI), "init", str(self.repo), name, cwd=self.repo
        ).stdout
        return next(
            line.split(": ", 1)[1]
            for line in output.splitlines()
            if line.startswith("review_id: ")
        )

    def guard(self, review_id: str, *scope: str, check: bool = True):
        run(
            sys.executable,
            str(CLI),
            "lock",
            "acquire",
            str(self.repo),
            review_id,
            cwd=self.repo,
            check=False,
        )
        return run(
            sys.executable,
            str(CLI),
            "inspect",
            str(self.repo),
            review_id,
            *scope,
            cwd=self.repo,
            check=check,
        )

    def test_an_active_review_blocks_an_overlapping_guard(self) -> None:
        self.guard(self.first, "src")

        completed = self.guard(self.second, "src/app.py", check=False)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(self.first, completed.stderr)
        self.assertIn("src/app.py", completed.stderr)
        # The refusal must name a legal way forward, not just say no.
        self.assertIn("start-follow-up", completed.stderr)
        self.assertFalse((self.reviews / f"{self.second}.guard.json").exists())

    def test_a_disjoint_scope_is_allowed(self) -> None:
        self.guard(self.first, "src")

        self.guard(self.second, "other.txt")

        self.assertTrue((self.reviews / f"{self.second}.guard.json").is_file())

    def test_a_terminal_review_does_not_block(self) -> None:
        created_at = "2026-08-17T10:00:00+00:00"
        document = review_state.new_document(self.first, "first")
        document["created_at"] = created_at
        event = review_state.event_template("initial_review_timeout")
        event["reason"] = "the reviewer never appeared"
        event["started_at"] = created_at
        event["deadline"] = "2026-08-17T12:00:00+00:00"
        event["occurred_at"] = "2026-08-17T12:00:01+00:00"
        document = review_state.append_event(document, event)
        (self.reviews / f"{self.first}.json").write_text(
            json.dumps(document, indent=2) + "\n"
        )

        self.guard(self.second, "src")

        self.assertTrue((self.reviews / f"{self.second}.guard.json").is_file())

    def test_a_retired_review_does_not_block(self) -> None:
        run(
            sys.executable,
            str(CLI),
            "retire",
            str(self.repo),
            self.first,
            cwd=self.repo,
        )

        self.guard(self.second, "src")

        self.assertTrue((self.reviews / f"{self.second}.guard.json").is_file())


if __name__ == "__main__":
    unittest.main()
