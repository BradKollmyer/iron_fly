"""Fly monitor. Once a second, mark open flies from the chain and store the P&L."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from datetime import time as clock_time

from iron_fly.config import Settings
from iron_fly.db import connect
from iron_fly.desk import (
    _BOOK_LOCK,
    _CASH_AT,
    OWNER_PLAYER,
    _clock,
    _ensure_state,
    _pid,
    _stamp_flies,
    bind_player,
    closed_pause,
    ensure_cash_close,
    load_book,
    quotes_session,
    reset_player,
    roll_session,
)
from iron_fly.domain.market import ChainSnapshot, DeskMarks, combo_mark, leg_book, straddle_book
from iron_fly.market_feed import last_spx, read_chain
from iron_fly.orders import leftover_long_rows, leftovers_sell_allowed, sell_ready_leftovers
from iron_fly.strategy.rules import book_clock

log = logging.getLogger("iron_fly.step")

_LOCK = threading.Lock()
_BOARD: FlyBoard | None = None
_CHAIN: ChainSnapshot | None = None
_SEEN: tuple | None = None
_MISS_LOGGED = False
_CHAIN_MONO = 0.0
_OPEN_DAY = ""
_CLOSE_DAY = ""
_OPEN_AT = clock_time(9, 30)
_CLOSE_AT = clock_time(16, 0)


@dataclass(frozen=True)
class FlySnap:
    strike: float
    wings: int
    credit: float
    status: str
    pnl: float | None
    opened_at: str
    origin: str


@dataclass(frozen=True)
class FlyBoard:
    flies: tuple[FlySnap, ...]
    pnl: float | None
    source: str
    updated_at: datetime | None


def flies(conn) -> FlyBoard:
    """Memory first. The database is the fallback after a restart."""
    global _BOARD
    with _LOCK:
        if _BOARD is not None:
            return _BOARD
    loaded = _from_db(conn, None)
    with _LOCK:
        if _BOARD is None:
            _BOARD = loaded
        return _BOARD


def latest_chain() -> ChainSnapshot | None:
    with _LOCK:
        return _CHAIN


def prime(settings: Settings) -> FlyBoard:
    """Load the saved flies before the first chain read."""
    global _BOARD
    conn = connect(settings.db_path)
    token = bind_player(OWNER_PLAYER)
    try:
        _ensure_state(conn)
        loaded = _from_db(conn, None)
        conn.commit()
    finally:
        reset_player(token)
        conn.close()
    with _LOCK:
        _BOARD = loaded
    return loaded


def note_book(conn) -> FlyBoard:
    """Publish the book just written by the engine. Keep P&L already marked."""
    global _BOARD
    with _LOCK:
        previous = _BOARD
    loaded = _from_db(conn, previous)
    with _LOCK:
        _BOARD = loaded
    return loaded


def tick(settings: Settings, *, now: datetime | None = None) -> FlyBoard:
    """One mark. A chain miss keeps the P&L already stored."""
    conn = connect(settings.db_path)
    token = bind_player(OWNER_PLAYER)
    try:
        board = _refresh(conn, settings, now)
        conn.commit()
        return board
    finally:
        reset_player(token)
        conn.close()


def serve(settings: Settings, stop: threading.Event, interval: float = 1.0) -> None:
    """Mark until `stop` is set. One bad tick does not end the loop."""
    waiting = False
    polling = False
    while not stop.is_set():
        # A short interval is a test. The live loop sleeps through a closed market.
        if interval >= 1.0 and not quotes_session():
            if not waiting:
                log.info("Fly monitor is waiting for the market. Open flies are marked weekdays from 9:29 ET.")
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
                    log.info("Fly monitor updating from the replay desk.")
                    polling = True
            elif not waiting:
                log.info("Fly monitor is waiting for the market. Open flies are marked weekdays from 9:29 ET.")
                waiting = True
                polling = False
        except Exception:
            log.exception("fly monitor tick failed")
        remaining = interval - (time.monotonic() - started)
        if remaining > 0:
            stop.wait(remaining)


def clear() -> None:
    """Drop the in-memory flies and chain. Tests use this between cases."""
    global _BOARD, _CHAIN, _SEEN, _MISS_LOGGED, _CHAIN_MONO, _OPEN_DAY, _CLOSE_DAY
    with _LOCK:
        _BOARD = None
        _CHAIN = None
        _SEEN = None
        _MISS_LOGGED = False
        _CHAIN_MONO = 0.0
        _OPEN_DAY = ""
        _CLOSE_DAY = ""


def chain_due(settings: Settings, clock: datetime, *, now: float | None = None) -> bool:
    """The chain is for EM and P&L. Live and sim read it every 2 seconds.

    The 9:30 and 16:00 prints are read even when that slot has not elapsed.
    """
    del settings
    moment = time.monotonic() if now is None else now
    day = clock.date().isoformat()
    wall = book_clock(clock)
    if _OPEN_DAY != day and _OPEN_AT <= wall < clock_time(9, 31):
        return True
    if _CLOSE_DAY != day and _CLOSE_AT <= wall < clock_time(16, 0, 2):
        return True
    if _CHAIN_MONO <= 0:
        return True
    return moment - _CHAIN_MONO >= 2.0


def _refresh(conn, settings: Settings, now: datetime | None) -> FlyBoard:
    global _BOARD, _CHAIN, _MISS_LOGGED, _CHAIN_MONO, _OPEN_DAY, _CLOSE_DAY
    clock = _clock(now)
    roll_session(conn, clock)
    with _LOCK:
        previous = _BOARD
    past_cash = book_clock(clock) >= _CASH_AT
    day = clock.date().isoformat()
    wall = book_clock(clock)
    want_open = _OPEN_DAY != day and _OPEN_AT <= wall < clock_time(9, 31)
    want_close = _CLOSE_DAY != day and _CLOSE_AT <= wall < clock_time(16, 0, 2)
    session = quotes_session(clock) and (not past_cash or want_close)
    active, parked = _open_book(conn)
    conn.commit()
    need_chain = session and chain_due(settings, clock)
    chain = None
    chain_source = ""
    if need_chain:
        _CHAIN_MONO = time.monotonic()
        chain, chain_source = read_chain(settings, conn, clock)
    with _BOOK_LOCK:
        book = load_book(conn)
        active = any(fly.status == "active" for fly in book.flies)
        parked = bool(leftover_long_rows(conn))
        source = previous.source if previous is not None else "database"
        ensure_cash_close(conn, settings, DeskMarks(ts=clock), clock)
        if need_chain:
            if chain is None:
                if not _MISS_LOGGED:
                    log.warning("Fly monitor has no option chain from the replay tape")
                    _MISS_LOGGED = True
            else:
                _MISS_LOGGED = False
                source = chain_source or source
                if want_open:
                    _OPEN_DAY = day
                    _log_open_quote(clock, chain)
                if want_close:
                    _CLOSE_DAY = day
                    _log_close_quote(clock, book, chain)
                elif active:
                    _log_legs(clock, book, chain)
                if active and not past_cash:
                    _write_pnl(conn, book, chain, clock)
                elif past_cash:
                    _write_pnl(conn, book, None, clock)
                if parked and not past_cash:
                    from iron_fly.desk import _leftover_auto

                    if leftovers_sell_allowed(wall, _leftover_auto(conn)):
                        sell_ready_leftovers(conn, settings, chain, _leftover_auto(conn), clock)
        elif not active or past_cash:
            _write_pnl(conn, book, None, clock)
        keep = previous if chain is None and active else None
        board = _from_db(conn, keep)
        board = FlyBoard(board.flies, board.pnl, source, clock)
        if not quotes_session(clock):
            chain_label = "closed"
        elif not active and not parked:
            chain_label = "idle"
        elif chain is None:
            chain_label = "none"
        else:
            chain_label = chain_source or source
    with _LOCK:
        _BOARD = board
        if chain is not None:
            _CHAIN = chain
    if quotes_session(clock):
        _log(board, clock, chain_label)
    return board


def _open_book(conn) -> tuple[bool, bool]:
    book = load_book(conn)
    active = any(fly.status == "active" for fly in book.flies)
    return active, bool(leftover_long_rows(conn))


def _write_pnl(conn, book, chain, clock: datetime) -> None:
    data = {"flies": [{} for _ in book.flies]}
    marks = DeskMarks(chain=chain, ts=clock, source="")
    _stamp_flies(data, book, marks, clock)
    rows = conn.execute(
        "SELECT id FROM fly WHERE player_id = ? ORDER BY id",
        (_pid(),),
    ).fetchall()
    for row, stamped in zip(rows, data["flies"], strict=True):
        conn.execute(
            "UPDATE fly SET pnl = ? WHERE id = ? AND player_id = ?",
            (stamped.get("pnl_mid"), row["id"], _pid()),
        )


def _from_db(conn, previous: FlyBoard | None) -> FlyBoard:
    rows = conn.execute(
        "SELECT * FROM fly WHERE player_id = ? ORDER BY id",
        (_pid(),),
    ).fetchall()
    kept = {}
    if previous is not None:
        kept = {(fly.strike, fly.opened_at, fly.status): fly.pnl for fly in previous.flies}
    snaps = []
    total = 0.0
    seen = False
    for row in rows:
        opened = str(row["opened_at"])
        status = str(row["status"])
        pnl = _float(row["pnl"]) if "pnl" in row.keys() else None
        if pnl is None:
            pnl = kept.get((float(row["strike"]), opened, status))
        snaps.append(
            FlySnap(
                strike=float(row["strike"]),
                wings=int(row["wings"]),
                credit=float(row["credit"]),
                status=status,
                pnl=pnl,
                opened_at=opened,
                origin=str(row["origin"] or ""),
            )
        )
        if pnl is not None:
            total += pnl
            seen = True
    return FlyBoard(tuple(snaps), round(total, 2) if seen else None, "database", None)


def _log_open_quote(clock: datetime, chain: ChainSnapshot) -> None:
    spot = last_spx()
    stamp = clock.strftime("%H:%M:%S")
    if spot is None or spot.last is None:
        log.info("step %s open quote", stamp)
        return
    strike = 5 * round(float(spot.last) / 5)
    log.info("step %s open quote spx=%.2f %s", stamp, float(spot.last), straddle_book(chain, strike))


def _log_close_quote(clock: datetime, book, chain: ChainSnapshot) -> None:
    stamp = clock.strftime("%H:%M:%S")
    flies = [fly for fly in book.flies if fly.status == "active"]
    if not flies:
        log.info("step %s close quote no open fly", stamp)
        return
    for fly in flies:
        mark = combo_mark(chain, fly.strike, fly.wings)
        pnl = None if mark is None else round(fly.credit - mark, 2)
        log.info(
            "step %s close quote %s mark=%s pnl=%s %s",
            stamp,
            int(fly.strike),
            "—" if mark is None else f"{mark:.2f}",
            "—" if pnl is None else f"{pnl:.2f}",
            leg_book(chain, fly.strike, fly.wings),
        )


def _log_legs(clock: datetime, book, chain: ChainSnapshot) -> None:
    stamp = clock.strftime("%H:%M:%S")
    for fly in book.flies:
        if fly.status != "active":
            continue
        log.info("step %s legs %s %s", stamp, int(fly.strike), leg_book(chain, fly.strike, fly.wings))


def _log(board: FlyBoard, clock: datetime, chain_label: str) -> None:
    global _SEEN
    signature = tuple((fly.strike, fly.status, fly.pnl) for fly in board.flies)
    with _LOCK:
        _SEEN = signature
    detail = " ".join(f"{int(fly.strike)}:{fly.status}:{_fmt(fly.pnl)}" for fly in board.flies) or "none"
    open_count = sum(1 for fly in board.flies if fly.status == "active")
    log.info(
        "flies %s open=%s chain=%s pnl=%s %s",
        clock.strftime("%H:%M:%S"),
        open_count,
        chain_label,
        _fmt(board.pnl),
        detail,
    )


def _fmt(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.2f}"


def _float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None
