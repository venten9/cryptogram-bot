"""Adversarial and boundary stress tests for application configuration and settings loading.

This suite stress-tests config.py under hostile and boundary environments:
1. Adversarial tokens (empty, whitespace, zero-width, unicode, placeholder variations).
2. Malformed and complex DB_PATH URIs (Windows backslashes, query parameters, sync drivers, memory).
3. Case insensitivity and boolean parsing edge cases.
4. Multithreaded concurrency stress for get_settings() singleton and cache invalidation.
5. Custom .env files (BOM, quotes, missing files, dynamic invalidation).
"""

import concurrent.futures
import os
import tempfile
import pytest
from pydantic import ValidationError
from sqlalchemy.exc import InvalidRequestError

from config import Settings, get_settings
from database.session import create_sqlite_async_engine


pytestmark = pytest.mark.boundary


class TestAdversarialTokens:
    """Stress-test BOT_TOKEN under boundary, placeholder, and unicode values."""

    @pytest.mark.parametrize(
        "empty_token",
        [
            "",
            "   ",
            "\t\n\r\f\v",
            " \t \n ",
        ],
    )
    def test_empty_and_whitespace_tokens(self, empty_token):
        """Verify that empty and whitespace tokens are detected as unconfigured."""
        s = Settings(BOT_TOKEN=empty_token)
        assert s.is_token_configured is False
        with pytest.raises(ValueError, match="BOT_TOKEN is required"):
            s.validate_for_bot_polling()

    @pytest.mark.parametrize(
        "placeholder",
        [
            "YOUR_BOT_TOKEN",
            "YOUR_BOT_TOKEN_HERE",
            "YOUR_TELEGRAM_BOT_TOKEN",
            "YOUR_API_KEY",
        ],
    )
    def test_uppercase_placeholders_rejected(self, placeholder):
        """Verify that standard template placeholder tokens starting with YOUR_ are rejected."""
        s = Settings(BOT_TOKEN=placeholder)
        assert s.is_token_configured is False
        with pytest.raises(ValueError, match="BOT_TOKEN is required"):
            s.validate_for_bot_polling()

    def test_lowercase_placeholder_boundary(self):
        """Document empirical boundary: lowercase placeholder 'your_bot_token' is currently accepted."""
        s = Settings(BOT_TOKEN="your_bot_token")
        # Since implementation checks startswith('YOUR_') without .upper(), this passes is_token_configured
        assert s.is_token_configured is True
        # It does not raise in validate_for_bot_polling()
        s.validate_for_bot_polling()

    def test_unicode_and_zero_width_token_boundary(self):
        """Test unicode strings and zero-width spaces in BOT_TOKEN."""
        # Zero-width space (\u200b) is not classified as whitespace by str.strip() in Python
        s_zw = Settings(BOT_TOKEN="\u200b")
        assert s_zw.is_token_configured is True

        # Non-ASCII / Cyrillic token
        s_cyr = Settings(BOT_TOKEN="123456:ТОКЕН_БОТА")
        assert s_cyr.is_token_configured is True
        assert s_cyr.BOT_TOKEN == "123456:ТОКЕН_БОТА"

        # Emoji token
        s_emoji = Settings(BOT_TOKEN="123456:🤖🔒")
        assert s_emoji.is_token_configured is True
        assert s_emoji.BOT_TOKEN == "123456:🤖🔒"

    def test_unstripped_whitespace_token_behavior(self):
        """Document token with leading/trailing spaces: is_token_configured is True but token preserves spaces."""
        raw_token = "  123456789:ABCdefGhIjkLmNoPqRsTuVwXyZ  "
        s = Settings(BOT_TOKEN=raw_token)
        assert s.is_token_configured is True
        # Implementation preserves exact unstripped value
        assert s.BOT_TOKEN == raw_token
        assert s.bot_token == raw_token


