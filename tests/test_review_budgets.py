"""Byte budgets for the views an agent reads on every turn.

The compact views exist to keep per-call and per-handoff cost off an agent's
context, and nothing else in the suite would notice if one of them grew back.
These budgets are the gate: each is an absolute ceiling on a view whose size is
supposed to stay bounded, measured against a deliberately large guarded scope at
every stage of one loop — including after most threads have resolved, which is
where a view that tracks history rather than outstanding work gives itself away.

Absolute ceilings are given only to views whose size is bounded by design.
`threads --json` and canonical JSON grow with review history without limit, so a
fixed ceiling on them would encode the fixture rather than a contract; the
compact-to-full ratio assertions below carry that part instead, and they keep
holding as the fixture grows.
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
from typing import Any

ROOT = Path(__file__).parents[1]
CLI = ROOT / "scripts" / "review_cli.py"

# A scope wide enough that a per-file payload shows up plainly in the numbers.
GUARDED_FILE_COUNT = 60

# The stages the loop is measured at. The two "2 resolved" stages carry the case the
# plan calls for and the first pair cannot show: durable history has accumulated and
# most threads are settled, so a view that grows with history rather than with open
# work is caught here and nowhere else.
FRESH_OWNER = "owner_response"
FRESH_REVIEWER = "reviewer_verification"
SETTLED_OWNER = "owner_response, 2 resolved"
SETTLED_REVIEWER = "reviewer_verification, 2 resolved"
TERMINAL = "terminal"

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
        cls.build_repository()
        cls.review_id = cls.open_loop()
        cls.measured = {}
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
    def guarded_draft(cls, kind: str) -> tuple[Path, dict[str, Any]]:
        cls.cli("lock", "acquire", str(cls.repo), cls.review_id)
        cls.inspect("--json")
        cls.cli("template", str(cls.repo), cls.review_id, kind)
        path = cls.repo / ".local" / "reviews" / f"{cls.review_id}.event.json"
        return path, json.loads(path.read_text())

    @classmethod
    def publish(cls, path: Path, draft: dict[str, Any]) -> None:
        path.write_text(json.dumps(draft, indent=2) + "\n")
        result = json.loads(cls.cli("publish", str(cls.repo), cls.review_id))
        if not result["committed"]:
            raise AssertionError(f"fixture publish failed: {result}")

    @classmethod
    def evidence(cls, occurred_at: str) -> dict[str, Any]:
        return {
            "basis": "source_inspection",
            "provenance": "src/module_00.py",
            "observed_at": occurred_at,
            "sanitized_result": "The guarded path carries the declared behavior.",
        }

    @classmethod
    def walk_loop(cls) -> None:
        """Publish review, owner_reply, reviewer_update, and final_review in turn."""
        path, draft = cls.guarded_draft("review")
        blank = draft["threads"][0]
        draft["threads"] = []
        for index in range(1, 4):
            thread = json.loads(json.dumps(blank))
            thread.update(
                {
                    "id": f"T{index}",
                    "title": f"Finding {index}",
                    "risk": "The guarded path returns an undocumented result.",
                    "required_behavior": "Return the documented result.",
                    "paths": [f"src/module_{index:02d}.py"],
                }
            )
            thread["evidence"] = cls.evidence(draft["occurred_at"])
            draft["threads"].append(thread)
        draft["validation"]["performed"] = [
            {"check": "source inspection", "result": "passed"}
        ]
        cls.publish(path, draft)

        cls.record(FRESH_OWNER)

        path, draft = cls.guarded_draft("owner_reply")
        draft["source_drift_assessment"] = "Only the guarded source changed."
        draft["guide_synchronization"] = "No behavior guide needed a change."
        draft["validation"]["performed"] = [
            {"check": "source inspection", "result": "passed"}
        ]
        for reply in draft["replies"]:
            reply["message"] = "Applied the documented behavior."
            reply["evidence"] = cls.evidence(draft["occurred_at"])
        cls.publish(path, draft)

        cls.record(FRESH_REVIEWER)

        # Resolve two of the three; a reviewer_update must leave one open, so this is
        # the most resolved history the loop can hold before its terminal.
        path, draft = cls.guarded_draft("reviewer_update")
        draft["validation"]["performed"] = [
            {"check": "source inspection", "result": "passed"}
        ]
        for decision in draft["decisions"]:
            decision["message"] = "Checked against the guarded tree."
            if decision["thread_id"] in {"T1", "T2"}:
                decision["action"] = "resolve"
                decision["verification"] = {
                    "independent": True,
                    "evidence": cls.evidence(draft["occurred_at"]),
                }
            else:
                decision["action"] = "comment"
        cls.publish(path, draft)

        cls.record(SETTLED_OWNER)

        path, draft = cls.guarded_draft("owner_reply")
        draft["source_drift_assessment"] = "Only the guarded source changed."
        draft["guide_synchronization"] = "No behavior guide needed a change."
        draft["validation"]["performed"] = [
            {"check": "source inspection", "result": "passed"}
        ]
        for reply in draft["replies"]:
            reply["message"] = "Applied the remaining documented behavior."
            reply["evidence"] = cls.evidence(draft["occurred_at"])
        cls.publish(path, draft)

        cls.record(SETTLED_REVIEWER)

        path, draft = cls.guarded_draft("final_review")
        draft["decision"] = "LGTM"
        draft["validation"]["performed"] = [
            {"check": "source inspection", "result": "passed"}
        ]
        for resolution in draft["resolutions"]:
            resolution["message"] = "Verified against the guarded tree."
            resolution["verification"] = {
                "independent": True,
                "evidence": cls.evidence(draft["occurred_at"]),
            }
        draft["structure_debt"].update(
            {
                "disposition": "structure_deferred",
                "message": "Real accretion; a structure round should follow.",
            }
        )
        cls.publish(path, draft)

        cls.measured[f"inspect --agent ({TERMINAL})"] = len(cls.inspect("--agent"))
        cls.measured[f"inspect --json ({TERMINAL})"] = len(cls.inspect("--json"))

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
