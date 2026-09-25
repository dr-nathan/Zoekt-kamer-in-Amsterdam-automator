from __future__ import annotations

import argparse
from pathlib import Path

from fb_automator.collector import FacebookCollector
from fb_automator.config import load_groups
from fb_automator.extractor import extract_database
from fb_automator.image_reviewer import review_database_images
from fb_automator.notifications import tracked_job

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
    subparsers.add_parser(
        "export-session",
        help="Export the saved Facebook login for secure transfer to another machine.",
    )

    collect = subparsers.add_parser("collect", help="Collect recent posts from configured groups.")
    collect.add_argument("--groups", type=Path, default=DEFAULT_GROUPS)
    collect.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    collect.add_argument("--max-posts", type=int, default=50)
    collect.add_argument("--max-scrolls", type=int, default=15)
    collect.add_argument(
        "--scroll-pause",
        type=float,
        default=3.0,
        help="Base seconds between scrolls (minimum: 2; default: 3).",
    )
    collect.add_argument(
        "--headless",
        action="store_true",
        help="Hide the browser. Use only after visible collection works reliably.",
    )

    extract = subparsers.add_parser(
        "extract", help="Extract filterable attributes from saved raw posts."
    )
    extract.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    extract.add_argument(
        "--model",
        help="OpenAI model (default: OPENAI_MODEL or gpt-5.4-mini).",
    )
    extract.add_argument(
        "--force",
        action="store_true",
        help="Re-extract posts even when their content is unchanged.",
    )
    extract.add_argument("--limit", type=int, help="Extract at most this many pending posts.")
    extract.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Concurrent API requests (default: 4).",
    )
    extract.add_argument(
        "--vision-model",
        help="Image-review model (default: OPENAI_VISION_MODEL or gpt-5.4-mini).",
    )
    extract.add_argument(
        "--skip-image-review",
        action="store_true",
        help="Skip cached cover-image selection after text extraction.",
    )

    digest = subparsers.add_parser(
        "digest", help="Send each verified subscriber one daily matched-listing digest."
    )
    digest.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    serve = subparsers.add_parser(
        "serve", help="Serve the private Chineur2000 web application."
    )
    serve.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "serve":
        if not 1 <= args.port <= 65535:
            raise SystemExit("error: --port must be between 1 and 65535")
        import uvicorn
        from fb_automator.web import create_app

        uvicorn.run(create_app(args.database.resolve()), host=args.host, port=args.port)
        return

    if args.command == "digest":
        from fb_automator.digest import send_daily_digests

        try:
            with tracked_job(args.database, "digest") as details:
                result = send_daily_digests(args.database)
                details.update(
                    {
                        "channels": result.channels,
                        "sent": result.sent,
                        "skipped": result.skipped,
                        "failed": result.failed,
                    }
                )
        except (RuntimeError, ValueError) as exc:
            raise SystemExit(f"error: {exc}") from exc
        print(
            f"channels={result.channels} sent={result.sent} "
            f"skipped={result.skipped} failed={result.failed}"
        )
        return

    if args.command == "extract":
        if args.limit is not None and args.limit < 1:
            raise SystemExit("error: --limit must be at least 1")
        if args.workers < 1:
            raise SystemExit("error: --workers must be at least 1")
        try:
            with tracked_job(args.database, "extract") as details:
                processed, cached, remaining, total = extract_database(
                    args.database,
                    model=args.model,
                    force=args.force,
                    limit=args.limit,
                    workers=args.workers,
                    progress=lambda done, count: print(f"extracting {done}/{count}"),
                )
                details.update(
                    {
                        "extracted": processed,
                        "cached": cached,
                        "pending": remaining,
                        "database_total": total,
                    }
                )
        except (RuntimeError, ValueError) as exc:
            raise SystemExit(f"error: {exc}") from exc
        print(
            f"extracted={processed} cached={cached} "
            f"pending={remaining} database_total={total}"
        )
        if not args.skip_image_review:
            try:
                with tracked_job(args.database, "image_review") as details:
                    reviewed, image_cached, image_remaining, image_total = (
                        review_database_images(
                            args.database,
                            model=args.vision_model,
                            limit=args.limit,
                            workers=args.workers,
                            progress=lambda done, count: print(
                                f"reviewing images {done}/{count}"
                            ),
                        )
                    )
                    details.update(
                        {
                            "reviewed": reviewed,
                            "cached": image_cached,
                            "pending": image_remaining,
                            "image_total": image_total,
                        }
                    )
            except (RuntimeError, ValueError) as exc:
                raise SystemExit(f"error: image review failed: {exc}") from exc
            print(
                f"images_reviewed={reviewed} image_cached={image_cached} "
                f"image_pending={image_remaining} image_total={image_total}"
            )
        return

    collector = FacebookCollector(
        profile_dir=args.profile_dir,
        database=getattr(args, "database", DEFAULT_DATABASE),
        headless=getattr(args, "headless", False),
        scroll_pause=getattr(args, "scroll_pause", 3.0),
    )

    try:
        if args.command == "login":
            collector.login()
        elif args.command == "export-session":
            collector.export_session()
        elif args.command == "collect":
            if args.max_posts < 1 or args.max_scrolls < 0:
                raise ValueError("Collection limits must be non-negative.")
            if args.scroll_pause < 2:
                raise ValueError("Scroll pause must be at least 2 seconds.")
            with tracked_job(args.database, "collect") as details:
                result = collector.collect(
                    load_groups(args.groups),
                    max_posts=args.max_posts,
                    max_scrolls=args.max_scrolls,
                )
                details["result"] = str(result)
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(f"error: {exc}") from exc


if __name__ == "__main__":
    main()
