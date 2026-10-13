#!/usr/bin/env python3
"""Download gzipped replay days from the public R2 base into REPLAY_CACHE."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_env() -> None:
    path = ROOT / ".env"
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def main() -> int:
    _load_env()
    sys.path.insert(0, str(ROOT / "src"))
    import httpx

    from iron_fly.replay_store import (
        bootstrap_days,
        default_public_base,
        default_replay_dir,
        download_day,
        fetch_manifest,
        select_days,
    )

    parser = argparse.ArgumentParser(description="Download SPX replay days from R2.")
    parser.add_argument("--last", type=int, metavar="N", help="newest N days (default: REPLAY_BOOTSTRAP_DAYS or 10)")
    parser.add_argument("--from", dest="day_from", type=date.fromisoformat, help="inclusive start YYYY-MM-DD")
    parser.add_argument("--to", dest="day_to", type=date.fromisoformat, help="inclusive end YYYY-MM-DD")
    parser.add_argument("--all", action="store_true", help="every day in the manifest")
    parser.add_argument("--replay", type=Path, help="unpack directory (default: REPLAY_CACHE or ./replay)")
    parser.add_argument("--base", default=None, help="public R2 base URL")
    parser.add_argument("--force", action="store_true", help="re-download even when sha matches")
    parser.add_argument("--continue-on-error", action="store_true")
    args = parser.parse_args()

    replay_dir = args.replay or default_replay_dir()
    replay_dir.mkdir(parents=True, exist_ok=True)
    base = args.base or default_public_base()
    last = args.last
    if not args.all and args.day_from is None and args.day_to is None and last is None:
        last = bootstrap_days()

    downloaded = skipped = 0
    errors: list[str] = []
    with httpx.Client(timeout=120.0) as client:
        try:
            manifest = fetch_manifest(base, client=client)
        except Exception as exc:
            print(f"manifest failed: {exc}", file=sys.stderr)
            return 1
        chosen = select_days(
            manifest,
            last=last,
            day_from=args.day_from,
            day_to=args.day_to,
            all_days=args.all,
        )
        if not chosen:
            print("no days selected")
            return 0
        print(f"days {chosen[0].date} .. {chosen[-1].date} ({len(chosen)}) → {replay_dir}")
        for item in chosen:
            try:
                status = download_day(
                    item,
                    replay_dir,
                    public_base=base,
                    client=client,
                    force=args.force,
                )
                if status == "skip":
                    skipped += 1
                    print(f"skip {item.date}")
                else:
                    downloaded += 1
                    print(f"get  {item.date}")
            except Exception as exc:
                errors.append(f"{item.date}: {exc}")
                print(f"fail {item.date}: {exc}", file=sys.stderr)
                if not args.continue_on_error:
                    return 1
    print(f"done downloaded={downloaded} skipped={skipped} errors={len(errors)}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
