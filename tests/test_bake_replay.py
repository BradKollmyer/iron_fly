"""Remote bake driver helpers (no live docker / postgres required)."""

from __future__ import annotations

import importlib.util
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from iron_fly.quotes.collector import CollectorClient

ET = ZoneInfo("America/New_York")
_BAKE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "bake-replay.py"


def _load_bake():
    spec = importlib.util.spec_from_file_location("bake_replay", _BAKE_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_remote_run_cmd_uses_network_not_exec_copy(monkeypatch):
    bake = _load_bake()
    monkeypatch.setattr(bake, "_DOCKER_CONTEXT", "mac-mini")
    monkeypatch.setattr(bake, "_DOCKER_NETWORK", "data")
    monkeypatch.setattr(bake, "_BAKE_IMAGE", "iron-fly-bake:local")
    monkeypatch.setattr(bake, "_WORKER_STREAMER", "http://collector:8080")
    cmd = bake.remote_run_cmd(
        "iron-fly-bake-1",
        ["2026-10-13"],
        "postgresql://postgres:x@postgres:5432/market",
        "tok",
    )
    joined = " ".join(cmd)
    assert "exec" not in cmd
    assert "\\copy" not in joined
    assert "--network" in cmd
    assert "data" in cmd
    assert "--worker" in cmd
    assert "2026-10-13" in cmd
    assert "MARKET_STREAMER_URL=http://collector:8080" in joined


def test_bake_day_writes_from_record_iterator(tmp_path):
    bake = _load_bake()
    day = date(2026, 10, 13)
    start = datetime(2026, 10, 13, 9, 30, tzinfo=ET)
    stamp = int(start.timestamp())

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

    client = CollectorClient("http://streamer", transport=httpx.MockTransport(handler))
    records = [
        (stamp, 7800.0, 10.0, 12.0, 8.0, 10.0),
        (stamp, 7750.0, 0.2, 0.4, 0.4, 0.6),
    ]
    error = bake.bake_day(day, tmp_path, client, iter(records))
    assert error is None
    path = tmp_path / "2026-10-13.json"
    assert path.is_file()
    assert path.stat().st_size > 100
