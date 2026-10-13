"""Live market feed is not available in the public replay desk."""

from __future__ import annotations

from datetime import datetime

from iron_fly.config import Settings
from iron_fly.domain.market import ChainSnapshot, DeskMarks


def last_spx(_settings: Settings | None = None) -> float | None:
    return None


def read_spx(
    _settings: Settings,
    _conn,
    _clock: datetime | None = None,
) -> DeskMarks | None:
    return None


def read_chain(
    _settings: Settings,
    _conn,
    _clock: datetime | None = None,
) -> tuple[ChainSnapshot | None, str | None]:
    return None, None
