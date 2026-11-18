"""Checks for the filename guard used before scanning git content."""

import importlib.util
from pathlib import Path
import unittest


spec = importlib.util.spec_from_file_location("scan_secrets", Path(__file__).with_name("scan_secrets.py"))
scanner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scanner)


class SecretScanPathTests(unittest.TestCase):
    def test_runtime_and_private_artifacts_are_blocked(self):
        for path in (
            ".env", ".env.production", "nested/.env.local", ".ENV.local", ".state/browser/session.json",
            "nested/cookies-facebook.json", "config/groups.json", "data/housing.sqlite3",
            "backup.sqlite-wal", "keys/private.pem", "keys/PRIVATE.KEY", "traffic.har", "server.log",
            "security-assessment-2026-11-18/evidence.json", "gitleaks-report.json",
        ):
            with self.subTest(path=path):
                self.assertTrue(scanner.is_sensitive_path(path))

    def test_example_configuration_and_source_are_allowed(self):
        for path in (
            ".env.example", "config/groups.example.json", "tests/test_notifications.py",
            "src/fb_automator/storage.py", "requirements.lock", ".github/gitleaks.toml",
        ):
            with self.subTest(path=path):
                self.assertFalse(scanner.is_sensitive_path(path))


if __name__ == "__main__":
    unittest.main()
