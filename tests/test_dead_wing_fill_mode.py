from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from iron_fly.config import Settings
from iron_fly.db import connect, migrate
from iron_fly.desk import _close_open_fly, _fill_intent, apply_spec
from iron_fly.domain.market import ChainSnapshot, StrikeQuote
from iron_fly.domain.models import Intent, IntentKind
from iron_fly.orders import leftover_long_rows
from iron_fly.strategy.dale_og import DaleOg
from iron_fly.strategy.spec import parse_spec

WHEN = datetime(2026, 10, 22, 15, 50, tzinfo=ZoneInfo("America/New_York"))


@pytest.mark.parametrize("manual", [False, True])
@pytest.mark.parametrize("mode, expected", [("mid", 19.60), ("ask", 21.60)])
def test_dead_wing_close_obeys_fill_mode(tmp_path, manual, mode, expected):
    settings = Settings.from_values(
        app_base_url="http://localhost:8000", session_secret="test", secrets_key="", data_dir=tmp_path
    )
    conn = connect(settings.db_path)
    migrate(conn)
    apply_spec(conn, parse_spec(f"fill_mode_close: {mode}\n"))
    chain = ChainSnapshot(
        WHEN,
        WHEN.date(),
        (
            StrikeQuote(7650, 0.2, 0.4, 0, 0.05),
            StrikeQuote(7700, 10, 12, 8, 10),
            StrikeQuote(7750, 0.4, 0.6, 0.3, 0.5),
        ),
    )
    strategy = DaleOg()
    fly = strategy.book.open_fly(7700, 18, 50, WHEN.replace(minute=45))
    if manual:
        assert _close_open_fly(conn, settings, strategy.book, fly, chain, WHEN) is None
    else:
        intent = Intent(IntentKind.CLOSE, "expiration breakeven", {"strike": 7700})
        assert _fill_intent(conn, settings, WHEN.date(), strategy, intent, WHEN, 50, chain)
    # Buy both shorts on the configured side; sell the live call wing at .40.
    assert fly.close_debit == pytest.approx(expected)
    assert [(lot["right"], lot["strike"]) for lot in leftover_long_rows(conn)] == [("put", 7650)]
    assert fly.sales == [{"right": "call", "strike": 7750.0, "bid": 0.4}]
    conn.close()
