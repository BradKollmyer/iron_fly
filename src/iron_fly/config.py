from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet

PUBLIC_REQUIRED = ("APP_BASE_URL", "SESSION_SECRET")
MODES = ("backtest-public",)


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    app_base_url: str
    session_secret: str
    secrets_key: str
    data_dir: Path
    desk_mode: str = "backtest-public"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def https(self) -> bool:
        return self.app_base_url.startswith("https://")

    @property
    def callback_url(self) -> str:
        return f"{self.app_base_url}/auth/callback"

    @property
    def is_public(self) -> bool:
        return self.desk_mode == "backtest-public"

    @property
    def is_backtest(self) -> bool:
        return self.desk_mode in MODES

    @classmethod
    def from_env(cls) -> Settings:
        mode = os.environ.get("DESK_MODE", "backtest-public").strip().lower() or "backtest-public"
        missing = [name for name in PUBLIC_REQUIRED if not os.environ.get(name, "").strip()]
        if missing:
            raise ConfigError("missing env: " + ", ".join(missing))
        return cls.from_values(
            app_base_url=os.environ["APP_BASE_URL"].strip(),
            session_secret=os.environ["SESSION_SECRET"].strip(),
            secrets_key=os.environ.get("SECRETS_KEY", "").strip(),
            data_dir=Path(os.environ.get("DATA_DIR", "data").strip() or "data"),
            desk_mode=mode,
        )

    @classmethod
    def from_values(
        cls,
        *,
        app_base_url: str,
        session_secret: str,
        secrets_key: str,
        data_dir: Path,
        desk_mode: str = "backtest-public",
        **_ignored: object,
    ) -> Settings:
        base = app_base_url.rstrip("/")
        if not (base.startswith("http://") or base.startswith("https://")):
            raise ConfigError("APP_BASE_URL must start with http:// or https://")
        mode = (desk_mode or "backtest-public").strip().lower()
        if mode not in MODES:
            raise ConfigError("DESK_MODE must be " + ", ".join(MODES))
        if not secrets_key:
            secrets_key = Fernet.generate_key().decode()
        try:
            Fernet(secrets_key.encode())
        except (ValueError, TypeError) as exc:
            raise ConfigError("SECRETS_KEY is not a Fernet key") from exc
        return cls(
            app_base_url=base,
            session_secret=session_secret,
            secrets_key=secrets_key,
            data_dir=data_dir,
            desk_mode=mode,
        )
