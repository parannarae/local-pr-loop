"""Lock corruption, token, and concurrency tests."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MODULE = Path(__file__).parents[1] / "scripts" / "review_lock.py"
SPEC = importlib.util.spec_from_file_location("review_lock", MODULE)
assert SPEC and SPEC.loader
review_lock = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(review_lock)


class ReviewLockTest(unittest.TestCase):
    def test_concurrent_acquire_has_one_winner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "lock"
            self.assertEqual(review_lock.acquire(path, "review.json"), 0)
            self.assertEqual(review_lock.acquire(path, "review.json"), 1)

    def test_corrupt_owner_cannot_be_released(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "lock"
            path.mkdir()
            (path / "owner.json").write_text("{bad json")
            self.assertEqual(review_lock.release(path, "anything"), 1)
            self.assertTrue(path.exists())

    def test_tombstone_cleanup_failure_does_not_leave_active_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "lock"
            self.assertEqual(review_lock.acquire(path, "review.json"), 0)
            token = json.loads((path / "owner.json").read_text())["token"]
            with mock.patch.object(
                review_lock.shutil, "rmtree", side_effect=OSError("injected")
            ):
                self.assertEqual(review_lock.release(path, token), 0)
            self.assertFalse(path.exists())
            self.assertEqual(len(list(Path(temporary).glob("lock.released-*"))), 1)


if __name__ == "__main__":
    unittest.main()
