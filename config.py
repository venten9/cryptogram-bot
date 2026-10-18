"""Configuration module using Pydantic Settings for Cooperative Cryptogram Bot."""
from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables and .env file.

    Fields:
        BOT_TOKEN: Telegram Bot API Token obtained from @BotFather.
                   Default is empty to allow headless offline tests & simulations.
        DB_PATH: SQLAlchemy connection URL or SQLite file path.
        BOT_USERNAME: Bot username for deep link invite generation (without @).
        DEBUG: Flag enabling verbose debug logs and SQLAlchemy query echoing.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    BOT_TOKEN: str = ""
    DB_PATH: str = "sqlite+aiosqlite:///cryptogram.db"
    BOT_USERNAME: Optional[str] = "SpyAnalystBot"
    DEBUG: bool = False

    @property
    def is_token_configured(self) -> bool:
        """Check whether a non-empty BOT_TOKEN is present and not a dummy placeholder."""
        return bool(
            self.BOT_TOKEN
            and self.BOT_TOKEN.strip()
            and not self.BOT_TOKEN.strip().startswith("YOUR_")
        )

    @property
    def effective_bot_username(self) -> str:
        """Return sanitized bot username without leading '@', falling back to 'SpyAnalystBot'."""
        if self.BOT_USERNAME and self.BOT_USERNAME.strip():
            return self.BOT_USERNAME.strip().lstrip("@")
        return "SpyAnalystBot"

    @property
    def db_url(self) -> str:
        """Return standardized SQLAlchemy async SQLite connection URL."""
        if "://" not in self.DB_PATH:
            return f"sqlite+aiosqlite:///{self.DB_PATH}"
        return self.DB_PATH

    @property
    def bot_token(self) -> str:
        """Ergonomic lowercase alias for BOT_TOKEN."""
        return self.BOT_TOKEN

    @property
    def db_path(self) -> str:
        """Ergonomic lowercase alias for DB_PATH."""
        return self.DB_PATH

    @property
    def bot_username(self) -> Optional[str]:
        """Ergonomic lowercase alias for BOT_USERNAME."""
        return self.BOT_USERNAME

    @property
    def debug(self) -> bool:
        """Ergonomic lowercase alias for DEBUG."""
        return self.DEBUG

    def validate_for_bot_polling(self) -> None:
        """Validate that essential parameters required for live Telegram polling are set.

        Raises:
            ValueError: If BOT_TOKEN is empty or missing.
        """
        if not self.is_token_configured:
            raise ValueError(
                "BOT_TOKEN is required for live Telegram bot polling. "
                "Please set BOT_TOKEN in .env file or as an environment variable."
            )


import threading

_settings_lock = threading.Lock()
_cached_settings: Optional[Settings] = None


def get_settings() -> Settings:
    """Return cached singleton instance of application settings.

    To clear the cache during test execution:
        get_settings.cache_clear()
    """
    global _cached_settings
    if _cached_settings is None:
        with _settings_lock:
            if _cached_settings is None:
                _cached_settings = Settings()
    return _cached_settings


def _clear_settings_cache() -> None:
    """Thread-safe cache invalidation for testing environments."""
    global _cached_settings
    with _settings_lock:
        _cached_settings = None


get_settings.cache_clear = _clear_settings_cache
