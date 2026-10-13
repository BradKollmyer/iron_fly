"""Leftover longs and the hold check that refuses a short already held long.

A 4-leg close that finds a dead wing buys back the two shorts and parks the
wings. Those parked longs show above the blotter. A later open is refused
only when one of its new shorts is the same contract. Matching wing leftovers
can be reused: STO the short straddle and attach the parked longs.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import time

from iron_fly.config import Settings
from iron_fly.domain.market import ChainSnapshot, StrikeQuote, mid
from iron_fly.strategy.spec import leftover_until_clock

log = logging.getLogger("iron_fly.step")

DEAD_LONG_MARK = 0.05
HOLD_CONFLICT = "Same contract cannot stay long and short."
ROLL_ABOVE = 0.50
ROLL_TARGET = 0.40


def dead_reason(bid: float | None, ask: float | None) -> str | None:
    """Why a long wing cannot ride in a 4-leg close. None when it still trades."""
    if bid is None and ask is None:
        return "no quote"
    if bid is None or bid <= 0:
        return "no bid"
    mark = mid(bid, ask)
    if mark is None or mark < DEAD_LONG_MARK:
        shown = 0.0 if mark is None else mark
        return f"mark {shown:.2f} < 0.05"
    return None


def dead_wings(chain: ChainSnapshot | None, strike: float, wings: int) -> list[str]:
    """Labels for long wings that cannot STC. Empty when both wings still trade."""
    notes = []
    for right, wing in (("put", strike - wings), ("call", strike + wings)):
        row = _row(chain, wing)
        reason = "no quote" if row is None else dead_reason(*_side(row, right))
        if reason:
            notes.append(f"{right} {int(wing)} {reason}")
    return notes


def dead_long_banner(flies: list[dict]) -> list[dict]:
    """Rows for the blotter warning: slot, label, detail. Active flies only."""
    out: list[dict] = []
    for fly in flies:
        notes = fly.get("dead_longs") or []
        if fly.get("status") != "active" or not notes:
            continue
        strike = fly.get("strike")
        out.append(
            {
                "slot": fly.get("slot"),
                "label": int(strike) if strike is not None else fly.get("label"),
                "detail": ", ".join(str(item) for item in notes),
            }
        )
    return out


def short_mark(chain: ChainSnapshot | None, strike: float) -> float | None:
    """Debit to buy back the two shorts."""
    row = _row(chain, strike)
    if row is None:
        return None
    call = mid(row.call_bid, row.call_ask)
    put = mid(row.put_bid, row.put_ask)
    if call is None or put is None:
        return None
    return round(call + put, 2)


def wing_lots(chain: ChainSnapshot | None, strike: float, wings: int) -> list[dict]:
    """The two long wings, one row per OCC."""
    lots = []
    for right, wing in (("put", strike - wings), ("call", strike + wings)):
        row = _row(chain, wing)
        lots.append(
            {
                "occ": _occ(row, right, wing),
                "right": right,
                "strike": float(wing),
                "quantity": 1,
            }
        )
    return lots


def park_wings(conn: sqlite3.Connection, chain: ChainSnapshot | None, strike: float, wings: int) -> list[dict]:
    """Remember the wings a shorts-only close left behind. One row per OCC."""
    return park_lots(conn, wing_lots(chain, strike, wings))


def leftovers_sell_allowed(wall: time, auto: dict[str, object] | None = None) -> bool:
    """True while parked longs may still be sold or rolled."""
    return wall < leftover_until_clock(auto)


def reusable_wing_lots(
    conn: sqlite3.Connection,
    chain: ChainSnapshot | None,
    strike: float,
    wings: int,
) -> list[dict] | None:
    """Both parked wing longs for this fly, or None if either is missing."""
    parked = leftover_long_rows(conn)
    if not parked:
        return None
    by_key = {(str(lot["right"]), int(lot["strike"])): lot for lot in parked}
    by_occ = {str(lot["occ"]): lot for lot in parked}
    matched: list[dict] = []
    for need in wing_lots(chain, strike, wings):
        lot = by_key.get((str(need["right"]), int(need["strike"])))
        if lot is None:
            lot = by_occ.get(str(need["occ"]))
        if lot is None or int(lot["quantity"]) < 1:
            return None
        matched.append(lot)
    return matched if len(matched) == 2 else None


def consume_wing_lots(conn: sqlite3.Connection, lots: list[dict]) -> None:
    """Remove one contract from each reused leftover row."""
    pid = _pid()
    for lot in lots:
        occ = str(lot["occ"])
        qty = int(lot["quantity"]) - 1
        if qty <= 0:
            conn.execute(
                "DELETE FROM leftover_long WHERE player_id = ? AND occ = ?",
                (pid, occ),
            )
        else:
            conn.execute(
                "UPDATE leftover_long SET quantity = ? WHERE player_id = ? AND occ = ?",
                (qty, pid, occ),
            )


def park_lots(conn: sqlite3.Connection, lots: list[dict]) -> list[dict]:
    """Remember longs the wing order did not take. One row per OCC."""
    pid = _pid()
    for lot in lots:
        conn.execute(
            """
            INSERT INTO leftover_long (player_id, occ, right, strike, quantity)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(player_id, occ) DO UPDATE SET
                right = excluded.right,
                strike = excluded.strike,
                quantity = excluded.quantity
            """,
            (pid, lot["occ"], lot["right"], float(lot["strike"]), int(lot["quantity"])),
        )
    return lots


def leftover_long_rows(conn: sqlite3.Connection) -> list[dict]:
    """Parked longs. Empty when every long is still attached to a fly."""
    rows = conn.execute(
        """
        SELECT occ, right, strike, quantity FROM leftover_long
        WHERE player_id = ?
        ORDER BY strike, right
        """,
        (_pid(),),
    ).fetchall()
    return [
        {
            "occ": str(row["occ"]),
            "right": str(row["right"]),
            "strike": float(row["strike"]),
            "quantity": int(row["quantity"]),
        }
        for row in rows
    ]


def leftover_long_note(lots: list[dict]) -> str | None:
    if not lots:
        return None
    parts = [f"{lot['right']} {int(lot['strike'])}" for lot in lots]
    return "leftover longs " + ", ".join(parts)


def leftover_long_panel(conn: sqlite3.Connection, chain: ChainSnapshot | None) -> list[dict]:
    """One row per parked OCC, with the latest bid, ask, and mid."""
    out = []
    for lot in leftover_long_rows(conn):
        row = _row(chain, float(lot["strike"]))
        bid, ask = (None, None) if row is None else _side(row, str(lot["right"]))
        mark = mid(bid, ask)
        reason = dead_reason(bid, ask)
        out.append(
            {
                "right": lot["right"],
                "strike": lot["strike"],
                "occ": lot["occ"],
                "quantity": lot["quantity"],
                "bid": bid,
                "ask": ask,
                "mid": None if mark is None else round(mark, 2),
                "dead": reason is not None,
                "dead_reason": reason,
            }
        )
    return out


def hold_conflict(conn: sqlite3.Connection, chain: ChainSnapshot | None, strike: float) -> str | None:
    """Refuse an open whose new short is already a leftover long.

    A wing parked at another strike does not block the open.
    """
    parked = leftover_long_rows(conn)
    if not parked:
        return None
    keys = set()
    for lot in parked:
        keys.add(lot["occ"])
        keys.add(_key(str(lot["right"]), float(lot["strike"])))
    hits = []
    for right in ("call", "put"):
        row = _row(chain, strike)
        occ = _occ(row, right, strike)
        if occ in keys or _key(right, strike) in keys:
            hits.append(f"long {int(strike)} {right}")
    if not hits:
        return None
    return "This fly would fight leftover longs (" + ", ".join(hits) + "). " + HOLD_CONFLICT


def sell_ready_leftovers(
    conn: sqlite3.Connection,
    settings: Settings,
    chain: ChainSnapshot | None,
) -> int:
    """Sell a cheap parked long. Roll one worth more than $0.50 to the $0.40 strike.

    The put and the call are separate. The $0.40 wing stays parked.
    """
    done = 0
    for lot in leftover_long_rows(conn):
        right = str(lot["right"])
        strike = float(lot["strike"])
        row = _row(chain, strike)
        bid, ask = (None, None) if row is None else _side(row, right)
        value = _value(bid, ask)
        anchor = _forty_strike(chain, right)
        if anchor is not None and int(anchor) == int(strike):
            continue
        if value is not None and value > ROLL_ABOVE:
            target = _forty_strike(chain, right, beyond=strike)
            if target is not None and _roll_leftover(conn, settings, chain, lot, target):
                done += 1
                continue
        if bid is None or bid <= 0:
            continue
        if close_leftover_long(conn, settings, chain, str(lot["occ"])) is None:
            done += 1
    return done


def _value(bid: float | None, ask: float | None) -> float | None:
    mark = mid(bid, ask)
    if mark is not None:
        return mark
    if bid is not None and bid > 0:
        return bid
    return None


def _forty_strike(chain: ChainSnapshot | None, right: str, *, beyond: float | None = None) -> float | None:
    """Strike of this right whose mid is nearest $0.40. `beyond` stays further out."""
    if chain is None:
        return None
    marks: list[tuple[float, float]] = []
    for row in chain.strikes:
        mark = _value(*_side(row, right))
        if mark is None or mark <= 0:
            continue
        if beyond is not None and right == "put" and row.strike >= beyond:
            continue
        if beyond is not None and right == "call" and row.strike <= beyond:
            continue
        marks.append((row.strike, mark))
    if not marks:
        return None
    return min(marks, key=lambda item: (abs(item[1] - ROLL_TARGET), abs(item[0] - (beyond or item[0]))))[0]


def _roll_leftover(
    conn: sqlite3.Connection,
    settings: Settings,
    chain: ChainSnapshot | None,
    lot: dict,
    target: float,
) -> bool:
    """STC this long and BTO the further strike. The new long stays parked."""
    right = str(lot["right"])
    row = _row(chain, target)
    opened = _occ(row, right, target)
    closed = str(lot["occ"])
    if opened == closed:
        return False
    quantity = int(lot["quantity"])
    _replace_leftover(conn, closed, opened, right, target, quantity)
    log.info("step rolled leftover %s %s to %s", right, int(lot["strike"]), int(target))
    return True


def _replace_leftover(
    conn: sqlite3.Connection,
    closed: str,
    opened: str,
    right: str,
    strike: float,
    quantity: int,
) -> None:
    pid = _pid()
    conn.execute(
        "DELETE FROM leftover_long WHERE player_id = ? AND occ = ?",
        (pid, closed),
    )
    conn.execute(
        """
        INSERT INTO leftover_long (player_id, occ, right, strike, quantity)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(player_id, occ) DO UPDATE SET
            quantity = leftover_long.quantity + excluded.quantity
        """,
        (pid, opened, right, strike, quantity),
    )


def close_leftover_long(
    conn: sqlite3.Connection,
    settings: Settings,
    chain: ChainSnapshot | None,
    occ: str,
) -> str | None:
    """STC one parked long at the bid from the replay chain."""
    lot = next((row for row in leftover_long_rows(conn) if row["occ"] == occ), None)
    if lot is None:
        return "missing"
    row = _row(chain, float(lot["strike"]))
    bid, _ask = (None, None) if row is None else _side(row, str(lot["right"]))
    if bid is None or bid <= 0:
        return "nobid"
    conn.execute(
        "DELETE FROM leftover_long WHERE player_id = ? AND occ = ?",
        (_pid(), occ),
    )
    log.info("step sold leftover long %s %s", lot["right"], int(lot["strike"]))
    return None


def _pid() -> str:
    from iron_fly.desk import _pid as player

    return player()


def _row(chain: ChainSnapshot | None, strike: float) -> StrikeQuote | None:
    if chain is None:
        return None
    for row in chain.strikes:
        if row.strike == strike:
            return row
    return None


def _side(row: StrikeQuote, right: str) -> tuple[float | None, float | None]:
    if right == "call":
        return row.call_bid, row.call_ask
    return row.put_bid, row.put_ask


def _occ(row: StrikeQuote | None, right: str, strike: float) -> str:
    if row is not None:
        symbol = row.call_symbol if right == "call" else row.put_symbol
        if symbol:
            return symbol
    return _key(right, strike)


def _key(right: str, strike: float) -> str:
    return f"{right}:{int(strike)}"
