"""Run one desk YAML over baked replay days.

    .venv/bin/python scripts/backtest.py config/default.yaml --day 2026-10-09
    .venv/bin/python scripts/backtest.py config/default.yaml --last 10
    .venv/bin/python scripts/backtest.py config/1545-20.yaml --last 30 --skip 2026-09-30,2026-10-05
    .venv/bin/python scripts/backtest.py config.yaml

The file is the same YAML as the Configuration box. A `date` field chooses
the session when `--day` is omitted. `--last N` runs the N newest files in
replay/, or every file when fewer than N are baked, and prints P&L by day.
`--skip` is a comma-separated list of YYYY-MM-DD sessions left out of that
run. Marks come from replay/.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
from dataclasses import dataclass, replace
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from cryptography.fernet import Fernet

from iron_fly.config import Settings
from iron_fly.db import connect, migrate
from iron_fly.desk import apply_spec, load_day_tape, replay_until, set_switches, watch_frame
from iron_fly.quotes.replay import baked_days
from iron_fly.strategy.spec import parse_spec

_ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class DayResult:
    day: date
    report: str
    pnl: float | None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backtest a desk YAML against baked replay days.")
    parser.add_argument("config", type=Path, help="desk YAML, the same file as the Configuration box")
    parser.add_argument("--day", action="append", default=[], help="session date YYYY-MM-DD; repeat to run several")
    parser.add_argument(
        "--last", type=int, metavar="N", help="run the N newest replay files, or every file if fewer exist"
    )
    parser.add_argument(
        "--skip",
        default="",
        metavar="DATES",
        help="comma-separated YYYY-MM-DD sessions to leave out, e.g. 2026-09-30,2026-10-05",
    )
    parser.add_argument("--replay", type=Path, help="replay directory (default: the repo replay/ folder)")
    parser.add_argument("--hydrate", action="store_true", help="download newest REPLAY_BOOTSTRAP_DAYS from R2 first")
    parser.add_argument("--verbose", action="store_true", help="print each strategy step")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(message)s")
    if args.last is not None and args.day:
        print("pass --day or --last, not both", file=sys.stderr)
        return 1
    if args.hydrate:
        from iron_fly.app import hydrate_replay_cache

        if args.replay is not None:
            os.environ["REPLAY_CACHE"] = str(args.replay)
        hydrate_replay_cache(required=False)
    try:
        text = args.config.read_text()
        days = _days(text, args.day, args.last, args.replay, skip=args.skip)
        if args.last is not None:
            _print_pnl(text, days, replay=args.replay)
        else:
            print("\n".join(run(text, days, replay=args.replay)))
    except (OSError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


def run(text: str, days: list[date], *, replay: Path | None = None) -> list[str]:
    """Walk each day from the open to the last stored print and return one report per day."""
    return [item.report for item in _walk(text, days, replay=replay)]


def _walk(text: str, days: list[date], *, replay: Path | None = None):
    spec = parse_spec(text)
    if replay is not None:
        os.environ["REPLAY_CACHE"] = str(replay)
    for day in days:
        with tempfile.TemporaryDirectory() as folder:
            settings = _settings(Path(folder))
            conn = connect(settings.data_dir / "backtest.db")
            try:
                migrate(conn)
                set_switches(conn, True, True, True)
                apply_spec(conn, spec)
                conn.commit()
                end = datetime.combine(day, time(23, 59), tzinfo=_ET)
                replay_until(conn, settings, day, end)
                conn.commit()
                marks = load_day_tape(settings, day)
                if marks.error or not marks.tape:
                    raise ValueError(marks.error or f"No baked tape for {day.isoformat()}")
                last = marks.tape[-1]
                marks = replace(
                    marks,
                    ts=datetime.fromtimestamp(int(last["time"]), tz=_ET),
                    last=float(last["value"]),
                )
                frame = watch_frame(conn, settings, marks)
                settled = marks.ts is not None and marks.ts.astimezone(_ET).time() >= time(16, 0)
            finally:
                conn.close()
        pnl = frame.get("pnl")
        yield DayResult(
            day,
            _report(day, spec.summary(), frame, settled=settled),
            float(pnl) if isinstance(pnl, (int, float)) else None,
        )


def _print_pnl(text: str, days: list[date], *, replay: Path | None) -> None:
    print(parse_spec(text).summary())
    total = 0
    counted = False
    for item in _walk(text, days, replay=replay):
        print(f"{item.day.isoformat():<12}{_money(item.pnl)}", flush=True)
        amount = _dollar_amount(item.pnl)
        if amount is not None:
            total += amount
            counted = True
    shown = f"{total:+12,.0f}" if counted else f"{'—':>12}"
    print(f"{'Total':<12}{shown}")


def _replay_dates(replay: Path | None) -> list[date]:
    """Baked session dates, oldest first. Filenames only, so the files stay unread."""
    if replay is not None:
        directories = [replay]
    else:
        from iron_fly.quotes.replay import _shipped_replay_dirs

        directories = _shipped_replay_dirs()
    found: set[date] = set()
    for directory in directories:
        if not directory.is_dir():
            continue
        for path in directory.glob("????-??-??.json"):
            try:
                found.add(date.fromisoformat(path.stem))
            except ValueError:
                continue
    return sorted(found)


def _days(
    text: str,
    requested: list[str],
    last: int | None,
    replay: Path | None,
    *,
    skip: str = "",
) -> list[date]:
    if last is not None:
        if last < 1:
            raise ValueError("--last must be at least 1")
        available = _replay_dates(replay)
        if not available:
            where = replay if replay is not None else Path("replay")
            raise ValueError(f"no replay files in {where}")
        chosen = available[-last:]
    elif requested:
        chosen = [date.fromisoformat(item) for item in requested]
    else:
        raw = yaml.safe_load(text or "")
        found = raw.get("date") if isinstance(raw, dict) else None
        if found in (None, ""):
            names = ", ".join(baked_days()) or "none"
            raise ValueError(f"pass --day YYYY-MM-DD, --last N, or set date in the config. Baked days: {names}")
        chosen = [date.fromisoformat(str(found))]
    return _without_skipped(chosen, skip)


def _without_skipped(days: list[date], skip: str) -> list[date]:
    skipped = _skip_dates(skip)
    if not skipped:
        return days
    kept = [day for day in days if day not in skipped]
    if not kept:
        listed = ", ".join(day.isoformat() for day in days)
        raise ValueError(f"--skip leaves no sessions (selected {listed})")
    return kept


def _skip_dates(raw: str) -> set[date]:
    found: set[date] = set()
    for item in raw.split(","):
        text = item.strip()
        if not text:
            continue
        try:
            found.add(date.fromisoformat(text))
        except ValueError:
            raise ValueError(f"--skip dates must be YYYY-MM-DD, got {text}") from None
    return found


def _settings(data_dir: Path) -> Settings:
    return Settings.from_values(
        app_base_url="http://localhost:8001",
        session_secret="cli",
        secrets_key=Fernet.generate_key().decode(),
        data_dir=data_dir,
        desk_mode="backtest-public",
    )


def _report(day: date, summary: str, frame: dict, *, settled: bool) -> str:
    lines = [f"{day.isoformat()}  {summary}"]
    flies = frame.get("flies") or []
    if not flies:
        lines.append("  no flies")
    for fly in flies:
        close = fly.get("time_close") or ("16:00" if settled and fly.get("status") == "active" else "open")
        strike = int(fly["strike"]) if fly.get("strike") is not None else "—"
        sto = "—" if fly.get("sto") is None else f"{float(fly['sto']):.2f}"
        lines.append(
            f"  {fly.get('time_open') or '—':<5} {close:<5}  {strike}  wings {fly.get('wings')}  "
            f"sto {sto}  pnl {_dollars(fly.get('pnl'))}"
        )
    lines.append(f"P&L {_dollars(frame.get('pnl'))}")
    return "\n".join(lines)


def _dollars(points: object) -> str:
    amount = _dollar_amount(points)
    if amount is None:
        return "—"
    return f"{amount:+,.0f}"


def _money(points: object) -> str:
    amount = _dollar_amount(points)
    if amount is None:
        return f"{'—':>12}"
    return f"{amount:+12,.0f}"


def _dollar_amount(points: object) -> int | None:
    if not isinstance(points, (int, float)):
        return None
    return round(float(points) * 100)


if __name__ == "__main__":
    raise SystemExit(main())
