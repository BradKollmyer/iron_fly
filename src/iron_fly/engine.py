"""Main engine. An SPX print wakes a live stop; sim decides every 3 seconds.

The result matches the backtest step frame: ladder, flies, and P&L.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from datetime import time as clock_time

from iron_fly.config import Settings
from iron_fly.current_config import current
from iron_fly.db import connect
from iron_fly.desk import (
    _BOOK_LOCK,
    _PREV_PRINT,
    OWNER_PLAYER,
    _arm_clocks,
    _clock,
    _clock_text,
    _ensure_state,
    _fill_intent,
    _pid,
    _saved_spec,
    _stamp_flies,
    _store_ladder,
    bind_player,
    closed_pause,
    ensure_cash_close,
    load_book,
    load_switches,
    quotes_session,
    remember_cash_close,
    reset_player,
    roll_session,
    save_book,
)
from iron_fly.domain.market import DeskMarks
from iron_fly.domain.models import IntentKind, Quote
from iron_fly.fly_monitor import flies as fly_board
from iron_fly.fly_monitor import latest_chain, note_book
from iron_fly.market_feed import read_chain, read_spx
from iron_fly.strategy.dale_og import DaleOg
from iron_fly.strategy.rules import book_clock, wings_for_clock

log = logging.getLogger("iron_fly.step")

_LOCK = threading.Lock()
_STEP: dict | None = None
_SEEN: tuple | None = None
_MISS_LOGGED = False
_TAPE: list[dict] = []
_TAPE_DAY = ""


def last_step(conn) -> dict:
    """Memory first. The database is the fallback after a restart."""
    global _STEP
    with _LOCK:
        if _STEP is not None:
            return _STEP
    loaded = _read_step(conn)
    with _LOCK:
        if _STEP is None:
            _STEP = loaded
        return _STEP


def watch_next(conn) -> dict:
    """Watch and Next from the last engine step."""
    frame = last_step(conn)
    return {
        "watch": frame.get("watch"),
        "next": frame.get("next"),
        "ladder": frame.get("ladder") or [],
    }


def prime(settings: Settings) -> dict:
    """Load the last step before the first SPX read."""
    global _STEP
    conn = connect(settings.db_path)
    token = bind_player(OWNER_PLAYER)
    try:
        _ensure_state(conn)
        loaded = _read_step(conn)
        conn.commit()
    finally:
        reset_player(token)
        conn.close()
    with _LOCK:
        _STEP = loaded
    return loaded


def tick(settings: Settings, *, now: datetime | None = None) -> dict:
    """One decision. A missed SPX print keeps the last step."""
    conn = connect(settings.db_path)
    token = bind_player(OWNER_PLAYER)
    try:
        frame = _refresh(conn, settings, now)
        conn.commit()
        return frame
    finally:
        reset_player(token)
        conn.close()


def tick_interval(settings: Settings) -> float:
    """Public/replay desk paces decisions every 3 seconds."""
    del settings
    return 3.0


def serve(settings: Settings, stop: threading.Event, interval: float | None = None) -> None:
    """Decide until `stop` is set. One bad tick does not end the loop."""
    waiting = False
    polling = False
    while not stop.is_set():
        if interval is None and not quotes_session():
            if not waiting:
                log.info("Main engine is waiting for the market. Decisions run weekdays from 9:29 ET.")
                waiting = True
                polling = False
            stop.wait(closed_pause())
            continue
        started = time.monotonic()
        try:
            tick(settings)
            if quotes_session():
                waiting = False
                if not polling:
                    log.info("Main engine stepping the replay desk.")
                    polling = True
            elif not waiting:
                log.info("Main engine is waiting for the market. Decisions run weekdays from 9:29 ET.")
                waiting = True
                polling = False
        except Exception:
            log.exception("main engine tick failed")
        _wait_next(settings, stop, started, interval)


def _wait_next(settings: Settings, stop: threading.Event, started: float, interval: float | None) -> None:
    """A live print wakes the next stop. A fixed interval is for tests."""
    if interval is not None:
        remaining = interval - (time.monotonic() - started)
        if remaining > 0:
            stop.wait(remaining)
        return
    
    if False:
        spent = time.monotonic() - started
        if spent < 0.10 and stop.wait(0.10 - spent):
            return
        time.sleep(0.25)
        return
    if False:
        spent = time.monotonic() - started
        if spent < 0.10 and stop.wait(0.10 - spent):
            return
        time.sleep(0.25)
        return
    remaining = tick_interval(settings) - (time.monotonic() - started)
    if remaining > 0:
        stop.wait(remaining)


def session_tape(after: int | None = None) -> list[dict]:
    """SPX prints recorded by the engine. `after` keeps only later seconds."""
    with _LOCK:
        rows = list(_TAPE)
    if after is None:
        return rows
    return [row for row in rows if int(row["time"]) > int(after)]


def clear() -> None:
    """Drop the in-memory step. Tests use this between cases."""
    global _STEP, _SEEN, _MISS_LOGGED, _TAPE, _TAPE_DAY
    with _LOCK:
        _STEP = None
        _SEEN = None
        _MISS_LOGGED = False
        _TAPE = []
        _TAPE_DAY = ""


def _refresh(conn, settings: Settings, now: datetime | None) -> dict:
    global _MISS_LOGGED
    clock = _clock(now)
    rolled = roll_session(conn, clock)
    ensure_cash_close(conn, settings, DeskMarks(ts=clock), clock)
    conn.commit()
    if not quotes_session(clock):
        if rolled:
            frame = _empty()
            _write_step(conn, frame)
            _remember(frame)
            return frame
        return last_step(conn)
    spot = read_spx(settings, conn, clock)
    if spot is None or spot.last is None:
        if not _MISS_LOGGED:
            log.warning("Main engine has no SPX print")
            _MISS_LOGGED = True
        log.info("engine %s spx miss, kept the last step", clock.strftime("%H:%M:%S"))
        return last_step(conn)
    _MISS_LOGGED = False
    if spot.ts is None:
        spot.ts = clock
    _note_print(float(spot.last), clock)
    # Refine and persist cash close while SPX is still printing after the bell.
    ensure_cash_close(conn, settings, DeskMarks(last=float(spot.last), ts=clock), clock)
    chain = latest_chain()
    chain_from = "monitor"
    if chain is None:
        chain, _source = read_chain(settings, conn, clock)
        chain_from = "fetched" if chain is not None else "none"
    spot.chain = chain
    config = current(conn)
    board = fly_board(conn)
    frame = _decide(conn, settings, spot, config, clock, chain_from)
    _overlay_pnl(frame, board)
    _write_step(conn, frame)
    _remember(frame)
    return frame


def _decide(conn, settings: Settings, marks: DeskMarks, config, clock: datetime, chain_from: str) -> dict:
    wall = book_clock(clock)
    switches = load_switches(conn)
    spec = _saved_spec(conn) if switches["use_config"] else None
    if spec is not None:
        # Use Config always trades the YAML rules; open_paused is for manual Auto Open.
        auto_open = True
        auto_drop = True
    else:
        auto_open = switches["auto_open"] and not switches["open_paused"]
        auto_drop = switches["auto_drop"]
    with _BOOK_LOCK:
        book = load_book(conn)
        book.regime = config.regime
        book.stop_pad = int(config.stop)
        strategy = DaleOg(book)
        spot = float(marks.last)
        strategy.on_quote(Quote(symbol="SPX", last=spot, bid=marks.bid, ask=marks.ask, ts=clock))
        mode = book.mode_at(clock).name
        prev = _PREV_PRINT.get(_pid())
        width_choice = str(config.wings)
        strategy.arm_cookie = spec is not None
        _arm_clocks(
            strategy,
            spec,
            auto_open=auto_open,
            em_pct=config.em_pct,
            wall=wall,
            book=book,
        )
        if spec is not None:
            raw_until = spec.auto.get("add_until")
            if raw_until:
                strategy.add_until = clock_time.fromisoformat(str(raw_until))
        if not auto_open and not auto_drop:
            _PREV_PRINT[_pid()] = (spot, mode)
            decision = "manual"
        else:
            if prev is not None:
                strategy._prev_spot = prev[0]
                strategy._prev_mode = prev[1]
            wings = _width(width_choice, wall)
            intents = strategy.decide(spot, clock, wings=wings)
            filled = False
            taken = []
            for intent in intents:
                blocked = (intent.kind is IntentKind.ADD and not auto_open) or (
                    intent.kind is IntentKind.CLOSE and not auto_drop
                )
                strike = intent.payload.get("strike")
                if blocked:
                    taken.append(f"blocked {intent.kind.value} {strike}")
                    log.info(
                        "step %s decision %s %s blocked",
                        _clock_text(clock),
                        intent.kind.value,
                        strike,
                    )
                    continue
                taken.append(f"{intent.kind.value} {strike}")
                log.info(
                    "step %s decision %s %s %s",
                    _clock_text(clock),
                    intent.kind.value,
                    strike,
                    intent.reason,
                )
                filled = (
                    _fill_intent(
                        conn,
                        settings,
                        clock.date(),
                        strategy,
                        intent,
                        clock,
                        wings or wings_for_clock(wall),
                        marks.chain,
                    )
                    or filled
                )
            if filled:
                save_book(conn, book)
                note_book(conn)
            _PREV_PRINT[_pid()] = (spot, book.mode_at(clock).name)
            decision = ", ".join(taken) if taken else "hold"
        frame = _frame(book, marks, config, clock, auto_open, auto_drop, bool(switches["use_config"]))
        _store_ladder(conn, frame["ladder"])
        watch = frame.get("watch") or {}
        nxt = frame.get("next") or {}
        f1 = "off"
        if spec is not None and auto_open:
            f1 = "held" if strategy.f1_at is None else strategy.f1_at.strftime("%H:%M")
        prev_text = "none" if prev is None else f"{float(prev[0]):.2f}"
        log.info(
            "engine %s spx=%.2f chain=%s use_config=%s auto_open=%s auto_drop=%s f1=%s prev=%s regime=%s wings=%s stop=+%s watch=%s/%s next=%s/%s flies=%s decision=%s",
            _clock_text(clock),
            spot,
            chain_from,
            int(bool(switches["use_config"])),
            int(auto_open),
            int(auto_drop),
            f1,
            prev_text,
            config.regime,
            config.wings,
            int(config.stop),
            _level(watch.get("spot")),
            _level(watch.get("strike")),
            _level(nxt.get("spot")),
            _level(nxt.get("strike")),
            len(frame.get("flies") or []),
            decision,
        )
    return frame


def _frame(book, marks: DeskMarks, config, clock: datetime, auto_open: bool, auto_drop: bool, use_config: bool) -> dict:
    spot = float(marks.last)
    data = book.live_strip(spot, clock)
    _stamp_flies(data, book, marks, clock)
    ladder = []
    for row in data.get("ladder") or []:
        ladder.append(
            {
                "watch": row.get("watch"),
                "kind": row.get("kind"),
                "strike": row.get("strike"),
                "var": row.get("var"),
                "enabled": bool(row.get("enabled")),
                "rule": bool(row.get("rule")),
            }
        )
    rows = []
    for row in data.get("flies") or []:
        if row.get("status") == "empty":
            continue
        rows.append(
            {
                "slot": row.get("slot"),
                "strike": row.get("strike"),
                "wings": row.get("put_wing"),
                "status": row.get("status"),
                "time_open": row.get("time_open") or "",
                "time_close": row.get("time_close") or "",
                "origin": row.get("origin") or "",
                "sto": row.get("sto"),
                "btc": row.get("btc_actual"),
                "pnl": row.get("pnl_mid"),
            }
        )
    up, down = book.watches(spot, clock)
    shown = config.wings if str(config.wings) != "auto" else str(wings_for_clock(book_clock(clock)))
    return {
        "ladder": ladder,
        "flies": rows,
        "manual": not auto_open and not auto_drop,
        "engine": {
            "regime": config.regime,
            "stop": int(config.stop),
            "wings": shown,
            "use_config": use_config,
            "auto_open": auto_open,
            "auto_drop": auto_drop,
        },
        "pnl": data.get("pnl_mid_total"),
        "max_profit": data.get("max_profit"),
        "max_profit_at": data.get("max_profit_at"),
        "max_profit_at_hi": data.get("max_profit_at_hi"),
        "max_profit_n": data.get("max_profit_n"),
        "watch": {"spot": up.trigger, "strike": up.add_strike, "var": round(up.trigger - spot, 2)},
        "next": {"spot": down.trigger, "strike": down.add_strike, "var": round(down.trigger - spot, 2)},
        "spx": spot,
        "at": int(clock.timestamp()),
    }


def _overlay_pnl(frame: dict, board) -> None:
    """Fill a blank mark from the fly monitor when this tick has no chain yet."""
    known = {(fly.strike, fly.status): fly.pnl for fly in board.flies}
    total = 0.0
    seen = False
    for row in frame.get("flies") or []:
        if row.get("pnl") is None:
            row["pnl"] = known.get((row.get("strike"), row.get("status")))
        if row.get("pnl") is not None:
            total += float(row["pnl"])
            seen = True
    if frame.get("pnl") is None and seen:
        frame["pnl"] = round(total, 2)


def _level(value: object) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "—"
    return f"{float(value):.0f}"


def _width(choice: str, wall) -> int | None:
    if choice == "auto":
        return None
    try:
        return int(choice)
    except (TypeError, ValueError):
        return wings_for_clock(wall)


def _write_step(conn, frame: dict) -> None:
    conn.execute(
        "UPDATE desk_state SET step_json = ? WHERE player_id = ?",
        (json.dumps(frame), _pid()),
    )


def _read_step(conn) -> dict:
    row = conn.execute("SELECT step_json FROM desk_state WHERE player_id = ?", (_pid(),)).fetchone()
    if row is None or not row["step_json"]:
        return _empty()
    try:
        parsed = json.loads(row["step_json"])
    except json.JSONDecodeError:
        return _empty()
    if not isinstance(parsed, dict):
        return _empty()
    return parsed


def _note_print(value: float, clock: datetime) -> None:
    """One SPX point per second for the chart. A later print in the same second replaces it."""
    global _TAPE, _TAPE_DAY
    day = clock.date().isoformat()
    remember_cash_close(clock, value)
    point = {"time": int(clock.timestamp()), "value": round(float(value), 2)}
    with _LOCK:
        if _TAPE_DAY != day:
            _TAPE = []
            _TAPE_DAY = day
        if _TAPE and int(_TAPE[-1]["time"]) == point["time"]:
            _TAPE[-1] = point
        elif not _TAPE or point["time"] > int(_TAPE[-1]["time"]):
            _TAPE.append(point)


def _remember(frame: dict) -> None:
    global _STEP, _SEEN
    signature = (
        frame.get("spx"),
        frame.get("pnl"),
        tuple((row.get("slot"), row.get("strike"), row.get("status")) for row in frame.get("flies") or []),
        tuple((row.get("kind"), row.get("strike"), row.get("watch")) for row in frame.get("ladder") or []),
    )
    with _LOCK:
        _STEP = frame
        changed = signature != _SEEN
        _SEEN = signature
    if not changed:
        return
    listed = (
        " ".join(f"{row.get('slot')}:{row.get('strike')}:{row.get('status')}" for row in frame.get("flies") or [])
        or "none"
    )
    log.info(
        "step return flies=%s pnl=%s watch=%s",
        listed,
        frame.get("pnl"),
        len(frame.get("ladder") or []),
    )


def _empty() -> dict:
    return {
        "ladder": [],
        "flies": [],
        "pnl": None,
        "watch": None,
        "next": None,
        "manual": True,
        "engine": {},
    }
