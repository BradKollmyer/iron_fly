"""Config slippage worsens a backtest fill. A later pick replaces it."""

from datetime import datetime, time
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet

from iron_fly.config import Settings
from iron_fly.db import connect, migrate
from iron_fly.desk import _fill_intent, apply_spec
from iron_fly.domain.market import ChainSnapshot, StrikeQuote, combo_fill
from iron_fly.domain.models import Intent, IntentKind
from iron_fly.strategy.config_state import slippage_now
from iron_fly.strategy.dale_og import DaleOg
from iron_fly.strategy.spec import parse_spec

ET = ZoneInfo("America/New_York")

SPEC = """\
slippage: 0.5
fill_mode_close: ask
picks:
  - at_time: "15:44"
    stop: 2
    slippage: 1.1
"""


def _strike(strike: float, call: tuple[float, float], put: tuple[float, float]) -> StrikeQuote:
    return StrikeQuote(strike, call[0], call[1], put[0], put[1], f"C{int(strike)}", f"P{int(strike)}")


def _chain() -> ChainSnapshot:
    return ChainSnapshot(
        ts=None,
        expiration=None,
        strikes=(
            _strike(7650, (0.2, 0.4), (0.4, 0.6)),
            _strike(7700, (10, 12), (8, 10)),
            _strike(7750, (0.4, 0.6), (0.3, 0.5)),
        ),
    )


def test_pick_slippage_replaces_the_desk_value_after_its_clock():
    spec = parse_spec(SPEC)
    assert slippage_now(spec, wall=time(15, 0)) == 0.5
    assert slippage_now(spec, wall=time(15, 44)) == 1.1


def test_backtest_close_adds_the_active_slippage(tmp_path):
    settings = Settings.from_values(
        app_base_url="http://localhost:8000",
        session_secret="test-session-secret",
        secrets_key=Fernet.generate_key().decode(),
        data_dir=tmp_path,
        desk_mode="backtest-public",
    )
    conn = connect(settings.db_path)
    migrate(conn)
    apply_spec(conn, parse_spec(SPEC))
    chain = _chain()
    strategy = DaleOg()
    strategy.book.open_fly(7700, 18.0, 50, datetime(2026, 10, 14, 10, 40, tzinfo=ET))
    ask = combo_fill(chain, 7700, 50, side="close", mode="ask")
    when = datetime(2026, 10, 14, 15, 50, tzinfo=ET)
    intent = Intent(IntentKind.CLOSE, "stop", {"strike": 7700.0, "wings": 50})
    assert _fill_intent(conn, settings, when.date(), strategy, intent, when, 50, chain) is True
    assert float(intent.payload["debit"]) == round(float(ask) + 1.1, 2)
    closed = [fly for fly in strategy.book.flies if fly.strike == 7700]
    assert float(closed[0].close_debit) == round(float(ask) + 1.1, 2)
    conn.close()


def test_backtest_open_subtracts_the_active_slippage(tmp_path):
    settings = Settings.from_values(
        app_base_url="http://localhost:8000",
        session_secret="test-session-secret",
        secrets_key=Fernet.generate_key().decode(),
        data_dir=tmp_path,
        desk_mode="backtest-public",
    )
    conn = connect(settings.db_path)
    migrate(conn)
    apply_spec(conn, parse_spec(SPEC))
    chain = _chain()
    strategy = DaleOg()
    raw = combo_fill(chain, 7700, 50, side="open", mode="mid")
    when = datetime(2026, 10, 14, 15, 50, tzinfo=ET)
    intent = Intent(IntentKind.ADD, "watch tagged", {"strike": 7700.0, "wings": 50})
    assert _fill_intent(conn, settings, when.date(), strategy, intent, when, 50, chain) is True
    assert float(intent.payload["credit"]) == round(float(raw) - 1.1, 2)
    opened = [fly for fly in strategy.book.flies if fly.strike == 7700]
    assert float(opened[0].credit) == round(float(raw) - 1.1, 2)
    conn.close()


def test_backtest_fee_is_one_dollar_on_each_of_four_legs(tmp_path):
    settings = Settings.from_values(
        app_base_url="http://localhost:8000",
        session_secret="test-session-secret",
        secrets_key=Fernet.generate_key().decode(),
        data_dir=tmp_path,
        desk_mode="backtest-public",
    )
    conn = connect(settings.db_path)
    migrate(conn)
    apply_spec(conn, parse_spec("fee_per_contract: 1\nfill_mode_close: ask\n"))
    chain = _chain()
    strategy = DaleOg()
    raw = combo_fill(chain, 7700, 50, side="open", mode="mid")
    when = datetime(2026, 10, 14, 10, 50, tzinfo=ET)
    opened = Intent(IntentKind.ADD, "watch tagged", {"strike": 7700.0, "wings": 50})
    assert _fill_intent(conn, settings, when.date(), strategy, opened, when, 50, chain) is True
    assert float(opened.payload["credit"]) == round(float(raw) - 0.04, 2)
    ask = combo_fill(chain, 7700, 50, side="close", mode="ask")
    closed = Intent(IntentKind.CLOSE, "stop", {"strike": 7700.0, "wings": 50})
    assert _fill_intent(conn, settings, when.date(), strategy, closed, when, 50, chain) is True
    assert float(closed.payload["debit"]) == round(float(ask) + 0.04, 2)
    conn.close()
