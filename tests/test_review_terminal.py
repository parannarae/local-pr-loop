"""Terminal-state reporting tests separating approvals from timeouts."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import review_cli
import review_state

CLI = ROOT / "scripts" / "review_cli.py"
PUBLISH = ROOT / "scripts" / "review_publish.py"
STATE = ROOT / "scripts" / "review_state.py"
LOCK = ROOT / "scripts" / "review_lock.py"


def run(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*args],
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


class TerminalReportingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.review = self.root / "review.json"
        self.event = self.root / "review.event.json"
        self.report = self.root / "review.latest.md"
        self.journal = self.root / "review.publish.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def dashboard(self, outcome: str) -> dict:
        document = review_state.new_document("abcdefgh", "review")
        document["state"]["workflow"]["phase"] = "terminal"
        document["state"]["workflow"]["primary_actor"] = None
        document["state"]["terminal"] = {
            "outcome": outcome,
            "occurred_at": "2026-08-17T12:00:00+00:00",
        }
        document["state"]["source_fingerprint"] = "a" * 64
        self.review.write_text(json.dumps(document, indent=2) + "\n")
        self.report.write_text(review_state.render_report(document))
        completed = run(
            sys.executable,
            str(PUBLISH),
            "operation",
            "--review",
            str(self.review),
            "--event",
            str(self.event),
            "--report",
            str(self.report),
            "--journal",
            str(self.journal),
            "--state-script",
            str(STATE),
            "--lock-json",
            "unlocked",
            "--repo",
            str(self.root),
            "--review-id",
            "abcdefgh",
            # A different fingerprint means the source moved after the terminal event.
            "--current-source-fingerprint",
            "b" * 64,
            "--command-prefix",
            "cli",
            "--json",
            cwd=self.root,
        )
        return json.loads(completed.stdout)

    def test_a_drifted_approval_is_stale_and_routes_to_a_successor(self) -> None:
        source = self.dashboard("lgtm")["source"]

        self.assertTrue(source["drift"])
        self.assertTrue(source["approval_stale"])
        self.assertFalse(source["source_moved_since_terminal"])

    def test_a_drifted_timeout_is_not_reported_as_a_stale_approval(self) -> None:
        """A timeout records that nothing was verified, so no approval can be stale."""

        dashboard = self.dashboard("reviewer_timeout")
        source = dashboard["source"]

        self.assertTrue(source["drift"])
        self.assertFalse(source["approval_stale"])
        self.assertEqual(source["terminal_outcome"], "reviewer_timeout")
        self.assertTrue(source["source_moved_since_terminal"])
        # Steering to a successor here would present a timeout as an aged-out approval.
        self.assertEqual(dashboard["recommended_next_command"], "none")


def _field(output: str, key: str) -> str:
    """Return one `key: value` line's value from CLI output."""
    return next(
        line.split(": ", 1)[1]
        for line in output.splitlines()
        if line.startswith(f"{key}: ")
    )


