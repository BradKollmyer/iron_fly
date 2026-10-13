"""Shim: iron_fly.strategy.config_state → iron_fly_strategy.config_state."""

from __future__ import annotations

import sys

from iron_fly_strategy import config_state as _impl

sys.modules[__name__] = _impl
