#!/usr/bin/env python3
"""Gzip local replay/*.json and upload to Cloudflare R2; refresh manifest.json."""

from __future__ import annotations

import argparse
import os
import sys
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
    from iron_fly.replay_store import default_replay_dir, upload_days

    parser = argparse.ArgumentParser(description="Upload gzipped replay days to R2.")
    parser.add_argument("--last", type=int, metavar="N", help="only the newest N local days")
    parser.add_argument("--day", action="append", default=[], help="specific YYYY-MM-DD (repeatable)")
    parser.add_argument("--replay", type=Path, help="local replay directory")
    args = parser.parse_args()

    replay_dir = args.replay or default_replay_dir()
    if not replay_dir.is_dir():
        print(f"no replay dir {replay_dir}", file=sys.stderr)
        return 1

    days: list[str] | None = None
    if args.day:
        days = list(args.day)
    elif args.last is not None:
        stems = sorted(path.stem for path in replay_dir.glob("????-??-??.json"))
        days = stems[-args.last :] if args.last > 0 else []

    try:
        uploaded, skipped, manifest = upload_days(replay_dir, days=days)
    except Exception as exc:
        print(f"upload failed: {exc}", file=sys.stderr)
        return 1
    print(f"uploaded={uploaded} skipped={skipped} manifest_days={len(manifest.days)}")
    if manifest.days:
        print(f"range {manifest.days[0].date} .. {manifest.days[-1].date}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
