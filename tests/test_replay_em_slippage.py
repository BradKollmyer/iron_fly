from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from iron_fly import desk
from iron_fly.config import Settings
from iron_fly.db import connect, migrate
from iron_fly.domain.market import ChainSnapshot, DeskMarks, StrikeQuote
from iron_fly.strategy.spec import parse_spec


@pytest.mark.parametrize("trigger", ["at_em_pct: 50", "at_em: 25"])
@pytest.mark.parametrize("missing", [False, True])
def test_replay_fills_use_current_em_and_clear_missing_readings(tmp_path, monkeypatch, trigger, missing):
    when = datetime(2026, 10, 20, 10, 40, tzinfo=ZoneInfo("America/New_York"))
    settings = Settings.from_values(
        app_base_url="http://localhost:8000", session_secret="test", secrets_key="", data_dir=tmp_path
    )
    conn = connect(settings.db_path)
    migrate(conn)
    desk.apply_spec(
        conn,
        parse_spec(
            'f1_at: "10:40"\nwings: 50\nregime: "±20/25"\nclose_fill_ms: 0\n'
            f"fill_mode_open: bid\nfill_mode_close: ask\npicks:\n  - {trigger}\n    slippage: 1\n"
        ),
    )
    desk.set_switches(conn, True, True, True)
    conn.execute("UPDATE desk_state SET em_pct = 1, em_now = 1")
    chain = ChainSnapshot(
        when,
        when.date(),
        (
            StrikeQuote(7650, 0.2, 0.4, 0.4, 0.6),
            StrikeQuote(7700, 10, 12, 8, 10),
            StrikeQuote(7750, 0.4, 0.6, 0.3, 0.5),
        ),
    )
    tape = [
        {"time": int(when.timestamp()), "value": 7700, "em": None if missing else 20, "pct": None if missing else 40},
        {"time": int(when.timestamp()) + 1, "value": 7720, "em": 30, "pct": 60},
    ]
    monkeypatch.setattr(desk, "load_day_tape", lambda *_: DeskMarks(tape=tape))
    monkeypatch.setattr(desk, "_spx_slice", lambda *_: None)
    monkeypatch.setattr(desk, "chain_asof", lambda *_: chain)
    desk.replay_until(conn, settings, when.date(), when)
    fly = desk.load_book(conn).flies[0]
    # Bid credit 16.80. The first reading activates the overlay unless missing.
    assert fly.credit == pytest.approx(16.80 if missing else 15.80)
    desk.replay_until(conn, settings, when.date(), when + timedelta(seconds=1))
    closed = desk.load_book(conn).flies[0]
    assert closed.status == "closed"
    # Recovered EM turns off the overlay at exit: 12 + 10 - .4 - .4.
    assert closed.close_debit == pytest.approx(21.20)
    conn.close()
