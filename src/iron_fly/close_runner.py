"""Live close runner is not available in the public replay desk."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from iron_fly.config import Settings


def clear() -> None:
    return None


def is_closing(_strike: float) -> bool:
    return False


def start_close(
    *,
    settings: Settings,
    strike: float,
    wings: int,
    chain,
    clock: datetime,
    fill_ms: int,
    finish: Callable[..., None],
) -> str | None:
    return "Live closes are off"
