"""Shim: iron_fly.strategy.spec → iron_fly_strategy.spec."""

from __future__ import annotations

import sys

from iron_fly_strategy import spec as _impl

sys.modules[__name__] = _impl
