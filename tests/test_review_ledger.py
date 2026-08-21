"""Tests for accretion-ledger derivation, acknowledgment, and chaining decisions."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

MODULE = Path(__file__).parents[1] / "scripts" / "review_ledger.py"
sys.path.insert(0, str(MODULE.parent))
SPEC = importlib.util.spec_from_file_location("review_ledger", MODULE)
assert SPEC and SPEC.loader
review_ledger = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(review_ledger)


def run(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


def thread(paths: list[str] | None) -> dict[str, Any]:
    value: dict[str, Any] = {"id": "T1", "title": "finding"}
    if paths is not None:
        value["paths"] = paths
    return value


def document(
    history: list[dict[str, Any]] | None = None,
    review_kind: str = "correctness",
    structure_policy: str = "auto",
    comparison_base: str | None = None,
    review_id: str = "abcdefgh",
) -> dict[str, Any]:
    return {
        "review_id": review_id,
        "review_kind": review_kind,
        "structure_policy": structure_policy,
        "comparison_base": comparison_base,
        "history": history if history is not None else [],
    }


class ThreadCountTest(unittest.TestCase):
    def test_counts_threads_and_new_threads_per_path(self) -> None:
        history = [
            {"kind": "review", "threads": [thread(["a.py", "b.py"]), thread(["a.py"])]},
            {"kind": "reviewer_update", "new_threads": [thread(["a.py"])]},
        ]
        self.assertEqual(
            review_ledger.thread_counts(history), {"a.py": 3, "b.py": 1}
        )

    def test_thread_without_paths_counts_toward_nothing(self) -> None:
        history = [{"kind": "review", "threads": [thread(None), thread([])]}]
        self.assertEqual(review_ledger.thread_counts(history), {})

    def test_five_threads_flag_a_file_and_four_do_not(self) -> None:
        four = [{"kind": "review", "threads": [thread(["a.py"])] * 4}]
        five = [{"kind": "review", "threads": [thread(["a.py"])] * 5}]
        ledger_four = review_ledger.ledger(".", document(four), ["a.py"], [])
        ledger_five = review_ledger.ledger(".", document(five), ["a.py"], [])
        self.assertEqual(ledger_four["flagged"], [])
        self.assertEqual(ledger_five["flagged"], ["a.py"])
        self.assertEqual(ledger_five["files"]["a.py"]["flags"], ["threads"])

    def test_thread_path_outside_the_guarded_scope_never_flags(self) -> None:
        history = [{"kind": "review", "threads": [thread(["outside.py"])] * 9}]
        ledger = review_ledger.ledger(".", document(history), ["guarded.py"], [])
        self.assertEqual(ledger["flagged"], [])
        self.assertNotIn("outside.py", ledger["files"])

    def test_directory_scope_covers_nested_thread_paths(self) -> None:
        history = [{"kind": "review", "threads": [thread(["src/a.py"])] * 5}]
        ledger = review_ledger.ledger(".", document(history), ["src"], [])
        self.assertEqual(ledger["flagged"], ["src/a.py"])

    def test_excluded_thread_path_is_dropped(self) -> None:
        history = [{"kind": "review", "threads": [thread(["src/a.py"])] * 5}]
        ledger = review_ledger.ledger(
            ".", document(history), ["src"], ["src/a.py"]
        )
        self.assertEqual(ledger["flagged"], [])


class GrowthTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name)
        run("git", "init", "-q", cwd=self.repo)
        run("git", "config", "user.email", "review@example.com", cwd=self.repo)
        run("git", "config", "user.name", "Review Test", cwd=self.repo)
        (self.repo / "module.py").write_text("line\n" * 100)
        run("git", "add", "module.py", cwd=self.repo)
        run("git", "commit", "-qm", "base", cwd=self.repo)
        self.base = run("git", "rev-parse", "HEAD", cwd=self.repo).stdout.strip()

    def grow(self, added_lines: int) -> None:
        (self.repo / "module.py").write_text("line\n" * (100 + added_lines))

    def ledger(self, history: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        return review_ledger.ledger(
            str(self.repo),
            document(history, comparison_base=self.base),
            ["module.py"],
            [],
        )

    def test_growth_above_threshold_flags_the_file(self) -> None:
        self.grow(21)
        ledger = self.ledger()
        self.assertEqual(ledger["flagged"], ["module.py"])
        entry = ledger["files"]["module.py"]
        self.assertEqual(entry["flags"], ["growth"])
        self.assertEqual(entry["base_lines"], 100)
        self.assertEqual(entry["growth"], 0.21)

    def test_growth_at_exactly_the_threshold_does_not_flag(self) -> None:
        self.grow(20)
        ledger = self.ledger()
        self.assertEqual(ledger["flagged"], [])
        self.assertEqual(ledger["files"]["module.py"]["growth"], 0.2)

    def test_file_created_after_the_base_is_not_auto_flagged(self) -> None:
        (self.repo / "new.py").write_text("line\n" * 500)
        run("git", "add", "new.py", cwd=self.repo)
        ledger = review_ledger.ledger(
            str(self.repo),
            document(comparison_base=self.base),
            ["module.py", "new.py"],
            [],
        )
        self.assertEqual(ledger["flagged"], [])
        self.assertIsNone(ledger["files"]["new.py"]["base_lines"])
        self.assertIsNone(ledger["files"]["new.py"]["growth"])

    def test_thread_and_growth_flags_combine_on_one_file(self) -> None:
        self.grow(30)
        history = [{"kind": "review", "threads": [thread(["module.py"])] * 5}]
        ledger = self.ledger(history)
        self.assertEqual(ledger["files"]["module.py"]["flags"], ["threads", "growth"])

    def test_missing_base_skips_growth_and_keeps_thread_signal(self) -> None:
        history = [{"kind": "review", "threads": [thread(["module.py"])] * 5}]
        ledger = review_ledger.ledger(
            str(self.repo), document(history), ["module.py"], []
        )
        self.assertEqual(ledger["flagged"], ["module.py"])
        self.assertFalse(ledger["comparison_base_reachable"])
        self.assertNotIn("base_lines", ledger["files"]["module.py"])

    def test_unreachable_base_degrades_to_the_thread_signal(self) -> None:
        self.grow(50)
        ledger = review_ledger.ledger(
            str(self.repo),
            document(comparison_base="f" * 40),
            ["module.py"],
            [],
        )
        self.assertEqual(ledger["flagged"], [])
        self.assertFalse(ledger["comparison_base_reachable"])

    def test_non_utf8_base_content_still_measures_growth(self) -> None:
        (self.repo / "legacy.txt").write_bytes(b"caf\xe9\n" * 100)
        run("git", "add", "legacy.txt", cwd=self.repo)
        run("git", "commit", "-qm", "latin-1 content", cwd=self.repo)
        base = run("git", "rev-parse", "HEAD", cwd=self.repo).stdout.strip()
        (self.repo / "legacy.txt").write_bytes(b"caf\xe9\n" * 130)
        ledger = review_ledger.ledger(
            str(self.repo),
            document(comparison_base=base),
            ["legacy.txt"],
            [],
        )
        self.assertTrue(ledger["comparison_base_reachable"])
        self.assertEqual(ledger["flagged"], ["legacy.txt"])
        self.assertEqual(ledger["files"]["legacy.txt"]["base_lines"], 100)

    def test_non_ascii_path_is_growth_flagged_under_its_scoped_name(self) -> None:
        name = "모듈.py"
        (self.repo / name).write_text("line\n" * 100)
        run("git", "add", name, cwd=self.repo)
        run("git", "commit", "-qm", "non-ascii path", cwd=self.repo)
        base = run("git", "rev-parse", "HEAD", cwd=self.repo).stdout.strip()
        (self.repo / name).write_text("line\n" * 130)
        ledger = review_ledger.ledger(
            str(self.repo),
            document(comparison_base=base),
            [name],
            [],
        )
        self.assertEqual(ledger["flagged"], [name])
        self.assertEqual(ledger["files"][name]["base_lines"], 100)

    def test_excluded_path_does_not_contribute_growth(self) -> None:
        self.grow(50)
        ledger = review_ledger.ledger(
            str(self.repo),
            document(comparison_base=self.base),
            ["module.py"],
            ["module.py"],
        )
        self.assertEqual(ledger["flagged"], [])


class FlaggedPathsTest(unittest.TestCase):
    def test_structure_round_never_reports_flagged_paths(self) -> None:
        history = [{"kind": "review", "threads": [thread(["a.py"])] * 9}]
        flagged = review_ledger.flagged_paths(
            ".", document(history, review_kind="structure"), ["a.py"], []
        )
        self.assertEqual(flagged, [])

    def test_policy_off_never_reports_flagged_paths(self) -> None:
        history = [{"kind": "review", "threads": [thread(["a.py"])] * 9}]
        flagged = review_ledger.flagged_paths(
            ".", document(history, structure_policy="off"), ["a.py"], []
        )
        self.assertEqual(flagged, [])


# --- acknowledgment_error ---


def debt(paths: list[str], disposition: str = "structure_deferred") -> dict[str, Any]:
    return {"disposition": disposition, "flagged_paths": paths, "message": "why"}


class AcknowledgmentTest(unittest.TestCase):
    def test_non_final_review_events_are_never_checked(self) -> None:
        error = review_ledger.acknowledgment_error(
            document(), {"kind": "owner_reply"}, ["a.py"]
        )
        self.assertIsNone(error)

    def test_flagged_files_without_structure_debt_are_rejected(self) -> None:
        error = review_ledger.acknowledgment_error(
            document(), {"kind": "final_review"}, ["a.py"]
        )
        self.assertIn("a.py", error or "")

    def test_matching_acknowledgment_passes(self) -> None:
        event = {"kind": "final_review", "structure_debt": debt(["a.py", "b.py"])}
        error = review_ledger.acknowledgment_error(document(), event, ["b.py", "a.py"])
        self.assertIsNone(error)

    def test_stale_flagged_set_is_rejected(self) -> None:
        event = {"kind": "final_review", "structure_debt": debt(["a.py"])}
        error = review_ledger.acknowledgment_error(document(), event, ["b.py"])
        self.assertIn("does not match", error or "")

    def test_unflagged_acknowledgment_is_rejected(self) -> None:
        event = {"kind": "final_review", "structure_debt": debt(["a.py"])}
        error = review_ledger.acknowledgment_error(document(), event, [])
        self.assertIn("no file is accretion-flagged", error or "")

    def test_clean_final_review_passes(self) -> None:
        error = review_ledger.acknowledgment_error(
            document(), {"kind": "final_review"}, []
        )
        self.assertIsNone(error)

    def test_structure_round_rejects_structure_debt(self) -> None:
        event = {"kind": "final_review", "structure_debt": debt(["a.py"])}
        error = review_ledger.acknowledgment_error(
            document(review_kind="structure"), event, []
        )
        self.assertIn("structure round", error or "")


# --- structure_follow_up_due ---


class FollowUpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.reviews = Path(self.temporary.name)

    def deferred_document(self, **overrides: Any) -> dict[str, Any]:
        history = [{"kind": "final_review", "structure_debt": debt(["a.py"])}]
        return document(history, **overrides)

    def test_deferred_terminal_under_auto_is_due(self) -> None:
        self.assertTrue(
            review_ledger.structure_follow_up_due(
                self.deferred_document(), self.reviews
            )
        )

    def test_policy_defer_is_not_due(self) -> None:
        self.assertFalse(
            review_ledger.structure_follow_up_due(
                self.deferred_document(structure_policy="defer"), self.reviews
            )
        )

    def test_structure_round_is_never_due(self) -> None:
        history = [{"kind": "final_review"}]
        self.assertFalse(
            review_ledger.structure_follow_up_due(
                document(history, review_kind="structure"), self.reviews
            )
        )

    def test_reviewed_disposition_is_not_due(self) -> None:
        history = [
            {
                "kind": "final_review",
                "structure_debt": debt(["a.py"], "structure_reviewed"),
            }
        ]
        self.assertFalse(
            review_ledger.structure_follow_up_due(document(history), self.reviews)
        )

    def test_existing_structure_successor_consumes_the_flag_set(self) -> None:
        successor = {
            "review_id": "zzzzzzzz",
            "prior_review_id": "abcdefgh",
            "review_kind": "structure",
        }
        (self.reviews / "zzzzzzzz.json").write_text(json.dumps(successor))
        self.assertFalse(
            review_ledger.structure_follow_up_due(
                self.deferred_document(), self.reviews
            )
        )

    def test_non_canonical_artifacts_are_ignored_in_the_successor_scan(self) -> None:
        successor = {
            "review_id": "zzzzzzzz",
            "prior_review_id": "abcdefgh",
            "review_kind": "structure",
        }
        (self.reviews / "zzzzzzzz.guard.json").write_text(json.dumps(successor))
        self.assertTrue(
            review_ledger.structure_follow_up_due(
                self.deferred_document(), self.reviews
            )
        )


if __name__ == "__main__":
    unittest.main()
