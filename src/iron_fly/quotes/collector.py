"""HTTP client for market_streamer collector (bake SPX tapes only)."""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import httpx

from iron_fly.domain.market import DeskMarks, downsample

_ET = ZoneInfo("America/New_York")
_POOL: dict[str, httpx.Client] = {}


class CollectorClient:
    """GET-only client. Bake uses session_days + day_spx_tape; chains come from Postgres."""

    def __init__(self, base_url: str, token: str = "", transport: httpx.BaseTransport | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._transport = transport

    def session_days(self) -> list[str]:
        body = self._get("/v1/days")
        raw = body.get("spx") if isinstance(body, dict) else None
        days = [item for item in raw if isinstance(item, str)] if isinstance(raw, list) else []
        return sorted(days)

    def day_spx_tape(self, day: date) -> DeskMarks:
        """SPX prints and opening EM for one session. No option-chain HTTP."""
        marks = DeskMarks(source="market_streamer")
        start = datetime.combine(day, time(9, 30), tzinfo=_ET)
        end = datetime.combine(day, time(16, 16), tzinfo=_ET)
        try:
            samples = self._get("/v1/spx/samples", {"start": start.isoformat(), "end": end.isoformat()})
            session = self._get_optional("/v1/session", {"date": day.isoformat()})
        except httpx.HTTPError as exc:
            marks.error = _status_message(exc, "market_streamer")
            return marks
        except (LookupError, ValueError):
            marks.error = f"No SPX tape for {day.isoformat()}"
            return marks
        marks.tape = _tape(samples.get("samples"), limit=None)
        if not marks.tape:
            marks.error = f"No SPX tape for {day.isoformat()}"
            return marks
        marks.last = float(marks.tape[0]["value"])
        marks.ts = datetime.fromtimestamp(marks.tape[0]["time"], tz=_ET)
        if session:
            marks.em_open = _num(session.get("opening_em"))
        return marks

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _get(self, path: str, params: dict[str, str] | None = None) -> dict:
        if not self.base_url:
            raise LookupError("MARKET_STREAMER_URL is not set")
        if self._transport is not None:
            with httpx.Client(timeout=30.0, headers=self._headers(), transport=self._transport) as client:
                return _object(client.get(f"{self.base_url}{path}", params=params))
        pool = _POOL.get(self.base_url)
        if pool is None:
            pool = httpx.Client(timeout=30.0, headers=self._headers(), base_url=self.base_url)
            _POOL[self.base_url] = pool
        try:
            return _object(pool.get(path, params=params))
        except httpx.HTTPError:
            _POOL.pop(self.base_url, None)
            raise

    def _get_optional(self, path: str, params: dict[str, str] | None = None) -> dict | None:
        try:
            return self._get(path, params)
        except (httpx.HTTPError, LookupError, ValueError):
            return None


def _ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_ET)
    return parsed


def _num(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _object(response: httpx.Response) -> dict:
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict):
        raise LookupError("expected a JSON object")
    if body.get("error"):
        raise LookupError(str(body["error"]))
    return body


def _status_message(exc: httpx.HTTPError, name: str) -> str:
    response = getattr(exc, "response", None)
    if response is not None and response.status_code == 401:
        return f"{name} rejected the token"
    return f"{name} is not reachable"


def _tape(samples: object, limit: int | None = 480) -> list[dict[str, float]]:
    by_time: dict[int, float] = {}
    if isinstance(samples, list):
        for item in samples:
            if not isinstance(item, dict):
                continue
            last = _num(item.get("last"))
            when = _ts(item.get("ts"))
            if last is None or when is None:
                continue
            by_time[int(when.timestamp())] = last
    points = [{"time": stamp, "value": by_time[stamp]} for stamp in sorted(by_time)]
    if limit is None:
        return points
    return downsample(points, limit)
