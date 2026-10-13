"""Chain marks used to price a fly."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True)
class StrikeQuote:
    strike: float
    call_bid: float | None = None
    call_ask: float | None = None
    put_bid: float | None = None
    put_ask: float | None = None
    call_symbol: str = ""
    put_symbol: str = ""


@dataclass(frozen=True)
class ChainSnapshot:
    ts: datetime | None
    expiration: date | None
    strikes: tuple[StrikeQuote, ...]


@dataclass
class DeskMarks:
    last: float | None = None
    bid: float | None = None
    ask: float | None = None
    ts: datetime | None = None
    chain: ChainSnapshot | None = None
    em_open: float | None = None
    em_now: float | None = None
    em_pct: float | None = None
    put_40_width: float | None = None
    tape: list[dict[str, float]] | None = None
    error: str | None = None
    source: str = ""

    def __post_init__(self) -> None:
        if self.tape is None:
            self.tape = []


def mid(bid: float | None, ask: float | None) -> float | None:
    if bid is None or ask is None or bid < 0 or ask <= 0:
        return None
    return (float(bid) + float(ask)) / 2


def _row(chain: ChainSnapshot, strike: float) -> StrikeQuote | None:
    for row in chain.strikes:
        if row.strike == strike:
            return row
    return None


def _px(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.2f}"


def _side(row: StrikeQuote | None, strike: float, right: str) -> str:
    name = int(row.strike) if row is not None else int(strike)
    if row is None:
        return f"{name}{right} —/—"
    bid = row.call_bid if right == "C" else row.put_bid
    ask = row.call_ask if right == "C" else row.put_ask
    return f"{name}{right} {_px(bid)}/{_px(ask)}"


def leg_book(chain: ChainSnapshot | None, strike: float, wings: int) -> str:
    """Bid/ask for the short call, short put, call wing, and put wing."""
    center = _row(chain, strike) if chain is not None else None
    call_wing = _row(chain, strike + wings) if chain is not None else None
    put_wing = _row(chain, strike - wings) if chain is not None else None
    return " ".join(
        (
            _side(center, strike, "C"),
            _side(center, strike, "P"),
            _side(call_wing, strike + wings, "C"),
            _side(put_wing, strike - wings, "P"),
        )
    )


def straddle_book(chain: ChainSnapshot | None, strike: float) -> str:
    """Bid/ask for the call and put at one strike."""
    row = _row(chain, strike) if chain is not None else None
    return f"{_side(row, strike, 'C')} {_side(row, strike, 'P')}"


def combo_mark(chain: ChainSnapshot | None, strike: float, wings: int) -> float | None:
    """Credit to open, or debit to close, the same 4-leg fly. Shorts minus wings."""
    legs = _combo_legs(chain, strike, wings)
    if legs is None:
        return None
    short_call, short_put, long_call, long_put = legs
    return round(short_call + short_put - long_call - long_put, 2)


def combo_quote(chain: ChainSnapshot | None, strike: float, wings: int) -> tuple[float, float, float] | None:
    """Combo credit bid, mid, and ask. Bid sells the shorts and lifts the wings."""
    if chain is None:
        return None
    center = _row(chain, strike)
    put_wing = _row(chain, strike - wings)
    call_wing = _row(chain, strike + wings)
    if center is None or put_wing is None or call_wing is None:
        return None
    prices = (
        center.call_bid,
        center.call_ask,
        center.put_bid,
        center.put_ask,
        call_wing.call_bid,
        call_wing.call_ask,
        put_wing.put_bid,
        put_wing.put_ask,
    )
    if any(price is None for price in prices):
        return None
    mark = combo_mark(chain, strike, wings)
    if mark is None:
        return None
    bid = round(float(center.call_bid) + float(center.put_bid) - float(call_wing.call_ask) - float(put_wing.put_ask), 2)
    ask = round(float(center.call_ask) + float(center.put_ask) - float(call_wing.call_bid) - float(put_wing.put_bid), 2)
    return bid, mark, ask


def combo_fill(
    chain: ChainSnapshot | None,
    strike: float,
    wings: int,
    *,
    side: str,
    mode: str,
) -> float | None:
    """Book price for an open or close. Falls back to mid when the quote is thin.

    ``side`` is ``open`` or ``close`` (documentation only). ``mode`` is ``mid``,
    ``bid``, or ``ask``. Open+bid is the worst STO credit; close+ask is the
    worst BTC debit.
    """
    del side
    if mode != "bid" and mode != "ask":
        return combo_mark(chain, strike, wings)
    quote = combo_quote(chain, strike, wings)
    if quote is None:
        return combo_mark(chain, strike, wings)
    bid, _mark, ask = quote
    return bid if mode == "bid" else ask


def _combo_legs(chain: ChainSnapshot | None, strike: float, wings: int) -> tuple[float, float, float, float] | None:
    if chain is None:
        return None
    center = _row(chain, strike)
    put_wing = _row(chain, strike - wings)
    call_wing = _row(chain, strike + wings)
    if center is None or put_wing is None or call_wing is None:
        return None
    short_call = mid(center.call_bid, center.call_ask)
    short_put = mid(center.put_bid, center.put_ask)
    long_call = mid(call_wing.call_bid, call_wing.call_ask)
    long_put = mid(put_wing.put_bid, put_wing.put_ask)
    if None in (short_call, short_put, long_call, long_put):
        return None
    return float(short_call), float(short_put), float(long_call), float(long_put)


def straddle_mid(chain: ChainSnapshot | None, strike: float) -> float | None:
    if chain is None:
        return None
    row = _row(chain, strike)
    if row is None:
        return None
    call = mid(row.call_bid, row.call_ask)
    put = mid(row.put_bid, row.put_ask)
    if call is None or put is None:
        return None
    return round(call + put, 2)


def wing_choice(chain: ChainSnapshot | None, atm: float) -> float | None:
    """50 while the 50-wide put is still $0.40. Otherwise the $0.40 width, and 20 once that put is gone."""
    if chain is None or not chain.strikes:
        return None
    above: list[float] = []
    saw = False
    for row in chain.strikes:
        width = atm - row.strike
        if width < 20:
            continue
        mark = mid(row.put_bid, row.put_ask)
        if mark is None:
            continue
        saw = True
        if mark >= 0.40:
            above.append(width)
    fifty = _row(chain, atm - 50)
    fifty_mid = None if fifty is None else mid(fifty.put_bid, fifty.put_ask)
    if fifty_mid is not None and fifty_mid >= 0.40:
        return 50.0
    if above:
        return min(50.0, max(above))
    if saw:
        return 20.0
    return None


def put40_width(chain: ChainSnapshot | None, atm: float) -> float | None:
    """Points from ATM to the put whose mid is $0.40.

    The strike is interpolated when the chain brackets $0.40. If the stored
    wing is still richer than $0.40, the distance is extended on the log slope
    of the outer puts.
    """
    if chain is None:
        return None
    puts: list[tuple[float, float]] = []
    for row in chain.strikes:
        width = atm - row.strike
        if width < 20:
            continue
        mark = mid(row.put_bid, row.put_ask)
        if mark is None or mark <= 0:
            continue
        puts.append((width, mark))
    if not puts:
        return None
    puts.sort(key=lambda item: item[0])
    rich = [item for item in puts if item[1] >= 0.40]
    cheap = [item for item in puts if item[1] < 0.40]
    if rich and cheap:
        return _width_at_price(rich[-1], cheap[0])
    if len(rich) >= 2:
        return _width_at_price(rich[-2], rich[-1])
    if rich:
        return rich[-1][0]
    return min(puts, key=lambda item: abs(item[1] - 0.40))[0]


def _width_at_price(near: tuple[float, float], far: tuple[float, float]) -> float:
    """Width where the put mid, on a log line between two strikes, is $0.40."""
    width_near, mark_near = near
    width_far, mark_far = far
    if mark_near <= 0 or mark_far <= 0 or width_far <= width_near or mark_near <= mark_far:
        return width_far
    slope = (math.log(mark_far) - math.log(mark_near)) / (width_far - width_near)
    if slope >= -1e-9:
        return width_far
    width = width_far + (math.log(0.40) - math.log(mark_far)) / slope
    if width < min(width_near, width_far):
        return min(width_near, width_far)
    return float(5 * round(width / 5))


def downsample(points: list[dict[str, float]], limit: int = 480) -> list[dict[str, float]]:
    if len(points) <= limit:
        return points
    if limit < 2:
        return points[:1]
    step = (len(points) - 1) / (limit - 1)
    picked = [points[round(i * step)] for i in range(limit)]
    return picked
