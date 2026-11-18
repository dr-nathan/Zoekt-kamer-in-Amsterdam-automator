#!/usr/bin/env python3
"""Render production proxy configuration without secrets in command arguments."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import secrets
import tempfile


def load_environment(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text().splitlines() if path.exists() else ():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def write_private(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    old = path.stat() if path.exists() else None
    descriptor, name = tempfile.mkstemp(prefix=".security-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if old is not None and os.geteuid() == 0:
            os.chown(name, old.st_uid, old.st_gid)
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def render(template: str, values: dict[str, str], admin_hash: str, site_hash: str = "", private_feed: bool = False) -> str:
    proxy_secret = values.get("ADMIN_PROXY_SECRET", "")
    if re.fullmatch(r"[0-9a-f]{64}", proxy_secret) is None:
        raise ValueError("ADMIN_PROXY_SECRET must be a generated 32-byte hex secret.")
    if not re.fullmatch(r"\$2[aby]\$\d\d\$[./A-Za-z0-9]{53}", admin_hash):
        raise ValueError("A valid existing administrator password hash is required.")
    public_auth = "# Public listing feed; administration remains authenticated."
    if private_feed:
        if not re.fullmatch(r"\$2[aby]\$\d\d\$[./A-Za-z0-9]{53}", site_hash):
            raise ValueError("A valid visitor password hash is required for a private feed.")
        public_auth = "basic_auth {\n\t\t\tvisitor " + site_hash + "\n\t\t}"
    return (template.replace("REPLACE_WITH_ADMIN_PASSWORD_HASH", admin_hash)
            .replace("REPLACE_WITH_ADMIN_PROXY_SECRET", proxy_secret)
            .replace("REPLACE_WITH_PUBLIC_AUTH", public_auth))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--private-feed", action="store_true")
    args = parser.parse_args()
    app = args.app_dir.resolve()
    env_path = app / ".env"
    values = load_environment(env_path)
    app_secret = values.get("APP_SECRET", "")
    if len(app_secret.encode()) < 32 or not app_secret.strip():
        raise SystemExit("APP_SECRET must already contain at least 32 random bytes; refusing to invalidate existing links automatically.")
    if re.fullmatch(r"[0-9a-f]{64}", values.get("ADMIN_PROXY_SECRET", "")) is None:
        values["ADMIN_PROXY_SECRET"] = secrets.token_hex(32)
    values["FB_ALLOWED_HOSTS"] = "facebookrooms.nl,www.facebookrooms.nl,localhost,127.0.0.1"
    parent = app.parent
    admin_hash = (parent / ".facebookrooms-admin-password-hash").read_text().strip()
    site_file = parent / ".facebookrooms-password-hash"
    site_hash = site_file.read_text().strip() if args.private_feed else ""
    template = Path(__file__).with_name("Caddyfile.example").read_text()
    config = render(template, values, admin_hash, site_hash, args.private_feed)
    write_private(env_path, "".join(f"{key}={value}\n" for key, value in values.items()))
    write_private(args.output, config)
    print("Production configuration prepared; secret values were not displayed.")


if __name__ == "__main__":
    main()
