"""Shim: iron_fly.strategy.book → iron_fly_strategy.book."""

from __future__ import annotations

import sys

from iron_fly_strategy import book as _impl

sys.modules[__name__] = _impl
