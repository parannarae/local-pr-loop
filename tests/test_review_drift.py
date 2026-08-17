"""Source-drift reporting and routing tests."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def import_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


publisher = import_module("review_publish_drift", ROOT / "scripts" / "review_publish.py")


def snapshot(**overrides):
    value = {
        "revision": "1" * 40,
        "scope": ["src/app.py"],
        "exclusions": [],
        "additional_inputs": [],
        "staged_sha256": "0" * 64,
        "unstaged_sha256": "0" * 64,
        "untracked": [],
    }
    value.update(overrides)
    return value


# --- source_drift_detail ---


class SourceDriftDetailTest(unittest.TestCase):
    def test_identifies_a_modified_untracked_file_by_path(self) -> None:
        recorded = snapshot(untracked=[{"path": "new.py", "sha256": "a" * 64}])
        current = snapshot(untracked=[{"path": "new.py", "sha256": "b" * 64}])

        detail = publisher.source_drift_detail(recorded, current)

        self.assertEqual(
            detail["changed_paths"],
            [{"path": "new.py", "change": "modified", "kind": "untracked"}],
        )

    def test_identifies_added_and_removed_additional_inputs(self) -> None:
        recorded = snapshot(additional_inputs=[{"path": "old.md", "sha256": "a" * 64}])
        current = snapshot(additional_inputs=[{"path": "new.md", "sha256": "c" * 64}])

        changes = publisher.source_drift_detail(recorded, current)["changed_paths"]

        self.assertEqual(
            sorted((item["path"], item["change"]) for item in changes),
            [("new.md", "added"), ("old.md", "removed")],
        )
        self.assertTrue(all(item["kind"] == "additional_input" for item in changes))

    def test_reports_tracked_changes_as_an_aggregate_rather_than_guessing_paths(
        self,
    ) -> None:
        detail = publisher.source_drift_detail(
            snapshot(), snapshot(unstaged_sha256="9" * 64)
        )

        self.assertTrue(detail["tracked_diff_changed"])
        # Tracked paths are not recoverable from an aggregate digest, so none are claimed.
        self.assertEqual(detail["changed_paths"], [])

    def test_reports_scope_and_revision_changes(self) -> None:
        detail = publisher.source_drift_detail(
            snapshot(),
            snapshot(revision="2" * 40, scope=["src/app.py", "src/other.py"]),
        )

        self.assertTrue(detail["revision_changed"])
        self.assertEqual(detail["scope_changes"]["scope"]["added"], ["src/other.py"])
        self.assertEqual(detail["scope_changes"]["scope"]["removed"], [])

    def test_reports_no_detail_when_a_recorded_snapshot_is_unavailable(self) -> None:
        detail = publisher.source_drift_detail(None, snapshot())

        self.assertFalse(detail["available"])
        self.assertEqual(detail["changed_paths"], [])


if __name__ == "__main__":
    unittest.main()
