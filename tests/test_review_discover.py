"""Discovery tests for resumable-loop selection from canonical state."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import review_discover
import review_lock
import review_state

SCRIPT = ROOT / "scripts" / "review_cli.py"


def run(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*args],
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


class LoadCanonicalCandidateTest(unittest.TestCase):
    """The loading contract: a document, or CandidateError naming every reason."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_returns_the_document_for_a_valid_canonical_file(self) -> None:
        document = review_state.new_document("abcdefgh", "demo")
        path = self.directory / "abcdefgh.json"
        path.write_text(json.dumps(document))

        loaded = review_discover.load_canonical_candidate(path, "abcdefgh")

        self.assertEqual(loaded["review_id"], "abcdefgh")

    def test_raises_candidate_error_with_structured_reasons_for_unreadable_json(
        self,
    ) -> None:
        path = self.directory / "abcdefgh.json"
        path.write_text("{not json")

        with self.assertRaises(review_discover.CandidateError) as caught:
            review_discover.load_canonical_candidate(path, "abcdefgh")

        self.assertEqual(len(caught.exception.errors), 1)
        self.assertIn("unreadable canonical JSON", caught.exception.errors[0])

    def test_a_mismatched_review_id_is_appended_to_validation_errors(self) -> None:
        document = review_state.new_document("abcdefgh", "demo")
        path = self.directory / "other123.json"
        path.write_text(json.dumps(document))

        with self.assertRaises(review_discover.CandidateError) as caught:
            review_discover.load_canonical_candidate(path, "other123")

        self.assertIn(
            "review_id does not match the artifact file name",
            caught.exception.errors,
        )


