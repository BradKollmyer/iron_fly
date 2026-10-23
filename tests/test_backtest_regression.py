"""Lock the 1540-20-market desk to the baked replay tapes.

    make backtest ARGS='config/1540-20-market.yaml --day 2026-10-22'
    make backtest ARGS='config/1540-20-market.yaml --last 30 --skip 2026-09-16,2026-09-30'

Replay files stay out of git. The test skips when that tape set is not on disk.
"""

from datetime import date
from pathlib import Path

import pytest

from iron_fly.backtest import _replay_dates, main

ROOT = Path(__file__).resolve().parents[1]
REPLAY = ROOT / "replay"
CONFIG = ROOT / "config" / "1540-20-market.yaml"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Newest 30 tapes in replay/ when these goldens were recorded, oldest first.
# --skip removes 2026-09-16 and 2026-09-30 from the printed table.
LAST_30 = [
    date(2026, 9, 11),
    date(2026, 9, 14),
    date(2026, 9, 15),
    date(2026, 9, 16),
    date(2026, 9, 17),
    date(2026, 9, 18),
    date(2026, 9, 21),
    date(2026, 9, 22),
    date(2026, 9, 23),
    date(2026, 9, 24),
    date(2026, 9, 25),
    date(2026, 9, 28),
    date(2026, 9, 29),
    date(2026, 9, 30),
    date(2026, 10, 1),
    date(2026, 10, 2),
    date(2026, 10, 5),
    date(2026, 10, 6),
    date(2026, 10, 7),
    date(2026, 10, 8),
    date(2026, 10, 9),
    date(2026, 10, 12),
    date(2026, 10, 13),
    date(2026, 10, 14),
    date(2026, 10, 15),
    date(2026, 10, 16),
    date(2026, 10, 19),
    date(2026, 10, 20),
    date(2026, 10, 21),
    date(2026, 10, 22),
]


def _require_tapes(days: list[date]) -> None:
    missing = [day.isoformat() for day in days if not (REPLAY / f"{day.isoformat()}.json").is_file()]
    if missing:
        pytest.skip("replay tapes are not on disk: " + ", ".join(missing))


def test_1540_20_market_day_matches_2026_10_22(capsys: pytest.CaptureFixture[str]) -> None:
    day = date(2026, 10, 22)
    _require_tapes([day])
    code = main([str(CONFIG), "--day", day.isoformat(), "--replay", str(REPLAY)])
    assert code == 0
    expected = (FIXTURES / "1540-20-market-2026-10-22.txt").read_text()
    assert capsys.readouterr().out == expected


def test_1540_20_market_last_30_matches_the_saved_table(capsys: pytest.CaptureFixture[str]) -> None:
    taped = _replay_dates(REPLAY)
    if taped[-30:] != LAST_30:
        pytest.skip("replay/ is not the 2026-09-11 through 2026-10-22 window these numbers were recorded on")
    _require_tapes(LAST_30)
    code = main(
        [
            str(CONFIG),
            "--last",
            "30",
            "--skip",
            "2026-09-16,2026-09-30",
            "--replay",
            str(REPLAY),
        ]
    )
    assert code == 0
    expected = (FIXTURES / "1540-20-market-last-30.txt").read_text()
    assert capsys.readouterr().out == expected
