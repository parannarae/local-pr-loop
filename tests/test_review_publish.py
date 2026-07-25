"""Publication fault-injection tests around the canonical commit point."""

from __future__ import annotations

import importlib.util
import json
import shlex
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


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
            "staged_sha256": "0" * 64,
            "unstaged_sha256": "0" * 64,
            "untracked": [],
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
            snapshot_script=str(ROOT / "scripts" / "source_snapshot.py"),
            expected_review_sha=publisher.sha256(self.review),
            expected_source_fingerprint="1" * 64,
            scope_args=["example.txt"],
            lease=None,
            guard=None,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_canonical_replace_failure_is_precommit(self) -> None:
        real_atomic = publisher.atomic_bytes

        def fail_canonical(path: Path, content: bytes) -> None:
            if path == self.review:
                raise OSError("canonical move injected")
            real_atomic(path, content)

        with (
            mock.patch.object(publisher, "atomic_bytes", side_effect=fail_canonical),
            mock.patch.object(publisher, "verify_lock"),
            mock.patch.object(
                publisher,
                "current_snapshot",
                return_value=json.loads(self.event.read_text())["source_snapshot"],
            ),
        ):
            self.assertEqual(publisher.publish(self.args), 1)
        self.assertEqual(json.loads(self.review.read_text())["history"], [])
        self.assertTrue(self.journal.exists())

        with mock.patch.object(publisher, "verify_lock"):
            self.assertEqual(publisher.recover(self.args), 0)
        self.assertFalse(self.journal.exists())
        self.assertTrue(self.event.exists())
        self.assertEqual(json.loads(self.review.read_text())["history"], [])

    def test_report_failure_is_postcommit_and_receipt_survives(self) -> None:
        with (
            mock.patch.object(
                publisher, "write_report", side_effect=OSError("report move injected")
            ),
            mock.patch.object(publisher, "verify_lock"),
            mock.patch.object(
                publisher,
                "current_snapshot",
                return_value=json.loads(self.event.read_text())["source_snapshot"],
            ),
        ):
            self.assertEqual(publisher.publish(self.args), 1)
        document = json.loads(self.review.read_text())
        self.assertEqual(len(document["history"]), 1)
        self.assertTrue(self.journal.exists())
        self.assertTrue(self.event.exists())

        with (
            mock.patch.object(publisher, "verify_lock"),
            mock.patch.object(publisher, "release_lock"),
        ):
            self.assertEqual(publisher.recover(self.args), 0)
        self.assertFalse(self.journal.exists())
        self.assertFalse(self.event.exists())
        self.assertEqual(len(json.loads(self.review.read_text())["history"]), 1)

    def test_operation_distinguishes_ready_and_prepared_states(self) -> None:
        repository_path = "/tmp/review repo;unsafe"
        operation_args = Namespace(
            review=str(self.review),
            event=str(self.event),
            report=str(self.report),
            journal=str(self.journal),
            state_script=str(ROOT / "scripts" / "review_state.py"),
            lock_json='{"review_file": "review.json"}',
            repo=repository_path,
            review_id="abcdefgh",
            current_source_fingerprint="1" * 64,
            lease_present=True,
            json=True,
            command_prefix="python3 review_cli.py",
        )
        with mock.patch("builtins.print") as output:
            self.assertEqual(publisher.operation(operation_args), 0)
        dashboard = json.loads(output.call_args.args[0])
        self.assertEqual(dashboard["operation"]["status"], "ready_to_publish")
        self.assertEqual(
            shlex.split(dashboard["recommended_next_command"]),
            [
                "python3",
                "review_cli.py",
                "publish",
                repository_path,
                "abcdefgh",
            ],
        )

        real_atomic = publisher.atomic_bytes

        def fail_canonical(path: Path, content: bytes) -> None:
            if path == self.review:
                raise OSError("canonical move injected")
            real_atomic(path, content)

        with (
            mock.patch.object(publisher, "atomic_bytes", side_effect=fail_canonical),
            mock.patch.object(publisher, "verify_lock"),
            mock.patch.object(
                publisher,
                "current_snapshot",
                return_value=json.loads(self.event.read_text())["source_snapshot"],
            ),
        ):
            publisher.publish(self.args)
        with mock.patch("builtins.print") as output:
            self.assertEqual(publisher.operation(operation_args), 0)
        self.assertIn('"status": "prepared_precommit"', output.call_args.args[0])

    def test_clean_terminal_operation_recommends_no_command(self) -> None:
        document = json.loads(self.review.read_text())
        state.append_event(document, json.loads(self.event.read_text()))
        self.review.write_text(json.dumps(document, indent=2) + "\n")
        self.event.unlink()
        publisher.write_report(state, document, self.report)
        operation_args = Namespace(
            review=str(self.review),
            event=str(self.event),
            report=str(self.report),
            journal=str(self.journal),
            state_script=str(ROOT / "scripts" / "review_state.py"),
            lock_json="unlocked",
            repo=str(self.review.parent),
            review_id="abcdefgh",
            current_source_fingerprint="1" * 64,
            lease_present=False,
            json=True,
            command_prefix="python3 review_cli.py",
        )

        with mock.patch("builtins.print") as output:
            self.assertEqual(publisher.operation(operation_args), 0)

        dashboard = json.loads(output.call_args.args[0])
        self.assertEqual(dashboard["workflow"]["phase"], "terminal")
        self.assertEqual(dashboard["operation"]["status"], "clean")
        self.assertEqual(dashboard["recommended_next_command"], "none")

    def test_event_removal_failure_is_postcommit(self) -> None:
        real_unlink = Path.unlink

        def fail_event(path: Path, *args, **kwargs):
            if path == self.event:
                raise OSError("event removal injected")
            return real_unlink(path, *args, **kwargs)

        with (
            mock.patch.object(Path, "unlink", autospec=True, side_effect=fail_event),
            mock.patch.object(publisher, "verify_lock"),
            mock.patch.object(
                publisher,
                "current_snapshot",
                return_value=json.loads(self.event.read_text())["source_snapshot"],
            ),
        ):
            self.assertEqual(publisher.publish(self.args), 1)
        self.assertEqual(len(json.loads(self.review.read_text())["history"]), 1)
        self.assertTrue(self.journal.exists())

        operation_args = Namespace(
            review=str(self.review),
            event=str(self.event),
            report=str(self.report),
            journal=str(self.journal),
            state_script=str(ROOT / "scripts" / "review_state.py"),
            lock_json="unlocked",
            repo=str(self.review.parent),
            review_id="abcdefgh",
            current_source_fingerprint="2" * 64,
            lease_present=False,
            json=True,
            command_prefix="python3 review_cli.py",
        )
        with mock.patch("builtins.print") as output:
            self.assertEqual(publisher.operation(operation_args), 0)

        dashboard = json.loads(output.call_args.args[0])
        self.assertTrue(dashboard["source"]["approval_stale"])
        self.assertEqual(
            shlex.split(dashboard["recommended_next_command"]),
            [
                "python3",
                "review_cli.py",
                "recover-publish",
                str(self.review.parent),
                "abcdefgh",
            ],
        )

    def test_receipt_phase_update_failure_remains_recoverable(self) -> None:
        real_atomic_json = publisher.atomic_json

        def fail_phase(path: Path, value: dict) -> None:
            if value.get("commit_phase") == "canonical_committed":
                raise OSError("receipt phase update injected")
            real_atomic_json(path, value)

        with (
            mock.patch.object(publisher, "atomic_json", side_effect=fail_phase),
            mock.patch.object(publisher, "verify_lock"),
            mock.patch.object(
                publisher,
                "current_snapshot",
                return_value=json.loads(self.event.read_text())["source_snapshot"],
            ),
        ):
            self.assertEqual(publisher.publish(self.args), 1)
        self.assertEqual(len(json.loads(self.review.read_text())["history"]), 1)
        self.assertEqual(
            json.loads(self.journal.read_text())["commit_phase"], "prepared"
        )
        with (
            mock.patch.object(publisher, "verify_lock"),
            mock.patch.object(publisher, "release_lock"),
        ):
            self.assertEqual(publisher.recover(self.args), 0)
        self.assertEqual(len(json.loads(self.review.read_text())["history"]), 1)

    def test_corrupt_receipt_has_explicit_operation_state(self) -> None:
        self.journal.write_text('{"cookie": "secret"}\n')
        operation_args = Namespace(
            review=str(self.review),
            event=str(self.event),
            report=str(self.report),
            journal=str(self.journal),
            state_script=str(ROOT / "scripts" / "review_state.py"),
            lock_json="unlocked",
            repo=str(self.review.parent),
            review_id="abcdefgh",
            current_source_fingerprint=None,
            lease_present=False,
            json=True,
            command_prefix="python3 review_cli.py",
        )
        with mock.patch("builtins.print") as output:
            self.assertEqual(publisher.operation(operation_args), 0)
        self.assertIn('"status": "corrupt_artifact"', output.call_args.args[0])

    def test_prepared_recovery_without_token_preserves_receipt(self) -> None:
        real_atomic = publisher.atomic_bytes

        def fail_canonical(path: Path, content: bytes) -> None:
            if path == self.review:
                raise OSError("canonical move injected")
            real_atomic(path, content)

        with (
            mock.patch.object(publisher, "atomic_bytes", side_effect=fail_canonical),
            mock.patch.object(publisher, "verify_lock"),
            mock.patch.object(
                publisher,
                "current_snapshot",
                return_value=json.loads(self.event.read_text())["source_snapshot"],
            ),
        ):
            publisher.publish(self.args)
        self.args.token = None
        self.assertEqual(publisher.recover(self.args), 1)
        self.assertTrue(self.journal.exists())


if __name__ == "__main__":
    unittest.main()
