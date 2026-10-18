"""Unit and integration tests for application configuration and settings loading."""
import pytest
from config import Settings, get_settings
from database.session import create_sqlite_async_engine
from bot.app import create_dispatcher


@pytest.mark.unit
class TestSettings:
    """Test suite verifying default configuration, environment overrides, and helper methods."""

    def test_default_settings(self, monkeypatch):
        """Verify sensible defaults when no environment variables are present."""
        monkeypatch.delenv("BOT_TOKEN", raising=False)
        monkeypatch.delenv("DB_PATH", raising=False)
        monkeypatch.delenv("BOT_USERNAME", raising=False)
        monkeypatch.delenv("DEBUG", raising=False)
        monkeypatch.delenv("bot_token", raising=False)
        monkeypatch.delenv("db_path", raising=False)
        monkeypatch.delenv("bot_username", raising=False)
        monkeypatch.delenv("debug", raising=False)

        settings = Settings(_env_file=None)
        assert settings.BOT_TOKEN == ""
        assert settings.DB_PATH == "sqlite+aiosqlite:///cryptogram.db"
        assert settings.DEBUG is False
        assert settings.is_token_configured is False
        assert settings.effective_bot_username == "SpyAnalystBot"
        assert settings.db_url == "sqlite+aiosqlite:///cryptogram.db"
        # Test lowercase property aliases
        assert settings.bot_token == ""
        assert settings.db_path == "sqlite+aiosqlite:///cryptogram.db"
        assert settings.debug is False

    def test_env_overrides(self, monkeypatch):
        """Verify environment variables override default settings."""
        monkeypatch.setenv("BOT_TOKEN", "123456:TEST_TOKEN")
        monkeypatch.setenv("DB_PATH", "sqlite+aiosqlite:///custom_games.db")
        monkeypatch.setenv("BOT_USERNAME", "CustomAgentBot")
        monkeypatch.setenv("DEBUG", "true")

        settings = Settings()
        assert settings.BOT_TOKEN == "123456:TEST_TOKEN"
        assert settings.DB_PATH == "sqlite+aiosqlite:///custom_games.db"
        assert settings.BOT_USERNAME == "CustomAgentBot"
        assert settings.DEBUG is True
        assert settings.is_token_configured is True
        assert settings.effective_bot_username == "CustomAgentBot"
        assert settings.bot_token == "123456:TEST_TOKEN"
        assert settings.db_path == "sqlite+aiosqlite:///custom_games.db"
        assert settings.bot_username == "CustomAgentBot"
        assert settings.debug is True

    def test_case_insensitivity(self, monkeypatch):
        """Verify settings are case-insensitive (lowercase env vars are recognized)."""
        monkeypatch.setenv("bot_token", "lower_token_999")
        monkeypatch.setenv("debug", "1")
        monkeypatch.setenv("db_path", "test.db")
        monkeypatch.setenv("bot_username", "lower_bot")

        settings = Settings()
        assert settings.BOT_TOKEN == "lower_token_999"
        assert settings.DEBUG is True
        assert settings.DB_PATH == "test.db"
        assert settings.db_url == "sqlite+aiosqlite:///test.db"
        assert settings.effective_bot_username == "lower_bot"

    def test_extra_env_vars_ignored(self, monkeypatch):
        """Verify unknown extraneous environment variables do not cause validation errors."""
        monkeypatch.setenv("COMPLETELY_UNKNOWN_VAR", "some_value")
        settings = Settings()
        assert not hasattr(settings, "COMPLETELY_UNKNOWN_VAR")

    def test_bot_username_sanitization(self):
        """Verify effective_bot_username removes leading @ and handles whitespace/None."""
        s1 = Settings(BOT_USERNAME="@MyTelegramBot")
        assert s1.effective_bot_username == "MyTelegramBot"

        s2 = Settings(BOT_USERNAME="CleanBot")
        assert s2.effective_bot_username == "CleanBot"

        s3 = Settings(BOT_USERNAME="   ")
        assert s3.effective_bot_username == "SpyAnalystBot"

        s4 = Settings(BOT_USERNAME=None)
        assert s4.effective_bot_username == "SpyAnalystBot"

        s5 = Settings(BOT_USERNAME="")
        assert s5.effective_bot_username == "SpyAnalystBot"

        s6 = Settings(BOT_USERNAME="@@DoubleAtBot")
        assert s6.effective_bot_username == "DoubleAtBot"

    def test_db_url_normalization(self):
        """Verify db_url correctly prefixes bare file paths with sqlite+aiosqlite:///."""
        s1 = Settings(DB_PATH="custom.db")
        assert s1.db_url == "sqlite+aiosqlite:///custom.db"

        s2 = Settings(DB_PATH="sqlite+aiosqlite:///already_full_url.db")
        assert s2.db_url == "sqlite+aiosqlite:///already_full_url.db"

        s3 = Settings(DB_PATH="data/nested.db")
        assert s3.db_url == "sqlite+aiosqlite:///data/nested.db"

    def test_validate_for_bot_polling(self):
        """Verify validate_for_bot_polling raises ValueError on empty token and succeeds when set."""
        empty_settings = Settings(BOT_TOKEN="")
        with pytest.raises(ValueError, match="BOT_TOKEN is required"):
            empty_settings.validate_for_bot_polling()

        whitespace_settings = Settings(BOT_TOKEN="   ")
        with pytest.raises(ValueError, match="BOT_TOKEN is required"):
            whitespace_settings.validate_for_bot_polling()

        placeholder_settings = Settings(BOT_TOKEN="YOUR_BOT_TOKEN_HERE")
        with pytest.raises(ValueError, match="BOT_TOKEN is required"):
            placeholder_settings.validate_for_bot_polling()

        valid_settings = Settings(BOT_TOKEN="123456:VALID_TOKEN")
        valid_settings.validate_for_bot_polling()  # Should not raise

    def test_get_settings_lru_caching(self, monkeypatch):
        """Verify get_settings() returns cached instance and clears cache on demand."""
        get_settings.cache_clear()
        monkeypatch.setenv("BOT_TOKEN", "initial_token")

        first = get_settings()
        assert first.BOT_TOKEN == "initial_token"

        # Change env without clearing cache
        monkeypatch.setenv("BOT_TOKEN", "new_token")
        cached = get_settings()
        assert cached is first
        assert cached.BOT_TOKEN == "initial_token"

        # Clear cache and fetch again
        get_settings.cache_clear()
        refreshed = get_settings()
        assert refreshed.BOT_TOKEN == "new_token"
        assert refreshed is not first

    def test_load_from_custom_env_file(self, tmp_path):
        """Verify Settings can load configuration from a specific .env file."""
        env_file = tmp_path / ".env.test"
        env_file.write_text(
            "BOT_TOKEN=file_token_42\n"
            "DB_PATH=sqlite+aiosqlite:///from_file.db\n"
            "BOT_USERNAME=@EnvFileBot\n"
            "DEBUG=true\n",
            encoding="utf-8",
        )

        settings = Settings(_env_file=str(env_file))
        assert settings.BOT_TOKEN == "file_token_42"
        assert settings.DB_PATH == "sqlite+aiosqlite:///from_file.db"
        assert settings.effective_bot_username == "EnvFileBot"
        assert settings.DEBUG is True

    @pytest.mark.asyncio
    async def test_database_session_defaults(self, monkeypatch):
        """Verify create_sqlite_async_engine uses get_settings() when db_path/echo are omitted."""
        get_settings.cache_clear()
        monkeypatch.setenv("DB_PATH", "sqlite+aiosqlite:///:memory:")
        monkeypatch.setenv("DEBUG", "false")

        engine = create_sqlite_async_engine()
        try:
            assert engine.url.database == ":memory:"
            assert engine.url.drivername == "sqlite+aiosqlite"
            assert engine.echo is False
        finally:
            await engine.dispose()
            get_settings.cache_clear()

    def test_dispatcher_defaults_bot_username(self, monkeypatch):
        """Verify create_dispatcher defaults bot_username to get_settings().effective_bot_username."""
        get_settings.cache_clear()
        monkeypatch.setenv("BOT_USERNAME", "@ConfiguredDispatcherBot")

        dp = create_dispatcher(bot_username=None)
        try:
            assert dp["bot_username"] == "ConfiguredDispatcherBot"
        finally:
            get_settings.cache_clear()