class TestAdversarialDbPaths:
    """Stress-test DB_PATH with complex, Windows, memory, and sync driver URIs."""

    def test_windows_absolute_path_with_backslashes(self):
        """Verify Windows absolute paths with backslashes are prefixed with sqlite+aiosqlite:///."""
        win_path = r"C:\Users\User\AppData\Local\bot_games.db"
        s = Settings(DB_PATH=win_path)
        expected_url = f"sqlite+aiosqlite:///{win_path}"
        assert s.db_url == expected_url

        # Verify SQLAlchemy engine parses Windows path
        engine = create_sqlite_async_engine(db_path=s.DB_PATH)
        try:
            assert engine.url.database == win_path
            assert engine.url.drivername == "sqlite+aiosqlite"
        finally:
            # Engine did not establish a live file connection yet, but close cleanly
            pass

    def test_query_parameters_in_db_path(self):
        """Verify URI query parameters (e.g. timeout, cache, mode) are preserved."""
        url_with_params = "sqlite+aiosqlite:///app.db?timeout=30&cache=shared"
        s = Settings(DB_PATH=url_with_params)
        assert s.db_url == url_with_params

        engine = create_sqlite_async_engine(db_path=s.DB_PATH)
        assert engine.url.query.get("timeout") == "30"
        assert engine.url.query.get("cache") == "shared"

    def test_in_memory_bare_and_full_uri(self):
        """Verify both bare :memory: and full sqlite+aiosqlite:///:memory: are handled."""
        s1 = Settings(DB_PATH=":memory:")
        assert s1.db_url == "sqlite+aiosqlite:///:memory:"

        s2 = Settings(DB_PATH="sqlite+aiosqlite:///:memory:")
        assert s2.db_url == "sqlite+aiosqlite:///:memory:"

    def test_sync_sqlite_driver_raises_invalid_request(self):
        """Empirically verify that specifying sync sqlite:/// raises InvalidRequestError in async engine."""
        s = Settings(DB_PATH="sqlite:///test.db")
        # db_url leaves 'sqlite:///test.db' intact because '://' is present
        assert s.db_url == "sqlite:///test.db"

        with pytest.raises(InvalidRequestError, match="The asyncio extension requires an async driver"):
            create_sqlite_async_engine(db_path=s.DB_PATH)

    def test_empty_db_path_behavior(self):
        """Verify empty string DB_PATH produces bare prefix."""
        s = Settings(DB_PATH="")
        assert s.db_url == "sqlite+aiosqlite:///"


class TestCaseInsensitivityAndBooleans:
    """Stress-test mixed-case environment variables and boolean parsing."""

    def test_mixed_case_env_vars(self, monkeypatch):
        """Verify settings recognizes mixed-case environment variables."""
        monkeypatch.setenv("bOt_tOkEn", "mixed_token_123")
        monkeypatch.setenv("DeBuG", "1")
        monkeypatch.setenv("dB_pAtH", "mixed_path.db")
        monkeypatch.setenv("bOt_uSeRnAmE", "MixedCaseBot")

        s = Settings()
        assert s.BOT_TOKEN == "mixed_token_123"
        assert s.DEBUG is True
        assert s.DB_PATH == "mixed_path.db"
        assert s.BOT_USERNAME == "MixedCaseBot"
        assert s.effective_bot_username == "MixedCaseBot"

    @pytest.mark.parametrize(
        "val,expected",
        [
            ("1", True),
            ("0", False),
            ("true", True),
            ("false", False),
            ("yes", True),
            ("no", False),
            ("on", True),
            ("off", False),
            ("True", True),
            ("FALSE", False),
            ("YES", True),
            ("NO", False),
        ],
    )
    def test_boolean_env_string_variations(self, monkeypatch, val, expected):
        """Verify all standard boolean representations in environment are parsed correctly."""
        monkeypatch.setenv("DEBUG", val)
        s = Settings()
        assert s.DEBUG is expected

    def test_invalid_boolean_raises_validation_error(self, monkeypatch):
        """Verify non-boolean string in DEBUG raises Pydantic ValidationError."""
        monkeypatch.setenv("DEBUG", "not_a_boolean")
        with pytest.raises(ValidationError):
            Settings()


