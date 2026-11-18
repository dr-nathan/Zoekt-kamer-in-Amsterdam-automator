from __future__ import annotations

import importlib.util
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "deploy/configure-production.py"
spec = importlib.util.spec_from_file_location("production_config", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
activation_spec = importlib.util.spec_from_file_location("security_activation", SCRIPT.with_name("apply-security-release.py"))
activation = importlib.util.module_from_spec(activation_spec)
activation_spec.loader.exec_module(activation)


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


class BackgroundJobWaitTests(unittest.TestCase):
    @staticmethod
    def status(state="inactive", job=""):
        return subprocess.CompletedProcess([], 0, f"ActiveState={state}\nJob={job}\n", "")

    def test_active_oneshot_is_allowed_to_finish(self):
        with patch.object(activation.subprocess, "run", side_effect=[
            self.status("activating", 12), self.status(), self.status(), self.status("failed", 0),
        ]) as run, patch.object(activation.time, "monotonic", side_effect=[100, 100]), \
                patch.object(activation.time, "sleep") as sleep, patch("builtins.print"):
            activation.wait_for_background_jobs(30)
        sleep.assert_called_once_with(5)
        self.assertEqual(run.call_count, 4)
        self.assertTrue(all(call.args[0][:2] == ["systemctl", "show"] for call in run.call_args_list))

    def test_queued_job_is_not_mistaken_for_idle(self):
        with patch.object(activation.subprocess, "run", side_effect=[
            self.status(job=12), self.status(), self.status(), self.status(),
        ]), patch.object(activation.time, "monotonic", side_effect=[100, 100]), \
                patch.object(activation.time, "sleep") as sleep, patch("builtins.print"):
            activation.wait_for_background_jobs(30)
        sleep.assert_called_once_with(5)

    def test_busy_job_times_out_without_stopping_it(self):
        with patch.object(activation.subprocess, "run", side_effect=[
            self.status("activating", 12), self.status(),
        ]) as run, patch.object(activation.time, "monotonic", side_effect=[100, 130]), \
                patch.object(activation.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "Application configuration was not changed"):
                activation.wait_for_background_jobs(30)
        sleep.assert_not_called()
        self.assertTrue(all(call.args[0][1] == "show" for call in run.call_args_list))

    def test_wait_is_bounded_by_remaining_time(self):
        with patch.object(activation.subprocess, "run", side_effect=[
            self.status("activating", 12), self.status(), self.status(), self.status(),
        ]), patch.object(activation.time, "monotonic", side_effect=[100, 128]), \
                patch.object(activation.time, "sleep") as sleep, patch("builtins.print"):
            activation.wait_for_background_jobs(30)
        sleep.assert_called_once_with(2)

    def test_unknown_status_fails_closed(self):
        for result in [self.status("unknown"), self.status(job="unknown"),
                       subprocess.CompletedProcess([], 0, "ActiveState=inactive\n", ""),
                       subprocess.CompletedProcess([], 0, "", "")]:
            with self.subTest(result=result.stdout), patch.object(activation.subprocess, "run", return_value=result), \
                    patch.object(activation.time, "sleep") as sleep:
                with self.assertRaisesRegex(RuntimeError, "Cannot determine"):
                    activation.wait_for_background_jobs(30)
            sleep.assert_not_called()

    def test_systemd_query_failure_is_not_treated_as_idle(self):
        with patch.object(activation.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "systemctl")), \
                patch.object(activation.time, "sleep") as sleep:
            with self.assertRaises(subprocess.CalledProcessError):
                activation.wait_for_background_jobs(30)
        sleep.assert_not_called()


class ReleaseIntegrityTests(unittest.TestCase):
    def test_changed_release_file_is_rejected_on_recheck(self):
        with tempfile.TemporaryDirectory() as tmp:
            release = Path(tmp).resolve()
            helper = release / "helper.py"
            helper.write_bytes(b"reviewed")
            manifest = {"helper.py": hashlib.sha256(b"reviewed").hexdigest()}
            activation.verify_release_files(release, manifest)
            helper.write_bytes(b"changed during wait")
            with self.assertRaisesRegex(SystemExit, "no longer match"):
                activation.verify_release_files(release, manifest)

    def test_manifest_cannot_escape_release_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            release = Path(tmp).resolve() / "release"
            release.mkdir()
            (release.parent / "outside.py").write_bytes(b"outside")
            with self.assertRaisesRegex(SystemExit, "no longer match"):
                activation.verify_release_files(release, {"../outside.py": hashlib.sha256(b"outside").hexdigest()})
