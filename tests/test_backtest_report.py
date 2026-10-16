"""Daily backtest lines include the session open EM and the EM at each fly open."""

from datetime import datetime
from zoneinfo import ZoneInfo

from iron_fly.backtest import _report

_ET = ZoneInfo("America/New_York")


def test_report_prints_open_em_and_the_reading_at_each_fly_open():
    first = datetime(2026, 9, 8, 15, 40, 2, tzinfo=_ET)
    second = datetime(2026, 9, 8, 15, 42, 10, tzinfo=_ET)
    tape = [
        {"time": int(datetime(2026, 9, 8, 9, 30, 2, tzinfo=_ET).timestamp()), "em": 19.12, "pct": 83.5},
        {"time": int(first.timestamp()), "em": 5.82, "pct": 25.4},
        {"time": int(second.timestamp()), "em": 5.5, "pct": 24.0},
    ]
    frame = {
        "em_open": 22.9,
        "pnl": -9.18,
        "flies": [
            {
                "time_open": "15:40",
                "time_close": "15:50",
                "strike": 7685,
                "wings": 20,
                "status": "closed",
                "sto": 5.45,
                "pnl": -3.89,
            },
            {
                "time_open": "15:42",
                "time_close": "15:54",
                "strike": 7680,
                "wings": 20,
                "status": "closed",
                "sto": 5.80,
                "pnl": -3.75,
            },
        ],
    }
    text = _report(
        first.date(),
        "Desk auto",
        frame,
        settled=True,
        tape=tape,
        opened=[first, second],
    )
    assert text.splitlines()[0].endswith("open EM 22.9")
    assert "em   5.82  em%  25.4  pnl -389" in text
    assert "em    5.5  em%    24  pnl -375" in text