class TestMultithreadedSettingsConcurrency:
    """Stress-test thread safety of get_settings() singleton and cache invalidation."""

    def test_high_concurrency_get_settings(self):
        """Verify 50 concurrent threads accessing get_settings() receive identical singleton."""
        get_settings.cache_clear()
        results = []

        def call_settings():
            s = get_settings()
            return id(s), s.effective_bot_username, s.db_url

        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
            futures = [executor.submit(call_settings) for _ in range(50)]
            for f in concurrent.futures.as_completed(futures):
                results.append(f.result())

        instance_ids = {r[0] for r in results}
        # In the absence of cache_clear(), all threads MUST receive the exact same singleton instance
        assert len(instance_ids) == 1
        assert results[0][1] == "SpyAnalystBot"
        assert results[0][2] == "sqlite+aiosqlite:///cryptogram.db"

    def test_concurrent_access_with_cache_clear(self):
        """Verify concurrent get_settings() and cache_clear() never raises exceptions or corrupts state."""
        errors = []

        def worker(i):
            try:
                if i % 5 == 0:
                    get_settings.cache_clear()
                s = get_settings()
                assert isinstance(s, Settings)
                assert s.effective_bot_username is not None
            except Exception as e:
                errors.append(e)

        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
            futures = [executor.submit(worker, i) for i in range(100)]
            concurrent.futures.wait(futures)

        assert errors == [], f"Concurrency errors encountered: {errors}"
        get_settings.cache_clear()


class TestCustomEnvFileAndBOM:
    """Stress-test custom .env loading, UTF-8 BOM, and comments."""

    def test_env_file_with_bom_and_comments(self, tmp_path):
        """Verify Settings loads cleanly from a UTF-8 BOM encoded .env file with comments and quotes."""
        env_file = tmp_path / ".env.bom"
        env_content = (
            "# Top level comment\n"
            "\n"
            "BOT_TOKEN=\"bom_token_999\"\n"
            "# Database section\n"
            "DB_PATH='sqlite+aiosqlite:///bom_test.db'\n"
            "BOT_USERNAME=@BomBotAgent\n"
            "DEBUG=true\n"
        )
        # Write with UTF-8-SIG (byte order mark \xef\xbb\xbf)
        env_file.write_text(env_content, encoding="utf-8-sig")

        s = Settings(_env_file=str(env_file))
        assert s.BOT_TOKEN == "bom_token_999"
        assert s.DB_PATH == "sqlite+aiosqlite:///bom_test.db"
        assert s.effective_bot_username == "BomBotAgent"
        assert s.DEBUG is True

    def test_nonexistent_env_file_falls_back_to_defaults(self):
        """Verify specifying a nonexistent .env file gracefully uses defaults without crashing."""
        s = Settings(_env_file="this_file_does_not_exist_987654.env")
        assert s.BOT_TOKEN == ""
        assert s.DB_PATH == "sqlite+aiosqlite:///cryptogram.db"
        assert s.effective_bot_username == "SpyAnalystBot"
        assert s.DEBUG is False


class TestBotUsernameEdgeCases:
    """Stress-test BOT_USERNAME sanitization and boundary handling."""

    @pytest.mark.parametrize(
        "input_username,expected_effective",
        [
            ("@AgentBot", "AgentBot"),
            ("@@@TripleAtBot", "TripleAtBot"),
            ("  @SpacedBot  ", "SpacedBot"),
            ("   ", "SpyAnalystBot"),
            ("", "SpyAnalystBot"),
            (None, "SpyAnalystBot"),
            ("@ШпионБот", "ШпионБот"),
        ],
    )
    def test_username_sanitization_boundaries(self, input_username, expected_effective):
        """Verify effective_bot_username strips leading @ and whitespace, falling back correctly."""
        s = Settings(BOT_USERNAME=input_username)
        assert s.effective_bot_username == expected_effective
