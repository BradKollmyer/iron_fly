#!/usr/bin/env python3
"""Write replay/YYYY-MM-DD.json for the image bake.

Laptop driver: skip dense local days, bake missing ones inside a one-shot
container on the mini Docker network `data` (psycopg → postgres, HTTP →
collector), then `docker cp` only the finished JSON home.

Worker (`--worker`): SPX via `day_spx_tape`, chains from `BAKE_DATABASE_URL`.
Nothing here is imported by the public desk.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
KEEP = int(os.environ.get("BAKE_DAYS", "365"))
_ET = ZoneInfo("America/New_York")
_DOCKER_CONTEXT = os.environ.get("BAKE_DOCKER_CONTEXT", "mac-mini")
_DOCKER_NETWORK = os.environ.get("BAKE_DOCKER_NETWORK", "data")
_BAKE_IMAGE = os.environ.get("BAKE_IMAGE", "iron-fly-bake:local")
_WORKER_STREAMER = os.environ.get("BAKE_WORKER_STREAMER_URL", "http://collector:8080")
_CHAIN_SQL = (
    "SELECT EXTRACT(EPOCH FROM ts)::bigint, strike, call_bid, call_ask, put_bid, put_ask "
    "FROM option_chain_samples "
    "WHERE expiration = %s "
    "AND ts >= %s::timestamptz "
    "AND ts <= %s::timestamptz "
    "ORDER BY ts, strike"
)


def _load_env() -> None:
    path = ROOT / ".env"
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    logging.getLogger("iron_fly.quotes.collector").setLevel(logging.INFO)
    logging.getLogger("iron_fly.quotes.replay").setLevel(logging.INFO)
    _load_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true", help="bake inside the container on network data")
    parser.add_argument("--out", type=Path, default=None, help="output directory (worker default /out)")
    parser.add_argument("--days", nargs="+", default=[], help="YYYY-MM-DD to bake (worker; host uses streamer days)")
    args = parser.parse_args(argv)
    sys.path.insert(0, str(ROOT / "src"))
    if args.worker:
        return _worker_main(args.out or Path("/out"), args.days)
    return _host_main()


def _host_main() -> int:
    from iron_fly.quotes.collector import CollectorClient
    from iron_fly.quotes.replay import (
        _CACHE_VERSION,
        _read_payload,
        chains_keep_each_print,
        tape_keeps_each_second,
    )

    url = os.environ.get("MARKET_STREAMER_URL", "").strip()
    if not url:
        print("MARKET_STREAMER_URL is not set", file=sys.stderr)
        return 1
    dsn = os.environ.get("BAKE_DATABASE_URL", "").strip()
    if not dsn:
        print("BAKE_DATABASE_URL is not set (postgres on the mini data network)", file=sys.stderr)
        return 1
    token = os.environ.get("MARKET_STREAMER_TOKEN", "").strip()
    dest = ROOT / "replay"
    dest.mkdir(parents=True, exist_ok=True)
    client = CollectorClient(url, token)
    days = client.session_days()
    if KEEP > 0:
        days = days[-KEEP:]
    if not days:
        print("no session days", file=sys.stderr)
        return 1
    print(f"days {days[0]} .. {days[-1]} ({len(days)})")
    keep = set(days)
    for path in sorted(dest.glob("????-??-??.json")):
        if path.stem in keep:
            continue
        path.unlink()
        print(f"drop {path.stem}")
    missing: list[str] = []
    for item in days:
        path = dest / f"{item}.json"
        payload = _read_payload(path)
        chains = payload.get("chains") if isinstance(payload, dict) else None
        tape = payload.get("tape") if isinstance(payload, dict) else None
        dense = tape_keeps_each_second(tape) and chains_keep_each_print(chains)
        if (
            isinstance(payload, dict)
            and payload.get("version") == _CACHE_VERSION
            and isinstance(chains, dict)
            and chains
            and dense
        ):
            print(f"skip {item} tape={len(payload.get('tape') or [])} chains={len(chains)}")
            continue
        print(f"bake {item}", flush=True)
        missing.append(item)
    if not missing:
        return 0
    if error := _ensure_bake_image():
        print(error, file=sys.stderr)
        return 1
    return _remote_bake(missing, dest, dsn, token)


def _worker_main(out: Path, day_texts: list[str]) -> int:
    from iron_fly.quotes.collector import CollectorClient

    if not day_texts:
        print("worker needs --days YYYY-MM-DD …", file=sys.stderr)
        return 1
    dsn = os.environ.get("BAKE_DATABASE_URL", "").strip()
    if not dsn:
        print("BAKE_DATABASE_URL is not set", file=sys.stderr)
        return 1
    url = os.environ.get("MARKET_STREAMER_URL", "").strip() or _WORKER_STREAMER
    token = os.environ.get("MARKET_STREAMER_TOKEN", "").strip()
    out.mkdir(parents=True, exist_ok=True)
    client = CollectorClient(url, token)
    failed = 0
    for text in day_texts:
        day = date.fromisoformat(text)
        print(f"bake {text}", flush=True)
        error = bake_day(day, out, client, chain_records_db(day, dsn), dsn=dsn)
        path = out / f"{text}.json"
        if error or not path.is_file():
            failed += 1
            print(f"fail {text} {error or 'file missing'}", file=sys.stderr)
            continue
        print(f"wrote {text} bytes={path.stat().st_size}", flush=True)
    return 1 if failed else 0


def bake_day(day: date, dest: Path, client, records, *, dsn: str | None = None) -> str | None:
    """Write one replay file. `records` yields chain tuples. None means success."""
    from iron_fly.domain.market import DeskMarks
    from iron_fly.quotes.replay import apply_chain_records, write_day_cache

    marks = client.day_spx_tape(day)
    if marks.error or not marks.tape:
        return marks.error or "no SPX tape"
    tape = marks.tape
    em_open = marks.em_open
    if em_open is None and dsn:
        em_open = opening_em_db(day, dsn)
    book = DeskMarks(source="market_streamer", tape=tape, em_open=em_open)
    book.last = float(tape[0]["value"])
    book.ts = datetime.fromtimestamp(int(tape[0]["time"]), tz=_ET)
    count = apply_chain_records(day, book, records)
    if count == 0:
        return "chains missing"
    write_day_cache(dest, day, book)
    return None


def opening_em_db(day: date, dsn: str) -> float | None:
    import psycopg

    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT opening_em FROM session_state WHERE session_date = %s",
            (day,),
        ).fetchone()
    if not row or row[0] is None:
        return None
    return float(row[0])


def chain_records_db(day: date, dsn: str):
    """Yield (stamp, strike, call_bid, call_ask, put_bid, put_ask) from Postgres on the data network."""
    import psycopg

    start = f"{day.isoformat()} 09:30:00-04"
    end = f"{day.isoformat()} 16:16:00-04"
    with psycopg.connect(dsn) as conn:
        with conn.cursor(name=f"bake_chains_{day.isoformat().replace('-', '')}") as cur:
            cur.itersize = 20_000
            cur.execute(_CHAIN_SQL, (day, start, end))
            for row in cur:
                stamp, strike, call_bid, call_ask, put_bid, put_ask = row
                yield (
                    int(stamp),
                    float(strike) if strike is not None else None,
                    float(call_bid) if call_bid is not None else None,
                    float(call_ask) if call_ask is not None else None,
                    float(put_bid) if put_bid is not None else None,
                    float(put_ask) if put_ask is not None else None,
                )


def _ensure_bake_image() -> str | None:
    inspect = subprocess.run(
        ["docker", "--context", _DOCKER_CONTEXT, "image", "inspect", _BAKE_IMAGE],
        capture_output=True,
        text=True,
    )
    if inspect.returncode == 0 and os.environ.get("BAKE_REBUILD", "").strip() not in {"1", "true", "yes"}:
        return None
    print(f"build {_BAKE_IMAGE} on {_DOCKER_CONTEXT}", flush=True)
    built = subprocess.run(
        [
            "docker",
            "--context",
            _DOCKER_CONTEXT,
            "build",
            "-f",
            "Dockerfile.bake",
            "-t",
            _BAKE_IMAGE,
            ".",
        ],
        cwd=ROOT,
    )
    if built.returncode != 0:
        return f"failed to build {_BAKE_IMAGE}"
    return None


def remote_run_cmd(name: str, days: list[str], dsn: str, token: str) -> list[str]:
    """Docker run argv for the bake worker (no docker exec / \\copy)."""
    cmd = [
        "docker",
        "--context",
        _DOCKER_CONTEXT,
        "run",
        "--name",
        name,
        "--network",
        _DOCKER_NETWORK,
        "-e",
        f"BAKE_DATABASE_URL={dsn}",
        "-e",
        f"MARKET_STREAMER_URL={_WORKER_STREAMER}",
        "-e",
        f"MARKET_STREAMER_TOKEN={token}",
        _BAKE_IMAGE,
        "--worker",
        "--out",
        "/out",
        "--days",
        *days,
    ]
    return cmd


def _remote_bake(days: list[str], dest: Path, dsn: str, token: str) -> int:
    name = f"iron-fly-bake-{int(time.time())}"
    cmd = remote_run_cmd(name, days, dsn, token)
    print(f"remote {' '.join(days)} via {_BAKE_IMAGE}", flush=True)
    try:
        ran = subprocess.run(cmd)
        if ran.returncode != 0:
            print(f"fail remote bake exit={ran.returncode}", file=sys.stderr)
            # still try to copy any files that landed
        failed = 0
        for item in days:
            target = dest / f"{item}.json"
            copied = subprocess.run(
                ["docker", "--context", _DOCKER_CONTEXT, "cp", f"{name}:/out/{item}.json", str(target)],
                capture_output=True,
                text=True,
            )
            if copied.returncode != 0 or not target.is_file():
                failed += 1
                detail = (copied.stderr or copied.stdout or "docker cp failed").strip()
                print(f"fail {item} {detail}", file=sys.stderr)
                continue
            from iron_fly.quotes.replay import _read_payload

            payload = _read_payload(target)
            chains = payload.get("chains") if isinstance(payload, dict) else None
            tape = payload.get("tape") if isinstance(payload, dict) else None
            if not isinstance(chains, dict) or not chains:
                failed += 1
                print(f"fail {item} chains missing after copy", file=sys.stderr)
                continue
            print(f"wrote {item} tape={len(tape or [])} chains={len(chains)} bytes={target.stat().st_size}")
        return 1 if failed or ran.returncode != 0 else 0
    finally:
        subprocess.run(
            ["docker", "--context", _DOCKER_CONTEXT, "rm", "-f", name],
            capture_output=True,
            text=True,
        )


if __name__ == "__main__":
    raise SystemExit(main())
