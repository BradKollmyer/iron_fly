"""CollectorClient bake helpers (no live streamer)."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx

from iron_fly.quotes.collector import CollectorClient

ET = ZoneInfo("America/New_York")


def test_session_days_sorted():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/days"
        return httpx.Response(200, json={"spx": ["2026-10-13", "2026-10-10"]})

    client = CollectorClient("http://collector", transport=httpx.MockTransport(handler))
    assert client.session_days() == ["2026-10-10", "2026-10-13"]


def test_day_spx_tape_skips_chain_http():
    day = date(2026, 10, 13)
    start = datetime(2026, 10, 13, 9, 30, tzinfo=ET)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/spx/samples":
            return httpx.Response(
                200,
                json={"samples": [{"ts": start.isoformat(), "last": 7800.0}]},
            )
        if request.url.path == "/v1/session":
            return httpx.Response(200, json={"opening_em": 20.0})
        if request.url.path == "/v1/chain/samples":
            raise AssertionError("bake must not pull chain HTTP")
        return httpx.Response(404)

    client = CollectorClient("http://collector", transport=httpx.MockTransport(handler))
    marks = client.day_spx_tape(day)
    assert marks.error is None
    assert marks.tape
    assert marks.tape[0]["value"] == 7800.0
    assert marks.em_open == 20.0
