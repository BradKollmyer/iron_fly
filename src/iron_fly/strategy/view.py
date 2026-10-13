"""Shim: iron_fly.strategy.view → iron_fly_strategy.view."""

from __future__ import annotations

import sys

from iron_fly_strategy import view as _impl

sys.modules[__name__] = _impl
