"""Shim: iron_fly.strategy.rules → iron_fly_strategy.rules."""

from __future__ import annotations

import sys

from iron_fly_strategy import rules as _impl

sys.modules[__name__] = _impl
