"""Backtest must refuse opens when a wing quote is missing (no 0.01 STO)."""

from __future__ import annotations

import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet

from iron_fly.config import Settings
from iron_fly.db import connect, migrate
from iron_fly.desk import _fill_intent, load_book, open_strike
from iron_fly.domain.market import ChainSnapshot, DeskMarks, StrikeQuote, combo_mark
from iron_fly.domain.models import Intent, IntentKind
from iron_fly.strategy.dale_og import DaleOg

ET = ZoneInfo("America/New_York")


def _strike(
    strike: float,
    call: tuple[float, float],
    put: tuple[float, float],
) -> StrikeQuote:
    return StrikeQuote(strike, call[0], call[1], put[0], put[1], "", "")


def _settings(tmp_path) -> Settings:
    return Settings.from_values(
        app_base_url="http://localhost:8000",
        session_secret="test-session-secret",
        secrets_key=Fernet.generate_key().decode(),
        data_dir=tmp_path,
        desk_mode="backtest-public",
    )


def test_backtest_refuses_open_without_combo_mid_then_retries(tmp_path, caplog):
    """Missing wing quote must not book a penny STO; later tick opens when quoted."""
    thin = ChainSnapshot(
        ts=None,
        expiration=None,
        strikes=(
            _strike(7700, (10, 12), (8, 10)),
            _strike(7750, (0.4, 0.6), (0.3, 0.5)),
        ),
    )
    full = ChainSnapshot(
        ts=None,
        expiration=None,
        strikes=(
            _strike(7650, (0.2, 0.4), (0.4, 0.6)),
            _strike(7700, (10, 12), (8, 10)),
            _strike(7750, (0.4, 0.6), (0.3, 0.5)),
        ),
    )
    when = datetime(2026, 10, 14, 10, 50, tzinfo=ET)
    settings = _settings(tmp_path)
    conn = connect(settings.db_path)
    migrate(conn)
    marks = DeskMarks(last=7747, chain=thin, ts=when, source="replay")
    assert open_strike(conn, settings, marks, 7700, wings=50) == "nomid"
    assert conn.execute("SELECT COUNT(*) AS n FROM fly").fetchone()["n"] == 0

    strategy = DaleOg()
    strategy.book = load_book(conn)
    strategy.book.open_fly(7750, 21.0, 50, when.replace(minute=41))
    intent = Intent(IntentKind.ADD, "watch tagged", {"strike": 7700.0, "wings": 50})
    with caplog.at_level(logging.WARNING, logger="iron_fly.step"):
        assert (
            _fill_intent(conn, settings, date(2026, 10, 14), strategy, intent, when, 50, thin)
            is False
        )
    assert any("no quotes" in rec.message and "retry" in rec.message for rec in caplog.records)
    assert intent.payload.get("credit") in (None, 0, 0.0) or "credit" not in intent.payload
    active = [fly for fly in strategy.book.flies if fly.status == "active"]
    assert [(fly.strike, fly.credit) for fly in active] == [(7750, 21.0)]

    later = Intent(IntentKind.ADD, "watch tagged", {"strike": 7700.0, "wings": 50})
    assert (
        _fill_intent(
            conn, settings, date(2026, 10, 14), strategy, later, when.replace(second=5), 50, full
        )
        is True
    )
    assert float(later.payload["credit"]) == combo_mark(full, 7700, 50)
    opened = [fly for fly in strategy.book.flies if fly.strike == 7700 and fly.status == "active"]
    assert len(opened) == 1
    assert float(opened[0].credit) == combo_mark(full, 7700, 50)
    conn.close()
