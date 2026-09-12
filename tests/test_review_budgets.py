"""Byte budgets for the views an agent reads on every turn.

The compact views exist to keep per-call and per-handoff cost off an agent's
context, and nothing else in the suite would notice if one of them grew back.
These budgets are the gate: each is an absolute ceiling on a view whose size is
supposed to stay bounded, measured against a deliberately large guarded scope at
every stage of one loop — including after most threads have resolved, which is
where a view that tracks history rather than outstanding work gives itself away.

Absolute ceilings are given only to views whose size is bounded by design.
`threads --json`, `draft show`, and canonical JSON grow with the review's work
without a fixed limit, so a ceiling on them would encode the fixture rather than
a contract; the compact-to-full ratio and non-growth assertions below carry that
part instead, and they keep holding as the fixture grows.

Composer acknowledgments are bounded by their own shape: each one carries an
operation name, an outstanding count, a status, a count of what the act dropped,
the identifier `drop` would name the act by, and the gap the act opened alongside
it. The ceiling is what forbids an acknowledgment from growing into a draft path
or an echo of the operation it recorded.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
CLI = ROOT / "scripts" / "review_cli.py"

# A scope wide enough that a per-file payload shows up plainly in the numbers.
GUARDED_FILE_COUNT = 60

# The stages the loop is measured at. The two "2 resolved" stages carry the case the
# plan calls for and the first pair cannot show: durable history has accumulated and
# most threads are settled, so a view that grows with history rather than with open
# work is caught here and nowhere else.
INITIAL = "awaiting_initial_review"
FRESH_OWNER = "owner_response"
FRESH_REVIEWER = "reviewer_verification"
SETTLED_OWNER = "owner_response, 2 resolved"
SETTLED_REVIEWER = "reviewer_verification, 2 resolved"
TERMINAL = "terminal"

# The evidence every composed act carries, as the typed flags the composer takes.
EVIDENCE = (
    "--basis",
    "source_inspection",
    "--provenance",
    "src/module_00.py",
    "--sanitized-result",
    "The guarded path carries the declared behavior.",
)

# Ceiling, measured value, and why the view is bounded. Raise one only with a
# measurement and a reason; a rise with neither is the regression this catches.
# Every stage is budgeted, because a view that grows only once history settles
# would otherwise pass on the fresh stages alone.
BUDGET_BY_CASE = {
    # The card carries phase, actor, allowed events, next command, sequence, and
    # obligations. Flat across history: it describes the work outstanding, not the
    # history behind it. The reviewer_verification stages are larger only because a
    # final_review is allowed there, so the card carries the accretion flagged set.
    f"inspect --agent ({FRESH_OWNER})": (1_800, 1_227),
    f"inspect --agent ({SETTLED_OWNER})": (1_800, 1_207),
    f"inspect --agent ({FRESH_REVIEWER})": (3_500, 2_833),
    f"inspect --agent ({SETTLED_REVIEWER})": (3_500, 2_813),
    f"inspect --agent ({TERMINAL})": (1_200, 835),
    # Identity, priority, status, title, and paths for the open threads alone, so it
    # shrinks as threads resolve rather than growing with the conversation.
    f"threads --summary --open --json ({FRESH_OWNER})": (800, 426),
    f"threads --summary --open --json ({FRESH_REVIEWER})": (800, 426),
    f"threads --summary --open --json ({SETTLED_OWNER})": (300, 144),
    f"threads --summary --open --json ({SETTLED_REVIEWER})": (300, 144),
    f"threads --summary --open ({FRESH_OWNER})": (300, 158),
    f"threads --summary --open ({FRESH_REVIEWER})": (300, 158),
    f"threads --summary --open ({SETTLED_OWNER})": (200, 68),
    f"threads --summary --open ({SETTLED_REVIEWER})": (200, 68),
    # The human form carries the dashboard lines plus the same card as sentences.
    f"inspect (human, {FRESH_OWNER})": (7_000, 5_011),
    f"inspect (human, {SETTLED_OWNER})": (7_000, 5_016),
    f"inspect (human, {FRESH_REVIEWER})": (7_000, 5_279),
    f"inspect (human, {SETTLED_REVIEWER})": (7_000, 5_275),
    # Without a reachable final_review the ledger is absent, which is step 1's whole
    # effect; where one is reachable the ledger is carried by contract.
    f"inspect --json ({FRESH_OWNER})": (2_500, 1_676),
    f"inspect --json ({SETTLED_OWNER})": (2_500, 1_656),
    f"inspect --json ({FRESH_REVIEWER})": (20_000, 15_434),
    f"inspect --json ({SETTLED_REVIEWER})": (20_000, 15_414),
    f"inspect --json ({TERMINAL})": (2_000, 1_339),
    # One line of fixed fields, whatever the operation recorded: an operation
    # name, an outstanding count, a status, a count of the operations the act
    # dropped, the identifier `drop` names it by, and the gap it opened. The second
    # number here is that shape's own length rather than a reading off a run,
    # because the shape is what bounds it; the ceiling is roughly twice it, so an
    # acknowledgment that started carrying a draft path or an echo of its operation
    # fails here.
    "draft open-thread (acknowledgment)": (160, 109),
    "draft reply (acknowledgment)": (160, 109),
    "draft record-check (acknowledgment)": (160, 124),
}

# A compact view must stay a small fraction of the full one it replaces. Unlike an
# absolute ceiling this keeps its meaning as the fixture grows, so both pairs are
# taken at the most settled stage, where the full view carries the whole conversation.
MAX_COMPACT_FRACTION = {
    (
        f"threads --summary --open --json ({SETTLED_REVIEWER})",
        f"threads --json ({SETTLED_REVIEWER})",
    ): 0.10,
    (
        f"inspect --agent ({SETTLED_REVIEWER})",
        f"inspect --json ({SETTLED_REVIEWER})",
    ): 0.25,
}


def run(*arguments: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


class CompactViewBudgetTest(unittest.TestCase):
    """Walk one loop to terminal, recording every view's size along the way."""

    measured: dict[str, int]

    @classmethod
    def setUpClass(cls) -> None:
        cls.directory = tempfile.mkdtemp()
        cls.repo = Path(cls.directory) / "fixture"
        cls.repo.mkdir(parents=True)
        cls.measured = {}
        cls.build_repository()
        cls.review_id = cls.open_loop()
        cls.walk_loop()

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.directory, ignore_errors=True)

    # --- fixture construction ---

    @classmethod
    def cli(cls, *arguments: str) -> str:
        return run(sys.executable, str(CLI), *arguments, cwd=cls.repo).stdout

    @classmethod
    def build_repository(cls) -> None:
        run("git", "init", "-q", cwd=cls.repo)
        run("git", "config", "user.email", "budget@example.com", cwd=cls.repo)
        run("git", "config", "user.name", "Budget Fixture", cwd=cls.repo)
        (cls.repo / ".gitignore").write_text(".local/\n")
        (cls.repo / "src").mkdir()
        cls.guarded = [f"src/module_{index:02d}.py" for index in range(GUARDED_FILE_COUNT)]
        for name in cls.guarded:
            (cls.repo / name).write_text("value = 1\n" * 10)
        run("git", "add", "-A", cwd=cls.repo)
        run("git", "commit", "-qm", "initial", cwd=cls.repo)

    @classmethod
    def open_loop(cls) -> str:
        output = cls.cli("init", str(cls.repo), "budget", "--base-ref", "HEAD")
        review_id = next(
            line.split(": ", 1)[1]
            for line in output.splitlines()
            if line.startswith("review_id: ")
        )
        # Growth past the 20% threshold flags every guarded file, which is the
        # largest accretion payload the views can be asked to carry.
        for name in cls.guarded:
            (cls.repo / name).write_text("value = 1\n" * 30)
        return review_id

    @classmethod
    def inspect(cls, *flags: str) -> str:
        return cls.cli("inspect", str(cls.repo), cls.review_id, *flags, "src")

    @classmethod
    def threads(cls, *flags: str) -> str:
        return cls.cli("threads", str(cls.repo), cls.review_id, *flags)

    @classmethod
    def draft(cls, *arguments: str) -> str:
        return cls.cli("draft", str(cls.repo), cls.review_id, *arguments)

    @classmethod
    def guarded_draft(cls, kind: str, stage: str) -> None:
        """Lock, guard, and template one transaction, recording what it still owes."""
        cls.cli("lock", "acquire", str(cls.repo), cls.review_id)
        cls.inspect("--json")
        cls.cli("template", str(cls.repo), cls.review_id, kind)
        cls.measured[f"draft show ({stage})"] = len(cls.draft("show"))

    @classmethod
    def publish(cls) -> None:
        result = json.loads(cls.cli("publish", str(cls.repo), cls.review_id))
        if not result["committed"]:
            raise AssertionError(f"fixture publish failed: {result}")

    @classmethod
    def record_check(cls) -> str:
        return cls.draft(
            "record-check", "--check", "source inspection", "--result", "passed"
        )

    @classmethod
    def walk_loop(cls) -> None:
        """Publish review, owner_reply, reviewer_update, and final_review in turn."""
        cls.guarded_draft("review", INITIAL)
        for index in range(1, 4):
            acknowledgment = cls.draft(
                "open-thread",
                "--title",
                f"Finding {index}",
                "--risk",
                "The guarded path returns an undocumented result.",
                "--required-behavior",
                "Return the documented result.",
                "--paths",
                f"src/module_{index:02d}.py",
                *EVIDENCE,
            )
            if index == 1:
                cls.measured["draft open-thread (acknowledgment)"] = len(acknowledgment)
        cls.measured["draft record-check (acknowledgment)"] = len(cls.record_check())
        cls.publish()

        cls.record(FRESH_OWNER)

        cls.guarded_draft("owner_reply", FRESH_OWNER)
        cls.reply_to(["T1", "T2", "T3"], measure=True)
        cls.record_check()
        cls.publish()

        cls.record(FRESH_REVIEWER)

        # Resolve two of the three; a reviewer_update must leave one open, so this is
        # the most resolved history the loop can hold before its terminal.
        cls.guarded_draft("reviewer_update", FRESH_REVIEWER)
        cls.draft("comment", "T3", "--message", "Still verifying this one.")
        for thread_id in ("T1", "T2"):
            cls.draft(
                "resolve",
                thread_id,
                "--message",
                "Checked against the guarded tree.",
                "--verified",
                *EVIDENCE,
            )
        cls.record_check()
        cls.publish()

        cls.record(SETTLED_OWNER)

        cls.guarded_draft("owner_reply", SETTLED_OWNER)
        cls.reply_to(["T3"])
        cls.record_check()
        cls.publish()

        cls.record(SETTLED_REVIEWER)

        cls.guarded_draft("final_review", SETTLED_REVIEWER)
        cls.draft("resolve", "T3", "--message", "Verified against the guarded tree.")
        cls.draft(
            "approve",
            "--decision",
            "LGTM",
            "--structure-disposition",
            "structure_deferred",
            "--structure-message",
            "Real accretion; a structure round should follow.",
        )
        cls.record_check()
        cls.publish()

        cls.measured[f"inspect --agent ({TERMINAL})"] = len(cls.inspect("--agent"))
        cls.measured[f"inspect --json ({TERMINAL})"] = len(cls.inspect("--json"))

    @classmethod
    def reply_to(cls, thread_ids: list[str], *, measure: bool = False) -> None:
        cls.draft(
            "reply-context",
            "--drift",
            "Only the guarded source changed.",
            "--guide",
            "No behavior guide needed a change.",
        )
        for thread_id in thread_ids:
            acknowledgment = cls.draft(
                "reply",
                thread_id,
                "--decision",
                "applied",
                "--message",
                "Applied the documented behavior.",
                *EVIDENCE,
            )
            if measure and thread_id == thread_ids[0]:
                cls.measured["draft reply (acknowledgment)"] = len(acknowledgment)

    @classmethod
    def record(cls, stage: str) -> None:
        """Record every view at this stage.

        Uniform rather than selective: a stage that measured only some views is how
        the resolved-thread case went missing in the first place.
        """
        cls.measured[f"inspect --agent ({stage})"] = len(cls.inspect("--agent"))
        cls.measured[f"inspect --json ({stage})"] = len(cls.inspect("--json"))
        cls.measured[f"inspect (human, {stage})"] = len(cls.inspect())
        cls.measured[f"threads --summary --open --json ({stage})"] = len(
            cls.threads("--summary", "--open", "--json")
        )
        cls.measured[f"threads --summary --open ({stage})"] = len(
            cls.threads("--summary", "--open")
        )
        cls.measured[f"threads --json ({stage})"] = len(cls.threads("--json"))

    # --- budgets ---

    def test_every_budgeted_view_stays_within_its_ceiling(self) -> None:
        for case, (budget, recorded) in BUDGET_BY_CASE.items():
            with self.subTest(case=case):
                self.assertIn(case, self.measured, "the fixture never measured this view")
                size = self.measured[case]
                self.assertLessEqual(
                    size,
                    budget,
                    f"{case} grew to {size} B against a {budget} B budget "
                    f"(it measured {recorded} B when the budget was set)",
                )

    def test_the_phase_that_cannot_publish_a_final_review_drops_the_ledger(self) -> None:
        # Step 1's effect stated as a relationship rather than two absolute numbers,
        # so it keeps its meaning if the fixture's scope changes.
        self.assertLess(
            self.measured["inspect --json (owner_response)"] * 4,
            self.measured["inspect --json (reviewer_verification)"],
        )

    def test_compact_views_do_not_grow_as_durable_history_accumulates(self) -> None:
        """Compare each stage with the same phase two events later.

        Absolute ceilings can hold while a view quietly starts tracking history, so
        this states the property directly: the full conversation view must grow, and
        every compact view must not.
        """
        self.assertGreater(
            self.measured[f"threads --json ({SETTLED_REVIEWER})"],
            self.measured[f"threads --json ({FRESH_REVIEWER})"],
            "the fixture no longer accumulates history, so this proves nothing",
        )
        settled_by_fresh = {
            FRESH_OWNER: SETTLED_OWNER,
            FRESH_REVIEWER: SETTLED_REVIEWER,
        }
        compact_views = (
            "inspect --agent",
            "threads --summary --open --json",
            "threads --summary --open",
        )
        for view in compact_views:
            for fresh, settled in settled_by_fresh.items():
                with self.subTest(view=view, stage=settled):
                    self.assertLessEqual(
                        self.measured[f"{view} ({settled})"],
                        self.measured[f"{view} ({fresh})"],
                        f"{view} grew as history accumulated, so it is tracking the "
                        "conversation rather than the work outstanding",
                    )

    def test_draft_show_reports_outstanding_work_rather_than_history(self) -> None:
        # Both stages template the same kind, so the only difference is how many
        # obligations are left: three replies owed against one.
        self.assertLess(
            self.measured[f"draft show ({SETTLED_OWNER})"],
            self.measured[f"draft show ({FRESH_OWNER})"],
            "draft show is tracking the conversation rather than what the draft owes",
        )

    def test_each_compact_view_stays_a_small_fraction_of_the_full_one(self) -> None:
        for (compact, full), fraction in MAX_COMPACT_FRACTION.items():
            with self.subTest(compact=compact):
                self.assertLessEqual(
                    self.measured[compact],
                    self.measured[full] * fraction,
                    f"{compact} is no longer a compact alternative to {full}",
                )


if __name__ == "__main__":
    unittest.main()