class ReviewDiscoverTest(unittest.TestCase):
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

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def reviews_dir(self) -> Path:
        return self.repo / ".local" / "reviews"

    def cli(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return run(sys.executable, str(SCRIPT), *args, cwd=self.repo, check=check)

    def init_loop(self, name: str) -> str:
        output = self.cli("init", str(self.repo), name).stdout
        return next(
            line.split(": ", 1)[1]
            for line in output.splitlines()
            if line.startswith("review_id: ")
        )

    def discover(self) -> dict[str, Any]:
        return json.loads(self.cli("discover", str(self.repo), "--json").stdout)

    def write_terminal_loop(self, name: str) -> str:
        # Build the terminal through the real append path so the projected state
        # is genuine; a hand-edited state is correctly rejected as invalid.
        review_id = self.init_loop(name)
        created_at = "2026-08-17T10:00:00+00:00"
        document = review_state.new_document(review_id, name)
        document["created_at"] = created_at
        event = review_state.event_template("initial_review_timeout")
        event["reason"] = "the reviewer never appeared"
        event["started_at"] = created_at
        event["deadline"] = "2026-08-17T12:00:00+00:00"
        event["occurred_at"] = "2026-08-17T12:00:01+00:00"
        document = review_state.append_event(document, event)
        (self.reviews_dir() / f"{review_id}.json").write_text(
            json.dumps(document, indent=2) + "\n"
        )
        return review_id

    def publish_initial_review(
        self, review_id: str, additional_input: str | None = None
    ) -> None:
        declaration = ["example.txt"]
        if additional_input:
            declaration = ["--additional-input", additional_input, "example.txt"]
        source = json.loads(
            self.cli("snapshot", str(self.repo), *declaration).stdout
        )
        self.cli("lock", "acquire", str(self.repo), review_id)
        self.cli("inspect", str(self.repo), review_id, "--json", *declaration)
        self.cli("template", str(self.repo), review_id, "review")
        event_path = self.reviews_dir() / f"{review_id}.event.json"
        event = json.loads(event_path.read_text())
        event["source_snapshot"] = source
        event["threads"][0].update(
            {
                "title": "Update example",
                "risk": "Old result remains.",
                "required_behavior": "Use the new result.",
            }
        )
        event["threads"][0]["evidence"].update(
            {
                "provenance": "example.txt",
                "sanitized_result": "The file contains the old value.",
            }
        )
        event["validation"]["performed"] = [
            {"check": "source inspection", "result": "passed"}
        ]
        event_path.write_text(json.dumps(event, indent=2) + "\n")
        self.cli("publish", str(self.repo), review_id)

    # --- discover: selection ---

    def test_exactly_one_non_terminal_loop_is_selected(self) -> None:
        review_id = self.init_loop("only-loop")

        result = self.discover()

        self.assertEqual(result["status"], "selected")
        self.assertEqual(result["selected_review_id"], review_id)
        self.assertEqual(len(result["candidates"]), 1)
        candidate = result["candidates"][0]
        self.assertEqual(candidate["name"], "only-loop")
        self.assertEqual(candidate["review_kind"], "correctness")
        self.assertEqual(candidate["phase"], "awaiting_initial_review")
        self.assertEqual(candidate["primary_actor"], "reviewer")
        self.assertEqual(candidate["source_drift"], "no_snapshot")
        self.assertIs(candidate["lock"]["held"], False)

    def test_no_loops_returns_none_and_creates_nothing(self) -> None:
        result = self.discover()

        self.assertEqual(result["status"], "none")
        self.assertIsNone(result["selected_review_id"])
        self.assertEqual(result["candidates"], [])
        # An explicit none result never initializes a loop as a side effect.
        self.assertFalse(self.reviews_dir().exists())

    def test_multiple_non_terminal_loops_are_ambiguous_with_summaries(self) -> None:
        first = self.init_loop("first-loop")
        second = self.init_loop("second-loop")
        self.cli("lock", "acquire", str(self.repo), first)

        result = self.discover()

        self.assertEqual(result["status"], "ambiguous")
        self.assertIsNone(result["selected_review_id"])
        by_id = {item["review_id"]: item for item in result["candidates"]}
        self.assertEqual(set(by_id), {first, second})
        self.assertIs(by_id[first]["lock"]["held"], True)
        self.assertIs(by_id[second]["lock"]["held"], False)
        for candidate in by_id.values():
            for field in ("name", "review_kind", "phase", "primary_actor",
                          "created_at", "source_drift"):
                self.assertIn(field, candidate)

    # --- discover: exclusions ---

    def test_terminal_loop_is_excluded_and_listed(self) -> None:
        terminal_id = self.write_terminal_loop("finished-loop")
        active_id = self.init_loop("active-loop")

        result = self.discover()

        self.assertEqual(result["status"], "selected")
        self.assertEqual(result["selected_review_id"], active_id)
        self.assertEqual(
            [(item["review_id"], item["outcome"]) for item in result["terminal"]],
            [(terminal_id, "initial_review_timeout")],
        )

    def test_only_terminal_loops_yield_none(self) -> None:
        self.write_terminal_loop("finished-loop")

        result = self.discover()

        self.assertEqual(result["status"], "none")
        self.assertEqual(result["candidates"], [])
        self.assertEqual(len(result["terminal"]), 1)

    def test_auxiliary_artifacts_are_not_candidates(self) -> None:
        review_id = self.init_loop("real-loop")
        # A full auxiliary set for an identifier with no canonical file must be
        # invisible to discovery, not a candidate and not an invalid artifact.
        for suffix in (
            "event.json",
            "latest.md",
            "lease.json",
            "guard.json",
            "publish.json",
            "retired.json",
        ):
            (self.reviews_dir() / f"bcdefghj.{suffix}").write_text("{}\n")

        result = self.discover()

        self.assertEqual(result["status"], "selected")
        self.assertEqual(result["selected_review_id"], review_id)
        self.assertEqual(result["invalid"], [])

    def test_malformed_and_unsupported_canonicals_are_reported_not_selected(
        self,
    ) -> None:
        review_id = self.init_loop("valid-loop")
        (self.reviews_dir() / "cdefghjk.json").write_text("{not json\n")
        unsupported = review_state.new_document("defghjkm", "old-loop")
        unsupported["format_revision"] = "1999-01-01"
        (self.reviews_dir() / "defghjkm.json").write_text(
            json.dumps(unsupported, indent=2) + "\n"
        )

        result = self.discover()

        self.assertEqual(result["status"], "selected")
        self.assertEqual(result["selected_review_id"], review_id)
        invalid_ids = {item["review_id"] for item in result["invalid"]}
        self.assertEqual(invalid_ids, {"cdefghjk", "defghjkm"})
        for item in result["invalid"]:
            self.assertTrue(item["errors"])

    def test_symlink_and_directory_canonicals_are_invalid_not_selected(self) -> None:
        review_id = self.init_loop("valid-loop")
        (self.reviews_dir() / "ghjkmnpq.json").symlink_to(
            self.reviews_dir() / f"{review_id}.json"
        )
        (self.reviews_dir() / "hjkmnpqr.json").mkdir()

        result = self.discover()

        self.assertEqual(result["status"], "selected")
        self.assertEqual(result["selected_review_id"], review_id)
        self.assertEqual(
            {item["review_id"] for item in result["invalid"]},
            {"ghjkmnpq", "hjkmnpqr"},
        )

    def test_canonical_named_for_a_different_review_id_is_invalid(self) -> None:
        review_id = self.init_loop("valid-loop")
        canonical = (self.reviews_dir() / f"{review_id}.json").read_text()
        (self.reviews_dir() / "fghjkmnp.json").write_text(canonical)

        result = self.discover()

        self.assertEqual(result["status"], "selected")
        self.assertEqual(result["selected_review_id"], review_id)
        self.assertEqual(
            [item["review_id"] for item in result["invalid"]], ["fghjkmnp"]
        )

    # --- discover: candidate summaries ---

    def test_candidate_reports_scope_and_source_drift(self) -> None:
        review_id = self.init_loop("published-loop")
        self.publish_initial_review(review_id)

        clean = self.discover()["candidates"][0]
        self.assertEqual(clean["phase"], "owner_response")
        self.assertEqual(clean["scope"]["scope"], ["example.txt"])
        self.assertEqual(clean["open_threads"], 1)
        self.assertEqual(clean["latest_event"]["kind"], "review")
        self.assertEqual(clean["source_drift"], "clean")

        (self.repo / "example.txt").write_text("after\n")
        drifted = self.discover()["candidates"][0]
        self.assertEqual(drifted["source_drift"], "drifted")

    def test_source_drift_unavailable_when_scope_cannot_be_snapshotted(self) -> None:
        (self.repo / ".gitignore").write_text(".local/\ngenerated.txt\n")
        (self.repo / "generated.txt").write_text("artifact\n")
        review_id = self.init_loop("published-loop")
        self.publish_initial_review(review_id, additional_input="generated.txt")

        # A recorded additional input that no longer exists cannot be
        # re-snapshotted, which is distinct from ordinary content drift.
        (self.repo / "generated.txt").unlink()

        candidate = self.discover()["candidates"][0]
        self.assertEqual(candidate["source_drift"], "unavailable")
        self.assertEqual(candidate["scope"]["additional_inputs"], ["generated.txt"])

    def test_unavailable_lock_status_reports_held_null(self) -> None:
        review_id = self.init_loop("only-loop")
        canonical = self.reviews_dir() / f"{review_id}.json"
        # A lock directory whose holder record is unreadable makes lock status
        # fail, which discovery must report as unknown, not as held or free.
        lock_dir = review_lock.lock_directory(self.repo, str(canonical))
        lock_dir.mkdir(parents=True)
        (lock_dir / "owner.json").write_text("{}\n")

        candidate = self.discover()["candidates"][0]
        self.assertIsNone(candidate["lock"]["held"])

    # --- explicit-ID workflow ---

    def test_explicit_id_workflow_is_unchanged_amid_multiple_loops(self) -> None:
        first = self.init_loop("first-loop")
        self.init_loop("second-loop")

        # An explicitly supplied ID keeps working without discovery, even while
        # discovery itself reports ambiguity.
        self.assertEqual(self.discover()["status"], "ambiguous")
        validated = self.cli("validate", str(self.repo), first)
        self.assertEqual(validated.returncode, 0)
        inspected = json.loads(
            self.cli(
                "inspect", str(self.repo), first, "--json", "example.txt"
            ).stdout
        )
        self.assertIn("recommended_next_command", inspected)


if __name__ == "__main__":
    unittest.main()
