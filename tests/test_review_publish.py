"""Publication fault-injection tests around the canonical commit point."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parents[1]


def import_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


publisher = import_module("review_publish", ROOT / "scripts" / "review_publish.py")
state = import_module("review_state_for_publish", ROOT / "scripts" / "review_state.py")


class ReviewPublishFaultTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.review = root / "review.json"
        self.event = root / "review.event.json"
        self.report = root / "review.latest.md"
        self.journal = root / "review.publish.json"
        document = state.new_document("abcdefgh", "review")
        self.review.write_text(json.dumps(document, indent=2) + "\n")
        event = state.event_template("final_review")
        event["source_snapshot"] = {
            "revision": "1" * 40,
            "scope": ["example.txt"],
            "fingerprint": "1" * 64,
            "exclusions": [],
            "additional_inputs": [],
        }
        event["decision"] = "LGTM"
        self.event.write_text(json.dumps(event, indent=2) + "\n")
        self.args = Namespace(
            review=str(self.review),
            event=str(self.event),
            report=str(self.report),
            journal=str(self.journal),
            state_script=str(ROOT / "scripts" / "review_state.py"),
            lock_script=str(ROOT / "scripts" / "review_lock.py"),
            repo=str(root),
            token="token",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_canonical_replace_failure_is_precommit(self) -> None:
        real_atomic = publisher.atomic_bytes

        def fail_canonical(path: Path, content: bytes) -> None:
            if path == self.review:
                raise OSError("canonical move injected")
            real_atomic(path, content)

        with mock.patch.object(publisher, "atomic_bytes", side_effect=fail_canonical):
            self.assertEqual(publisher.publish(self.args), 1)
        self.assertEqual(json.loads(self.review.read_text())["history"], [])
        self.assertFalse(self.journal.exists())

    def test_report_failure_is_postcommit_and_receipt_survives(self) -> None:
        with mock.patch.object(
            publisher, "write_report", side_effect=OSError("report move injected")
        ):
            self.assertEqual(publisher.publish(self.args), 1)
        document = json.loads(self.review.read_text())
        self.assertEqual(len(document["history"]), 1)
        self.assertTrue(self.journal.exists())
        self.assertTrue(self.event.exists())

    def test_event_removal_failure_is_postcommit(self) -> None:
        real_unlink = Path.unlink

        def fail_event(path: Path, *args, **kwargs):
            if path == self.event:
                raise OSError("event removal injected")
            return real_unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", autospec=True, side_effect=fail_event):
            self.assertEqual(publisher.publish(self.args), 1)
        self.assertEqual(len(json.loads(self.review.read_text())["history"]), 1)
        self.assertTrue(self.journal.exists())


if __name__ == "__main__":
    unittest.main()
