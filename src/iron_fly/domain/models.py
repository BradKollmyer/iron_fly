"""Shim: iron_fly.domain.models → iron_fly_strategy.models (one IntentKind identity)."""

from __future__ import annotations

import sys

from iron_fly_strategy import models as _impl

sys.modules[__name__] = _impl
