#!/usr/bin/env python3
"""Scan git data, with redacted output and no traversal of runtime directories."""

from __future__ import annotations

import argparse
import fnmatch
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys


SENSITIVE_DIRECTORIES = {".state", ".venv", "venv", "data", "security-assessment"}
SENSITIVE_NAMES = {
    "groups.json",
    "storage-state.json",
    "storage_state.json",
    "id_rsa",
    "id_ed25519",
}
SENSITIVE_GLOBS = (
    "cookies*.json",
    "*.db",
    "*.db-*",
    "*.sqlite",
    "*.sqlite-*",
    "*.sqlite3",
    "*.sqlite3-*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.har",
    "*.log",
    "*.bak",
    "*.backup",
    "gitleaks-report*",
)


def is_sensitive_path(path: str) -> bool:
    parts = tuple(part.lower() for part in PurePosixPath(path).parts)
    if any(part in SENSITIVE_DIRECTORIES or part.startswith("security-assessment-") for part in parts):
        return True
    name = parts[-1] if parts else ""
    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        return True
    return name in SENSITIVE_NAMES or any(fnmatch.fnmatchcase(name, pattern) for pattern in SENSITIVE_GLOBS)


def tracked_sensitive_paths(repo: Path, staged: bool) -> list[str]:
    command = (
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"]
        if staged
        else ["git", "ls-files", "-z"]
    )
    result = subprocess.run(command, cwd=repo, check=True, stdout=subprocess.PIPE)
    return [
        os.fsdecode(path)
        for path in result.stdout.split(b"\0")
        if path and is_sensitive_path(os.fsdecode(path))
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true", help="scan staged changes instead of all reachable history")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[2]
    sensitive = tracked_sensitive_paths(repo, args.staged)
    if sensitive:
        # File names are quoted to prevent terminal control characters in git paths.
        for path in sensitive:
            print(f"Blocked runtime/private artifact in git: {ascii(path)}", file=sys.stderr)
        return 1

    executable = shutil.which("gitleaks")
    if not executable:
        print("Gitleaks is required. Install it from https://github.com/gitleaks/gitleaks/releases", file=sys.stderr)
        return 2
    command = [
        executable,
        "git",
        "--redact=100",
        "--no-banner",
        "--no-color",
        "--ignore-gitleaks-allow",
        "--gitleaks-ignore-path",
        str(repo / ".github" / "gitleaksignore"),
        "--config",
        str(repo / ".github" / "gitleaks.toml"),
        "--timeout=120",
    ]
    command.extend(["--staged"] if args.staged else ["--log-opts=--all"])
    command.append(str(repo))
    # No verbose output or report files: findings cannot be uploaded accidentally.
    return subprocess.run(command, cwd=repo, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
