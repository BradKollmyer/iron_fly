"""OCC helpers for synthetic SPXW symbols."""

from __future__ import annotations

from datetime import date


def spxw_symbol(expiration: date, right: str, strike: float) -> str:
    """0DTE SPXW symbol from expiration + strike."""
    side = "C" if right == "call" else "P"
    millis = int(round(float(strike) * 1000))
    return f"SPXW{expiration.strftime('%y%m%d')}{side}{millis:08d}"
