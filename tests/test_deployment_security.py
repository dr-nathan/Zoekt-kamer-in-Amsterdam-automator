from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy/configure-production.py"
spec = importlib.util.spec_from_file_location("production_config", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class DeploymentSecurityTests(unittest.TestCase):
    def test_render_preserves_public_feed_and_overwrites_trusted_header(self):
        template = SCRIPT.with_name("Caddyfile.example").read_text()
        generated = module.render(template, {"ADMIN_PROXY_SECRET": "a" * 64}, "$2a$14$" + "A" * 53)
        self.assertEqual(generated.count("basic_auth {"), 1)
        self.assertNotIn("REPLACE_WITH_", generated)
        self.assertIn('header_up X-Chineur-Admin "' + "a" * 64 + '"', generated)
        self.assertEqual(generated.count("header_up -X-Chineur-Admin"), 2)
        self.assertIn("request>headers delete", generated)
        self.assertIn("request>uri delete", generated)
        self.assertIn("resp_headers delete", generated)

    def test_private_feed_is_an_explicit_option(self):
        generated = module.render(SCRIPT.with_name("Caddyfile.example").read_text(),
                                  {"ADMIN_PROXY_SECRET": "a" * 64}, "$2a$14$" + "A" * 53,
                                  "$2a$14$" + "B" * 53, True)
        self.assertEqual(generated.count("basic_auth {"), 2)
        self.assertIn("visitor $2a$", generated)

    def test_invalid_secrets_or_hashes_never_reach_config(self):
        for values, password in [({}, "$2a$14$" + "A" * 53),
                                 ({"ADMIN_PROXY_SECRET": "a" * 64}, 'invalid"\nmalicious')]:
            with self.assertRaises(ValueError):
                module.render("", values, password)

    def test_secret_files_are_atomic_and_owner_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / ".env"
            module.write_private(target, "synthetic=first\n")
            module.write_private(target, "synthetic=second\n")
            self.assertEqual(target.read_text(), "synthetic=second\n")
            self.assertEqual(os.stat(target).st_mode & 0o777, 0o600)
            self.assertEqual(list(Path(tmp).glob(".security-*")), [])
