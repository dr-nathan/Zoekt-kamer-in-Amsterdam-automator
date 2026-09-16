from __future__ import annotations

import argparse
from pathlib import Path

from fb_automator.collector import FacebookCollector
from fb_automator.config import load_groups

DEFAULT_PROFILE = Path(".state/facebook-profile")
DEFAULT_DATABASE = Path("data/listings.db")
DEFAULT_GROUPS = Path("config/groups.json")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fb-housing", description="Collect housing posts from Facebook groups."
    )
    parser.add_argument("--profile-dir", type=Path, default=DEFAULT_PROFILE)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("login", help="Open Facebook and save a local login session.")

    collect = subparsers.add_parser("collect", help="Collect recent posts from configured groups.")
    collect.add_argument("--groups", type=Path, default=DEFAULT_GROUPS)
    collect.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    collect.add_argument("--max-posts", type=int, default=50)
    collect.add_argument("--max-scrolls", type=int, default=15)
    collect.add_argument("--scroll-pause", type=float, default=3.0)
    collect.add_argument(
        "--headless",
        action="store_true",
        help="Hide the browser. Use only after visible collection works reliably.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    collector = FacebookCollector(
        profile_dir=args.profile_dir,
        database=getattr(args, "database", DEFAULT_DATABASE),
        headless=getattr(args, "headless", False),
        scroll_pause=getattr(args, "scroll_pause", 3.0),
    )

    try:
        if args.command == "login":
            collector.login()
        elif args.command == "collect":
            if args.max_posts < 1 or args.max_scrolls < 0 or args.scroll_pause < 0:
                raise ValueError("Collection limits and pause must be non-negative.")
            collector.collect(
                load_groups(args.groups),
                max_posts=args.max_posts,
                max_scrolls=args.max_scrolls,
            )
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(f"error: {exc}") from exc


if __name__ == "__main__":
    main()
