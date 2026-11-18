#!/usr/bin/env python3
"""Activate a prepared release with private backups and automatic rollback."""
from __future__ import annotations

import argparse
import fcntl
from datetime import datetime, timezone
import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import sqlite3
import subprocess
import time
import urllib.request
from contextlib import closing


def verify_release_files(release: Path, files: dict[str, str]) -> None:
    for name, expected in files.items():
        target = (release / name).resolve()
        if not target.is_relative_to(release) or hashlib.sha256(target.read_bytes()).hexdigest() != expected:
            raise SystemExit("Release files no longer match the reviewed manifest; prepare the release again.")


def wait_for_background_jobs(wait_seconds: int) -> None:
    """Let existing jobs finish without terminating them or changing configuration."""
    deadline = time.monotonic() + wait_seconds
    next_notice = 0.0
    units = ("facebookrooms-collect.service", "facebookrooms-digest.service")
    while True:
        busy = []
        for unit in units:
            result = subprocess.run(
                ["systemctl", "show", "--property=ActiveState,Job", unit],
                capture_output=True, text=True, check=True,
            )
            properties = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
            state = properties.get("ActiveState")
            job = properties.get("Job")
            # systemctl prints an absent job as "Job=" on the production
            # version; other versions print the numeric zero identifier.
            if (job != "" and (job is None or not job.isascii() or not job.isdecimal())) or state not in {
                "active", "activating", "reloading", "deactivating", "refreshing", "inactive", "failed"
            }:
                raise RuntimeError("Cannot determine whether background jobs are idle. Application configuration was not changed.")
            if state not in {"inactive", "failed"} or job not in {"", "0"}:
                busy.append(unit)
        if not busy:
            return
        now = time.monotonic()
        if now >= deadline:
            raise RuntimeError("Timed out waiting for background jobs. Application configuration was not changed; scheduled jobs will be restored.")
        if now >= next_notice:
            print("Waiting for background jobs to finish: " + ", ".join(busy) +
                  f" (up to {int(deadline - now)} seconds remaining). New scheduled jobs are paused.", flush=True)
            next_notice = now + 60
        time.sleep(min(5, deadline - now))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-dir", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--private-feed", action="store_true")
    parser.add_argument("--wait-seconds", type=int, default=2700,
                        help="Wait up to this many seconds for running background jobs (default: 2700).")
    args = parser.parse_args()
    if args.wait_seconds < 0:
        parser.error("--wait-seconds must be zero or greater")
    if os.geteuid() != 0:
        raise SystemExit("Administrator privileges are required. Run this reviewed script with sudo.")
    os.umask(0o077)
    deployment_lock = open("/run/lock/facebookrooms-security-release.lock", "a")
    try:
        fcntl.flock(deployment_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("Another deployment is already running.") from None
    release = Path(__file__).resolve().parent.parent
    app = args.app_dir.resolve()
    data = args.data_dir.resolve()
    for path in (release, app, data):
        if any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./-" for c in str(path)):
            raise SystemExit("Deployment paths must not contain whitespace or unit-file metacharacters.")
    binary = release / ".venv/bin/fb-housing"
    if not binary.is_file() or not (release / "requirements.lock").is_file():
        raise SystemExit("Prepare and test the release virtualenv before activation.")
    manifest = json.loads((release / "release-manifest.json").read_text())
    verify_release_files(release, manifest["files"])
    owner = pwd.getpwuid(app.stat().st_uid)
    if owner.pw_uid == 0:
        raise SystemExit("The application must run as an unprivileged owner.")
    backup = Path("/var/backups/facebookrooms") / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup.mkdir(parents=True, mode=0o700, exist_ok=False)
    backup.chmod(0o700)

    def run(command: list[str]) -> None:
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode:
            (backup / "last-command-error.txt").write_text(result.stderr)
            raise RuntimeError(f"{command[0]} failed; private diagnostics are in {backup}.")

    env = app / ".env"
    caddy = Path("/etc/caddy/Caddyfile")
    database = data / "listings.db"
    dropins: dict[Path, bytes | None] = {}
    operations = {
        "facebookrooms.service": [f"{binary} serve --database {database} --host 127.0.0.1 --port 8000"],
        "facebookrooms-collect.service": [
            f"{binary} collect --groups {app}/config/groups.json --database {database} --max-posts 20 --max-scrolls 6 --scroll-pause 5 --headless",
            f"{binary} extract --database {database} --workers 2",
        ],
        "facebookrooms-digest.service": [f"{binary} digest --database {database}"],
    }
    modified = False
    timer_names = ["facebookrooms-collect.timer", "facebookrooms-digest.timer"]
    active_timers = [name for name in timer_names if subprocess.run(["systemctl", "is-active", "--quiet", name], capture_output=True).returncode == 0]
    try:
        run(["systemctl", "stop", *timer_names])
        wait_for_background_jobs(args.wait_seconds)
        # Snapshot only after existing jobs finish so a long wait does not
        # leave the deployment with stale backups of their database changes.
        env_stat = env.stat()
        shutil.copy2(env, backup / "environment")
        caddy_stat = caddy.stat()
        shutil.copy2(caddy, backup / "Caddyfile")
        with urllib.request.urlopen("http://127.0.0.1:2019/config/", timeout=5) as response:
            (backup / "active-caddy.json").write_bytes(response.read())
        with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(backup / "listings.db")) as destination:
                source.backup(destination)
        for unit in operations:
            target = Path("/etc/systemd/system") / (unit + ".d") / "security-release.conf"
            dropins[target] = target.read_bytes() if target.exists() else None
        (backup / "dropins.json").write_text(json.dumps({str(k):v.decode() if v is not None else None for k,v in dropins.items()}))
        verify_release_files(release, manifest["files"])
        modified = True
        command = ["python3", str(release / "deploy/configure-production.py"), "--app-dir", str(app), "--output", str(backup / "candidate.Caddyfile")]
        if args.private_feed:
            command.append("--private-feed")
        run(command)
        os.chown(env, env_stat.st_uid, env_stat.st_gid)
        logdir = Path("/var/log/caddy")
        if not logdir.exists():
            logdir.mkdir(mode=0o750)
            os.chown(logdir, pwd.getpwnam("caddy").pw_uid, grp.getgrnam("caddy").gr_gid)
        run(["caddy", "validate", "--config", str(backup / "candidate.Caddyfile"), "--adapter", "caddyfile"])
        # Validation may provision the file logger as root; hand it to Caddy
        # before the running unprivileged service opens it during reload.
        logfile = logdir / "facebookrooms-access.json"
        if logfile.exists():
            os.chown(logfile, pwd.getpwnam("caddy").pw_uid, grp.getgrnam("caddy").gr_gid)
            logfile.chmod(0o640)
        for unit, commands in operations.items():
            target = Path("/etc/systemd/system") / (unit + ".d") / "security-release.conf"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("[Service]\nExecStart=\n" + "".join(f"ExecStart={command}\n" for command in commands) +
                              f"Environment=PLAYWRIGHT_BROWSERS_PATH={release}/browsers\n" +
                              "UMask=0077\nRestrictSUIDSGID=true\nProtectKernelTunables=true\nProtectKernelModules=true\nProtectControlGroups=true\n")
            target.chmod(0o644)
        shutil.copyfile(backup / "candidate.Caddyfile", caddy)
        os.chown(caddy, caddy_stat.st_uid, grp.getgrnam("caddy").gr_gid)
        caddy.chmod(0o640)
        run(["systemctl", "daemon-reload"])
        run(["systemctl", "restart", "facebookrooms.service"])
        for attempt in range(20):
            try:
                request = urllib.request.Request("http://127.0.0.1:8000/health", headers={"Host": "facebookrooms.nl"})
                with urllib.request.urlopen(request, timeout=2) as response:
                    healthy = json.load(response) == {"status": "ok"}
                if healthy:
                    break
            except (OSError, ValueError):
                pass
            time.sleep(.5)
        else:
            raise RuntimeError("New application did not become healthy.")
        run(["caddy", "reload", "--config", str(caddy), "--adapter", "caddyfile"])
        run(["systemctl", "is-active", "--quiet", "facebookrooms.service", "caddy.service"])
        print("Security release activated. Private rollback backups: " + str(backup))
    except BaseException:
        if modified:
            shutil.copyfile(backup / "environment", env)
            os.chown(env, env_stat.st_uid, env_stat.st_gid)
            env.chmod(env_stat.st_mode & 0o777)
            shutil.copyfile(backup / "Caddyfile", caddy)
            os.chown(caddy, caddy_stat.st_uid, caddy_stat.st_gid)
            caddy.chmod(caddy_stat.st_mode & 0o777)
            for target, prior in dropins.items():
                if prior is None:
                    target.unlink(missing_ok=True)
                else:
                    target.write_bytes(prior)
                    target.chmod(0o644)
            recovery = [subprocess.run(command, capture_output=True).returncode for command in [
                ["systemctl", "daemon-reload"],
                ["systemctl", "restart", "facebookrooms.service"],
                ["caddy", "reload", "--config", str(backup / "active-caddy.json")],
            ]]
            if all(code == 0 for code in recovery):
                print("Activation failed; previous application configuration was restored. Database contents were not rolled back.")
            else:
                print("Activation and automatic recovery failed; administrator recovery is required. Private backups: " + str(backup))
        raise
    finally:
        if active_timers:
            result = subprocess.run(["systemctl", "start", *active_timers], capture_output=True)
            if result.returncode:
                raise RuntimeError("Scheduled jobs could not be restored; administrator action is required.")


if __name__ == "__main__":
    main()
