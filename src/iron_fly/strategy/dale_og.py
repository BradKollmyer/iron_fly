"""Shim: iron_fly.strategy.dale_og → iron_fly_strategy.dale_og."""

from __future__ import annotations

import sys

from iron_fly_strategy import dale_og as _impl

sys.modules[__name__] = _impl