class StartFollowUpTest(unittest.TestCase):
    """`start-follow-up` is the successor path for every terminal outcome."""

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
        output = run(
            sys.executable, str(CLI), "init", str(self.repo), "review", cwd=self.repo
        ).stdout
        self.review_id = next(
            line.split(": ", 1)[1]
            for line in output.splitlines()
            if line.startswith("review_id: ")
        )
        self.canonical = (
            self.repo / ".local" / "reviews" / f"{self.review_id}.json"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_a_timeout_terminal_can_start_a_linked_successor(self) -> None:
        # Build the terminal through the real append path so the projected state is
        # genuine; a hand-edited state is correctly rejected as invalid.
        created_at = "2026-08-17T10:00:00+00:00"
        document = review_state.new_document(self.review_id, "review")
        document["created_at"] = created_at
        event = review_state.event_template("initial_review_timeout")
        event["reason"] = "the reviewer never appeared"
        event["started_at"] = created_at
        event["deadline"] = "2026-08-17T12:00:00+00:00"
        event["occurred_at"] = "2026-08-17T12:00:01+00:00"
        document = review_state.append_event(document, event)
        self.canonical.write_text(json.dumps(document, indent=2) + "\n")
        self.assertEqual(document["state"]["workflow"]["phase"], "terminal")

        output = run(
            sys.executable,
            str(CLI),
            "start-follow-up",
            str(self.repo),
            self.review_id,
            "successor",
            cwd=self.repo,
        ).stdout
        successor_id = next(
            line.split(": ", 1)[1]
            for line in output.splitlines()
            if line.startswith("review_id: ")
        )
        successor = json.loads(
            (self.repo / ".local" / "reviews" / f"{successor_id}.json").read_text()
        )

        # Provenance must be visible from the successor, not only the predecessor's prose.
        self.assertEqual(successor["prior_review_id"], self.review_id)

    def _terminate(self) -> None:
        """Drive the review to a genuine terminal state through the append path."""
        created_at = "2026-08-17T10:00:00+00:00"
        document = review_state.new_document(self.review_id, "review")
        document["created_at"] = created_at
        event = review_state.event_template("initial_review_timeout")
        event["reason"] = "the reviewer never appeared"
        event["started_at"] = created_at
        event["deadline"] = "2026-08-17T12:00:00+00:00"
        event["occurred_at"] = "2026-08-17T12:00:01+00:00"
        document = review_state.append_event(document, event)
        self.canonical.write_text(json.dumps(document, indent=2) + "\n")

    def _live_successors(self) -> list[str]:
        """Read the surviving successors straight off disk, not through the CLI."""
        live = []
        for path in sorted((self.repo / ".local" / "reviews").glob("*.json")):
            if path.name.count(".") != 1:
                continue
            document = json.loads(path.read_text())
            if document.get("prior_review_id") != self.review_id:
                continue
            if document["state"]["workflow"]["phase"] == "terminal":
                continue
            live.append(document["review_id"])
        return sorted(live)

    def _create_successor_directly(
        self,
        name: str,
        *,
        # The review ID alphabet omits look-alike characters, so a hand-made ID
        # outside it is silently ignored by discovery rather than reported.
        review_id: str = "zzzzzzzz",
        created_at: str = "2099-01-01T00:00:00+00:00",
    ) -> str:
        """Write a second chained successor, bypassing the convergence in the CLI.

        This is the state a process leaves behind when it dies after creating its
        loop, which no lock held across creation could have prevented.
        """
        document = review_state.new_document(
            review_id,
            name,
            prior_review_id=self.review_id,
            review_kind="structure",
        )
        # The tie-break is decided by the test rather than by how fast the
        # machine ran: the default loses to any loop the CLI just made, and an
        # earlier stamp is passed when the fabricated loop has to win.
        document["created_at"] = created_at
        path = self.repo / ".local" / "reviews" / f"{review_id}.json"
        path.write_text(json.dumps(document, indent=2) + "\n")
        return review_id

    def _publish_a_thread(self, review_id: str) -> None:
        """Give a successor review history, which makes it ineligible for retirement."""
        path = self.repo / ".local" / "reviews" / f"{review_id}.json"
        document = json.loads(path.read_text())
        event = review_state.event_template("review")
        event["source_snapshot"].update(
            {
                "revision": "0" * 40,
                "scope": ["src"],
                "fingerprint": "b" * 64,
                "staged_sha256": "c" * 64,
                "unstaged_sha256": "d" * 64,
            }
        )
        thread = event["threads"][0]
        thread.update(
            {
                "title": "a finding that must not be discarded",
                "risk": "the behavior is wrong",
                "required_behavior": "it should be right",
                "paths": ["src"],
            }
        )
        thread["evidence"].update(
            {"provenance": "src", "sanitized_result": "observed directly"}
        )
        document = review_state.append_event(document, event)
        path.write_text(json.dumps(document, indent=2) + "\n")

    def _follow_up(self, name: str, *, kind: str = "structure", check: bool = True):
        return run(
            sys.executable,
            str(CLI),
            "start-follow-up",
            str(self.repo),
            self.review_id,
            name,
            "--kind",
            kind,
            cwd=self.repo,
            check=check,
        )

    def test_a_repeated_chained_follow_up_returns_the_same_successor(self) -> None:
        # The terminal dashboard recommends this command to whichever role runs
        # it, so both roles can reach it. Two successors would mean two agents
        # running the same round with separate thread histories.
        self._terminate()
        first = self._follow_up("review-structure").stdout
        second = self._follow_up("review-structure").stdout

        self.assertIn("status: created", first)
        self.assertIn("status: existing", second)
        self.assertEqual(_field(first, "review_id"), _field(second, "review_id"))
        loops = sorted(
            path.name
            for path in (self.repo / ".local" / "reviews").glob("*.json")
            if path.name.count(".") == 1
        )
        self.assertEqual(len(loops), 2, "one prior and one successor, not two successors")

    def test_simultaneous_follow_ups_create_one_successor_and_both_join_it(self) -> None:
        # Two agents reaching the recommendation together is the case that made
        # chaining idempotent, so the processes are started with no barrier
        # between them and left to interleave however the machine schedules them.
        self._terminate()
        command = [
            sys.executable,
            str(CLI),
            "start-follow-up",
            str(self.repo),
            self.review_id,
            "review-structure",
            "--kind",
            "structure",
        ]
        environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        racers = [
            subprocess.Popen(
                command,
                cwd=self.repo,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
            )
            for _ in range(3)
        ]
        results = [racer.communicate(timeout=60) for racer in racers]

        for racer, (_, errors) in zip(racers, results):
            self.assertEqual(racer.returncode, 0, errors)
        reported = {_field(out, "review_id") for out, _ in results}
        self.assertEqual(len(reported), 1, f"callers disagreed on the successor: {reported}")
        self.assertEqual(self._live_successors(), list(reported))

    def test_a_duplicate_left_by_a_dead_process_is_retired_on_the_next_call(self) -> None:
        # A process killed between creating its loop and converging leaves a
        # second live successor behind. Nothing holds a lock to clean up after
        # it, so the repair has to come from the next caller reading the files.
        self._terminate()
        first = _field(self._follow_up("review-structure").stdout, "review_id")
        orphan = self._create_successor_directly("orphaned-structure")
        self.assertEqual(len(self._live_successors()), 2, "the duplicate was not staged")

        joined = self._follow_up("review-structure").stdout

        self.assertIn("status: existing", joined)
        self.assertEqual(_field(joined, "review_id"), first)
        self.assertEqual(self._live_successors(), [first])
        retired = self.repo / ".local" / "reviews" / f"{orphan}.retired.json"
        self.assertTrue(retired.is_file(), "retirement must leave a tombstone, not a deletion")

    def test_a_successor_with_history_wins_over_an_older_eventless_one(self) -> None:
        # Creation order must not decide this one. The older loop published
        # nothing and the later loop holds the only review history, so ordering
        # would retire the findings.
        self._terminate()
        older = _field(self._follow_up("review-structure").stdout, "review_id")
        # Both roles run the name the terminal dashboard recommends, so a real
        # duplicate carries the same name as the round being joined.
        published = self._create_successor_directly("review-structure")
        self._publish_a_thread(published)
        self.assertEqual(len(self._live_successors()), 2, "the duplicate was not staged")

        joined = self._follow_up("review-structure").stdout

        self.assertEqual(_field(joined, "review_id"), published)
        self.assertEqual(self._live_successors(), [published])
        retired = self.repo / ".local" / "reviews" / f"{older}.retired.json"
        self.assertTrue(retired.is_file(), "the eventless loop should have been retired")

    def test_concurrent_follow_ups_with_different_names_settle_on_one_round(self) -> None:
        # Only one successor may be live per prior review and kind, so two
        # callers asking for different rounds cannot both win. This asserts the
        # end state under real concurrency; which branch the loser took depends
        # on scheduling, so the post-creation branch is covered deterministically
        # by test_a_caller_that_loses_after_creating_retires_its_own_loop.
        self._terminate()
        environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        racers = [
            subprocess.Popen(
                [
                    sys.executable,
                    str(CLI),
                    "start-follow-up",
                    str(self.repo),
                    self.review_id,
                    name,
                    "--kind",
                    "structure",
                ],
                cwd=self.repo,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
            )
            for name in ("alpha-structure", "beta-structure")
        ]
        results = [racer.communicate(timeout=60) for racer in racers]

        self.assertEqual(
            sorted(racer.returncode for racer in racers),
            [0, 1],
            "exactly one caller may take the round",
        )
        refused = next(
            errors
            for racer, (_, errors) in zip(racers, results)
            if racer.returncode == 1
        )
        self.assertIn("under a different name", refused)
        self.assertEqual(len(self._live_successors()), 1)

    def test_a_caller_that_loses_after_creating_retires_its_own_loop(self) -> None:
        """The post-creation exception to the no-side-effect contract.

        The competitor is injected between this call's creation and its
        settlement, which is the one window the sequential path never enters, so
        the branch is reached by construction rather than by winning a race.
        """
        self._terminate()
        mine: list[str] = []
        real_command_init = review_cli.command_init

        def create_then_let_a_competitor_land(init_args):
            result = real_command_init(init_args)
            mine.extend(init_args.created_review_id)
            # Stamped earlier than the loop just created, so the competitor wins
            # the selection and this caller becomes the loser.
            self._create_successor_directly(
                "beta-structure",
                review_id="ykkkkkkk",
                created_at="2000-01-01T00:00:00+00:00",
            )
            return result

        arguments = argparse.Namespace(
            repo=str(self.repo),
            prior_review_id=self.review_id,
            name="alpha-structure",
            review_kind="structure",
            structure_policy=None,
            base_ref=None,
        )
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(
                    review_cli,
                    "command_init",
                    side_effect=create_then_let_a_competitor_land,
                )
            )
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            errors = stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            exit_code = review_cli.command_start_follow_up(arguments)

        self.assertEqual(exit_code, 1)
        self.assertIn("under a different name", errors.getvalue())
        self.assertEqual(len(mine), 1, "the call under test must have created a loop")
        tombstone = self.repo / ".local" / "reviews" / f"{mine[0]}.retired.json"
        self.assertTrue(
            tombstone.is_file(),
            "the loser must retire the loop it created, not abandon it",
        )
        self.assertEqual(self._live_successors(), ["ykkkkkkk"])

    def test_a_wrong_name_request_is_refused_without_retiring_anything(self) -> None:
        # Refusing a request must not change which loops exist. Selection reads
        # the documents and decides; only an accepted request settles duplicates.
        self._terminate()
        first = _field(self._follow_up("review-structure").stdout, "review_id")
        duplicate = self._create_successor_directly("review-structure")
        self.assertEqual(len(self._live_successors()), 2, "the duplicate was not staged")

        completed = self._follow_up("a-different-round", check=False)

        self.assertEqual(completed.returncode, 1)
        self.assertIn("under a different name", completed.stderr)
        self.assertEqual(
            self._live_successors(),
            sorted([first, duplicate]),
            "a refused request must retire nothing",
        )
        for review_id in (first, duplicate):
            retired = self.repo / ".local" / "reviews" / f"{review_id}.retired.json"
            self.assertFalse(retired.is_file(), f"{review_id} should not be retired")

    def test_a_duplicate_that_cannot_be_retired_fails_instead_of_reporting_one(self) -> None:
        # Retirement blocked by a real condition — here another agent's lease —
        # must not leave the caller believing it was handed the only live loop.
        self._terminate()
        self._follow_up("review-structure")
        blocked = self._create_successor_directly("review-structure")
        (self.repo / ".local" / "reviews" / f"{blocked}.lease.json").write_text("{}\n")
        self.assertEqual(len(self._live_successors()), 2, "the duplicate was not staged")

        completed = self._follow_up("review-structure", check=False)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("did not converge to one", completed.stderr)
        self.assertIn(blocked, completed.stderr)
        self.assertEqual(len(self._live_successors()), 2, "nothing was retirable")

    def test_two_successors_with_published_events_refuse_to_converge(self) -> None:
        # Retiring either one would destroy review history, so this is the case
        # convergence must refuse instead of resolving on its own.
        self._terminate()
        first = _field(self._follow_up("review-structure").stdout, "review_id")
        second = self._create_successor_directly("second-structure")
        for successor in (first, second):
            self._publish_a_thread(successor)

        completed = self._follow_up("review-structure", check=False)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("more than one live successor has published events", completed.stderr)
        self.assertIn(first, completed.stderr)
        self.assertIn(second, completed.stderr)

    def test_an_existing_successor_reports_the_scope_to_adopt(self) -> None:
        # A successor with no guard and no events declares no scope, so whoever
        # inspects it first decides what it covers. The caller is told in the
        # words it must act on, never as a flag encoded into a text channel.
        self._terminate()
        self._follow_up("review-structure")
        joined = self._follow_up("review-structure").stdout

        self.assertIn("scope: not yet declared", joined)
        self.assertIn("phase: awaiting_initial_review", joined)
        self.assertIn(f"prior_review_id: {self.review_id}", joined)
        # A Python bool rendered into this channel reads as "True"/"False",
        # which is truthy either way to a caller testing the parsed value.
        self.assertNotIn("True", joined)
        self.assertNotIn("False", joined)

    def test_an_existing_successor_reports_the_scope_it_already_guards(self) -> None:
        # A loop can be guarded before it publishes anything. Reading only its
        # history would call that loop undeclared and send the caller to adopt
        # the prior review's scope over the one the loop actually holds.
        self._terminate()
        successor_id = _field(self._follow_up("review-structure").stdout, "review_id")
        run(sys.executable, str(CLI), "lock", "acquire", str(self.repo),
            successor_id, cwd=self.repo)
        run(sys.executable, str(CLI), "inspect", str(self.repo), successor_id,
            "example.txt", cwd=self.repo)

        joined = self._follow_up("review-structure").stdout
        self.assertIn("scope: example.txt", joined)
        self.assertNotIn("not yet declared", joined)

    def test_a_follow_up_under_a_different_name_is_refused(self) -> None:
        # Same prior and kind but a different name means a different round was
        # intended; handing back the running loop would hide that.
        self._terminate()
        self._follow_up("review-structure")
        refused = self._follow_up("adapter-only-structure", check=False)

        self.assertEqual(refused.returncode, 1)
        self.assertIn("under a different name", refused.stderr)
        self.assertIn("review-structure", refused.stderr)

    def test_a_later_round_of_the_same_kind_is_allowed_once_the_first_closes(
        self,
    ) -> None:
        # The invariant is one *live* successor per prior and kind, not one ever.
        self._terminate()
        first_id = _field(self._follow_up("review-structure").stdout, "review_id")
        successor_path = self.repo / ".local" / "reviews" / f"{first_id}.json"
        document = json.loads(successor_path.read_text())
        document["state"]["workflow"]["phase"] = "terminal"
        successor_path.write_text(json.dumps(document, indent=2) + "\n")

        later = self._follow_up("review-structure-again").stdout
        self.assertIn("status: created", later)
        self.assertNotEqual(_field(later, "review_id"), first_id)


if __name__ == "__main__":
    unittest.main()
