from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from iron_fly import desk, orders
from iron_fly.config import Settings
from iron_fly.db import connect, migrate
from iron_fly.domain.market import ChainSnapshot, DeskMarks, StrikeQuote
from iron_fly.strategy.spec import parse_spec

WHEN = datetime(2026, 10, 22, 15, 40, tzinfo=ZoneInfo("America/New_York"))


@pytest.fixture
def session(tmp_path):
    settings = Settings.from_values(
        app_base_url="http://localhost:8000", session_secret="test", secrets_key="", data_dir=tmp_path
    )
    conn = connect(settings.db_path)
    migrate(conn)
    desk.apply_spec(conn, parse_spec("fee_per_contract: 1.22\n"))
    yield conn, settings
    conn.close()


def chain(target_ask=0.45):
    return ChainSnapshot(
        WHEN,
        WHEN.date(),
        (
            StrikeQuote(7660, 1.10, 1.30, 0.05, 0.10),
            StrikeQuote(7685, 0.35, target_ask, 0.05, 0.10),
        ),
    )


def park(conn, strike=7660, quantity=2):
    orders.park_lots(conn, [{"occ": f"call:{strike}", "right": "call", "strike": strike, "quantity": quantity}])


def frame(conn, settings, quotes=None):
    return desk.watch_frame(conn, settings, DeskMarks(ts=WHEN, chain=quotes))


def test_sale_credits_quantity_scaled_proceeds_and_fees_once(session):
    conn, settings = session
    park(conn)
    assert orders.close_leftover_long(conn, settings, chain(), "call:7660", WHEN) is None
    assert orders.close_leftover_long(conn, settings, chain(), "call:7660", WHEN) == "missing"
    # Two contracts at $110 each, less two $1.22 fees.
    assert frame(conn, settings)["pnl"] * 100 == pytest.approx(217.56)
    assert conn.execute("SELECT COUNT(*) FROM parked_cashflow").fetchone()[0] == 1
    conn.commit()
    migrate(conn)
    assert frame(conn, settings)["pnl"] * 100 == pytest.approx(217.56)


def test_roll_books_bid_minus_ask_and_both_fees_then_sale(session):
    conn, settings = session
    park(conn)
    lot = orders.leftover_long_rows(conn)[0]
    assert orders._roll_leftover(conn, settings, chain(), lot, 7685, WHEN, 1.20)
    # (1.10 - .45) * 2 * $100 - four $1.22 fees.
    assert orders.parked_cash_points(conn, str(WHEN.date())) * 100 == pytest.approx(125.12)
    assert frame(conn, settings, chain())["pnl"] * 100 == pytest.approx(205.12)
    assert not orders._roll_leftover(conn, settings, chain(), lot, 7685, WHEN, 1.20)
    assert orders.close_leftover_long(conn, settings, chain(), "call:7685", WHEN) is None
    assert frame(conn, settings)["pnl"] * 100 == pytest.approx(192.68)


def test_roll_requires_an_executable_target_ask(session):
    conn, settings = session
    park(conn)
    lot = orders.leftover_long_rows(conn)[0]
    assert not orders._roll_leftover(conn, settings, chain(None), lot, 7685, WHEN, 1.20)
    assert orders.leftover_long_rows(conn)[0]["occ"] == "call:7660"
    assert orders.parked_cash_points(conn, str(WHEN.date())) == 0


@pytest.mark.parametrize("roll", [False, True])
def test_cash_record_failure_restores_inventory(session, monkeypatch, roll):
    conn, settings = session
    park(conn)

    def fail(*_):
        raise RuntimeError("cash write failed")

    monkeypatch.setattr(orders, "_record_parked_cash", fail)
    with pytest.raises(RuntimeError, match="cash write failed"):
        if roll:
            orders._roll_leftover(conn, settings, chain(), orders.leftover_long_rows(conn)[0], 7685, WHEN)
        else:
            orders.close_leftover_long(conn, settings, chain(), "call:7660", WHEN)
    assert orders.leftover_long_rows(conn)[0]["occ"] == "call:7660"


def test_parked_long_retains_intrinsic_value_at_settlement(session, monkeypatch):
    conn, settings = session
    park(conn, 7685, 1)
    monkeypatch.setattr(desk, "_cash_close", lambda _: 7690.0)
    result = desk.watch_frame(conn, settings, DeskMarks(ts=WHEN.replace(hour=16, minute=5)))
    assert result["parked_value_points"] == 5
    assert result["pnl"] == 5
    assert frame(conn, settings)["pnl"] is None  # Missing pre-close quotes are not zero value.


def test_cash_is_scoped_to_player_day_and_clear_preserves_previous_day(session):
    conn, settings = session
    park(conn, quantity=1)
    orders.close_leftover_long(conn, settings, chain(), "call:7660", WHEN)
    owner = orders.parked_cash_points(conn, str(WHEN.date()))
    token = desk.bind_player("other")
    try:
        assert orders.parked_cash_points(conn, str(WHEN.date())) == 0
    finally:
        desk.reset_player(token)
    desk._archive_flies(conn, str(WHEN.date()))
    assert orders.parked_cash_points(conn, str(WHEN.date())) == owner
    assert orders.parked_cash_points(conn, "2026-10-23") == 0
    conn.execute("UPDATE desk_state SET book_day = '2026-10-23' WHERE player_id = 'owner'")
    park(conn, quantity=1)
    orders.close_leftover_long(conn, settings, chain(), "call:7660", WHEN.replace(day=23))
    desk.clear_flies(conn)
    assert orders.parked_cash_points(conn, "2026-10-23") == 0
    assert orders.parked_cash_points(conn, str(WHEN.date())) == owner


def test_archive_retains_known_parked_wing_expiry_proceeds_once(session):
    conn, _settings = session
    park(conn, 7685, 1)
    desk._save_cash_close(conn, str(WHEN.date()), 7690)
    desk._archive_flies(conn, str(WHEN.date()))
    assert orders.leftover_long_rows(conn) == []
    assert orders.parked_cash_points(conn, str(WHEN.date())) == 5
    desk._archive_flies(conn, str(WHEN.date()))
    assert orders.parked_cash_points(conn, str(WHEN.date())) == 5
