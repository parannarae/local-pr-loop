"""Durability and permission tests for local operational artifacts."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts import review_io


class ReviewIoTest(unittest.TestCase):
    def test_secure_json_round_trips_as_mode_0600(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "lease.json"
            review_io.secure_json(path, {"token": "opaque"})

            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                review_io.load_secure_object(path, "lease"),
                {"token": "opaque"},
            )

    def test_secure_load_rejects_group_readable_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "lease.json"
            path.write_text(json.dumps({"token": "opaque"}))
            path.chmod(0o640)

            with self.assertRaisesRegex(ValueError, "regular 0600 file"):
                review_io.load_secure_object(path, "lease")

    def test_secure_load_rejects_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target.json"
            target.write_text(json.dumps({"token": "opaque"}))
            target.chmod(0o600)
            link = root / "lease.json"
            link.symlink_to(target)

            with self.assertRaisesRegex(ValueError, "regular 0600 file"):
                review_io.load_secure_object(link, "lease")
