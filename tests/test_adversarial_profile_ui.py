"""Adversarial Stress Test Suite for Milestone 3: Profile Formatting & Telegram UI.

Empirical verification covering:
1. Adversarial HTML Injection & Sanitization in usernames, first_name, last_name:
   - Hostile script tags, unclosed formatting tags, event handlers, special XML entities.
   - Strict XML validation verifying generated dossier cards are syntactically well-formed.
   - Verification that only intended Telegram HTML formatting tags are present.
2. Missing, Null, and Empty Name Edge Cases:
   - Empty strings, None values, whitespace strings, missing usernames.
   - Fallback hierarchy: (first_name + last_name) -> first_name -> @username -> Agent_{id}.
   - Extreme strings (long 500+ char names, emojis, RTL override characters).
3. 0-Game Accounts & Corrupted Negative Statistics:
   - 0 games played, 0 wins: zero-division safety and 0.0% winrate display.
   - Negative EXP, negative games, negative mistakes/hints: stability under corrupted state.
   - High volume counter values (millions of games, wins, mistakes, hints).
4. Marshal Max-Rank & Tier Boundary Card Formatting:
   - All 19 ranks rendered and validated.
   - Exact boundary transitions: 0, 99, 100, 34999, 35000, 35001, 10_000_000.
   - Exact formatting for Marshal: 'EXP (Максимальное звание)' and '[██████████] 100%'.
   - Absence of '/ None EXP' or broken placeholders.
5. Exact Presence Verification of all Required Fields:
   - Rank badge, title, EXP, progress bar, wins, winrate, mistakes, hints, callsign, roles.
6. Telegram Handler & Keyboard Interaction Stress:
   - Command /profile and callback menu:profile with hostile user entities.
   - Repeated in-place refresh with unmodified message shield.
   - Back navigation cycle (Main Menu <-> Dossier).
   - Concurrent profile requests from multiple players.
"""
import asyncio
import html
import re
import xml.etree.ElementTree as ET
from typing import AsyncGenerator, Dict, List

import pytest
import pytest_asyncio
from aiogram import Bot, types
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

from bot.app import create_dispatcher
from bot.callbacks import MenuCallback
from bot.handlers.common import format_player_profile, render_player_profile
from bot.keyboards import (
    build_main_menu_keyboard,
    build_profile_keyboard,
    get_profile_keyboard,
)
from cryptogram.ranks import RANKS
from database.models import Base, User
from database.session import create_session_factory
from services.lock_manager import SessionLockManager
from services.session_service import SessionService
from tests.mock_session import MockPlayerClient, MockTelegramSession

# Regex enforcing valid 10-block progress bar format
PROGRESS_BAR_REGEX = re.compile(r"^\[([█░]{10})\]\s*(\d+)%$")


# ==============================================================================
# Fixtures
# ==============================================================================


@pytest_asyncio.fixture(scope="function")
async def test_engine() -> AsyncGenerator[AsyncEngine, None]:
    """In-memory SQLite async engine with StaticPool."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def file_engine(tmp_path) -> AsyncGenerator[AsyncEngine, None]:
    """File-backed SQLite async engine with WAL mode for concurrency testing."""
    import secrets
    from database.session import create_sqlite_async_engine, init_db
    db_file = tmp_path / f"adversarial_profile_{secrets.token_hex(4)}.db"
    engine = create_sqlite_async_engine(f"sqlite+aiosqlite:///{db_file}", echo=False)
    await init_db(engine)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def test_session_service(test_engine: AsyncEngine) -> SessionService:
    """SessionService instance for testing."""
    session_factory = create_session_factory(test_engine)
    lock_manager = SessionLockManager()
    return SessionService(session_factory=session_factory, lock_manager=lock_manager)


@pytest_asyncio.fixture(scope="function")
async def bot_env(test_session_service: SessionService) -> Dict:
    """Pre-configured bot test environment with dispatcher and clients."""
    mock_session = MockTelegramSession()
    bot = Bot(token="42:MOCK_TOKEN", session=mock_session)
    dp = create_dispatcher(
        session_service=test_session_service,
        lock_manager=test_session_service.lock_manager,
        bot_username="SpyAnalystBot",
        storage=MemoryStorage(),
    )

    client1 = MockPlayerClient(dp, bot, user_id=3001, username="player_one", first_name="Алекс")
    client2 = MockPlayerClient(dp, bot, user_id=3002, username="player_two", first_name="Борис")

    return {
        "dp": dp,
        "bot": bot,
        "session": mock_session,
        "service": test_session_service,
        "client1": client1,
        "client2": client2,
    }


def assert_card_xml_valid(card_text: str) -> ET.Element:
    """Validate that the generated card text is well-formed XML/HTML."""
    # Wrap in root element for XML parser
    wrapped = f"<root>{card_text}</root>"
    try:
        root = ET.fromstring(wrapped)
    except ET.ParseError as e:
        pytest.fail(f"Dossier card contains invalid XML/HTML formatting:\n{e}\nCard text:\n{card_text}")

    # Ensure only Telegram supported tags are present (b, code)
    allowed_tags = {"root", "b", "code"}
    for elem in root.iter():
        assert elem.tag in allowed_tags, f"Unauthorized tag <{elem.tag}> found in card HTML"

    return root


def assert_all_required_fields_present(card: str, user: User) -> None:
    """Enforce exact presence of all 8 mandatory specification fields plus UI headers."""
    # 1. Rank badge
    assert user.current_rank_badge in card, f"Missing rank badge {user.current_rank_badge!r}"

    # 2. Title
    assert html.escape(user.rank_title) in card, f"Missing rank title {user.rank_title!r}"

    # 3. EXP
    assert f"<b>{user.exp}</b>" in card, f"Missing user exp {user.exp}"
    if user.next_rank_exp is not None:
        assert f"<b>{user.next_rank_exp}</b>" in card, f"Missing next rank exp {user.next_rank_exp}"
    else:
        assert "(Максимальное звание)" in card, "Missing '(Максимальное звание)' label for max rank"

    # 4. Progress bar
    assert f"<code>{user.rank_progress_bar}</code>" in card, "Missing or improperly formatted progress bar"

    # 5. Wins
    assert f"• Побед: <b>{user.games_won}</b>" in card, "Missing wins counter"

    # 6. Winrate
    assert f"(Винрейт: <b>{user.win_rate}%</b>)" in card, "Missing winrate display"

    # 7. Mistakes
    assert f"• Ошибок допущено: <b>{user.total_mistakes}</b>" in card, "Missing mistakes counter"

    # 8. Hints
    assert f"• Подсказок использовано: <b>{user.total_hints_used}</b>" in card, "Missing hints counter"

    # UI structure invariants
    assert "🎖 <b>ЛИЧНОЕ ДЕЛО ВОЕННОСЛУЖАЩЕГО</b>" in card
    assert "👤 <b>Позывной:</b>" in card
    assert f"• Всего игр: <b>{user.games_played}</b>" in card
    assert f"• В роли Аналитика: <b>{user.games_as_analyst}</b>" in card
    assert f"• В роли Агента: <b>{user.games_as_agent}</b>" in card


# ==============================================================================
# 1. Adversarial HTML Injection & Sanitization
# ==============================================================================


class TestHtmlInjectionSanitization:
    """Empirical challenge against HTML injection, tag escaping, and entity corruption."""

    HOSTILE_PAYLOADS = [
        "<script>alert('xss')</script>",
        "<b>bold</b>",
        "</b><script src='evil.js'></script><b>",
        "<a href='https://attacker.org'>Free Diamonds</a>",
        "<img src=x onerror=alert(1)>",
        "<iframe src='javascript:alert(1)'></iframe>",
        "& < > \" '",
        "&amp; &lt; &gt; &quot; &#39;",
        "<div style='position:fixed;top:0;'>cover</div>",
        "<<>> <><>",
        "<i><em><u><strike>unclosed",
        "'; DROP TABLE users; -- <script>",
    ]

    @pytest.mark.parametrize("payload", HOSTILE_PAYLOADS)
    def test_html_injection_in_first_name(self, payload: str):
        """Verify first_name containing hostile HTML is sanitized and XML-valid."""
        user = User(
            id=1001,
            username="normal_user",
            first_name=payload,
            last_name="Novikov",
            exp=500,
            games_played=5,
            games_won=3,
            games_as_analyst=2,
            games_as_agent=3,
            total_mistakes=1,
            total_hints_used=0,
        )
        card = render_player_profile(user)
        assert_card_xml_valid(card)
        assert_all_required_fields_present(card, user)
        # Verify raw script/img tags are NOT in output
        assert "<script>" not in card
        assert "<img" not in card
        assert "<iframe" not in card

    @pytest.mark.parametrize("payload", HOSTILE_PAYLOADS)
    def test_html_injection_in_username(self, payload: str):
        """Verify username containing hostile HTML is sanitized when used as fallback."""
        user = User(
            id=1002,
            username=payload,
            first_name="",
            last_name=None,
            exp=750,
            games_played=10,
            games_won=6,
            games_as_analyst=5,
            games_as_agent=5,
            total_mistakes=2,
            total_hints_used=1,
        )
        card = render_player_profile(user)
        assert_card_xml_valid(card)
        assert_all_required_fields_present(card, user)
        assert "<script>" not in card
        assert "<img" not in card
        assert "<iframe" not in card

    @pytest.mark.parametrize("payload", HOSTILE_PAYLOADS)
    def test_html_injection_in_last_name(self, payload: str):
        """Verify last_name containing hostile HTML is sanitized and XML-valid."""
        user = User(
            id=1003,
            username="operative",
            first_name="Агент",
            last_name=payload,
            exp=1200,
            games_played=12,
            games_won=10,
            games_as_analyst=6,
            games_as_agent=6,
            total_mistakes=0,
            total_hints_used=0,
        )
        card = render_player_profile(user)
        assert_card_xml_valid(card)
        assert_all_required_fields_present(card, user)
        assert "<script>" not in card
        assert "<img" not in card


# ==============================================================================
# 2. Missing, Null, and Empty Name Edge Cases
# ==============================================================================


class TestMissingAndEmptyNames:
    """Stress-test user identification when names and usernames are missing, empty, or whitespace."""

    def test_empty_first_and_last_name_with_username(self):
        """When first and last name are empty, fallback to @username."""
        user = User(
            id=2001,
            username="phantom_agent",
            first_name="",
            last_name="",
            exp=100,
            games_played=1,
            games_won=1,
            games_as_analyst=1,
            games_as_agent=0,
            total_mistakes=0,
            total_hints_used=0,
        )
        card = render_player_profile(user)
        assert_card_xml_valid(card)
        assert "👤 <b>Позывной:</b> @phantom_agent" in card
        assert_all_required_fields_present(card, user)

    def test_empty_first_last_and_username_fallback_to_agent_id(self):
        """When first, last, and username are all empty/None, fallback to Agent_{id}."""
        user = User(
            id=778899,
            username=None,
            first_name="",
            last_name=None,
            exp=0,
            games_played=0,
            games_won=0,
            games_as_analyst=0,
            games_as_agent=0,
            total_mistakes=0,
            total_hints_used=0,
        )
        card = render_player_profile(user)
        assert_card_xml_valid(card)
        assert "👤 <b>Позывной:</b> Agent_778899" in card
        assert_all_required_fields_present(card, user)

    def test_empty_first_name_with_last_name(self):
        """When first_name is empty but last_name is provided."""
        user = User(
            id=2002,
            username="bond007",
            first_name="",
            last_name="Бонд",
            exp=2500,
            games_played=15,
            games_won=12,
            games_as_analyst=7,
            games_as_agent=8,
            total_mistakes=3,
            total_hints_used=1,
        )
        card = render_player_profile(user)
        assert_card_xml_valid(card)
        assert "👤 <b>Позывной:</b> Бонд" in card
        assert_all_required_fields_present(card, user)

    def test_whitespace_only_names(self):
        """Whitespace strings should be safely handled without breaking formatting."""
        user = User(
            id=2003,
            username="   ",
            first_name="   ",
            last_name=None,
            exp=450,
            games_played=4,
            games_won=2,
            games_as_analyst=2,
            games_as_agent=2,
            total_mistakes=1,
            total_hints_used=0,
        )
        card = render_player_profile(user)
        assert_card_xml_valid(card)
        assert_all_required_fields_present(card, user)

    def test_extreme_length_and_unicode_names(self):
        """Extreme length, unicode emojis, and RTL direction override characters."""
        long_name = "Штирлиц" * 50  # 350 chars
        rtl_name = "\u202e\u0041\u0067\u0065\u006e\u0074\u202c"  # RTL override
        emoji_name = "🕵️‍♂️🎖️🛡️⚔️👑⚡🔥"

        for extreme in [long_name, rtl_name, emoji_name]:
            user = User(
                id=2004,
                username="extreme_user",
                first_name=extreme,
                last_name=None,
                exp=1900,
                games_played=8,
                games_won=6,
                games_as_analyst=4,
                games_as_agent=4,
                total_mistakes=0,
                total_hints_used=0,
            )
            card = render_player_profile(user)
            assert_card_xml_valid(card)
            assert_all_required_fields_present(card, user)


# ==============================================================================
# 3. 0-Game Accounts & Corrupted Negative Statistics
# ==============================================================================


class TestZeroGameAndCorruptedStats:
    """Stress-test numerical boundaries, zero-division hazards, and negative values."""

    def test_fresh_zero_game_account(self):
        """A brand new player with 0 games, 0 wins, 0 exp."""
        user = User(
            id=3001,
            username="recruit_zero",
            first_name="Рекрут",
            last_name=None,
            exp=0,
            games_played=0,
            games_won=0,
            games_as_analyst=0,
            games_as_agent=0,
            total_mistakes=0,
            total_hints_used=0,
        )
        card = render_player_profile(user)
        assert_card_xml_valid(card)
        assert_all_required_fields_present(card, user)
        assert "Всего игр: <b>0</b>" in card
        assert "Побед: <b>0</b> (Винрейт: <b>0.0%</b>)" in card
        assert "• Опыт: <b>0</b> / <b>100</b> EXP" in card
        assert "[░░░░░░░░░░] 0%" in card

    def test_games_played_zero_with_anomalous_positive_wins(self):
        """Defensive test: if games_played is 0 but games_won has data, win_rate must be 0.0% without ZeroDivisionError."""
        user = User(
            id=3002,
            username="glitch",
            first_name="Глюк",
            last_name=None,
            exp=0,
            games_played=0,
            games_won=5,
            games_as_analyst=0,
            games_as_agent=0,
            total_mistakes=0,
            total_hints_used=0,
        )
        card = render_player_profile(user)
        assert_card_xml_valid(card)
        assert "Винрейт: <b>0.0%</b>" in card

    def test_negative_statistics_robustness(self):
        """Corrupted negative counters (exp < 0, games < 0) must not crash card rendering."""
        user = User(
            id=3003,
            username="negative_user",
            first_name="Отрицательный",
            last_name=None,
            exp=-500,
            games_played=-10,
            games_won=-5,
            games_as_analyst=-3,
            games_as_agent=-7,
            total_mistakes=-2,
            total_hints_used=-1,
        )
        card = render_player_profile(user)
        assert_card_xml_valid(card)
        assert "🪖 Рядовой" in card
        assert "[░░░░░░░░░░] 0%" in card
        assert "Винрейт: <b>0.0%</b>" in card

    def test_astronomical_counters(self):
        """Accounts with massive counter values (millions) must format without integer overflow or layout truncation."""
        user = User(
            id=3004,
            username="veteran_millions",
            first_name="Маршал_Вселенной",
            last_name=None,
            exp=999_999_999,
            games_played=5_000_000,
            games_won=4_500_000,
            games_as_analyst=2_500_000,
            games_as_agent=2_500_000,
            total_mistakes=1_000_000,
            total_hints_used=500_000,
        )
        card = render_player_profile(user)
        assert_card_xml_valid(card)
        assert_all_required_fields_present(card, user)
        assert "👑⭐ Маршал" in card
        assert "999999999" in card
        assert "Винрейт: <b>90.0%</b>" in card
        assert "5000000" in card


# ==============================================================================
# 4. Marshal Max-Rank & Tier Boundary Card Formatting
# ==============================================================================


class TestMarshalAndBoundaryRankFormatting:
    """Rigorous boundary verification across all 19 ranks and Marshal maximum rank status."""

    def test_all_19_ranks_rendered_accurately(self):
        """Verify each of the 19 ranks produces valid dossier cards with exact badge and title."""
        for rank in RANKS:
            user = User(
                id=4000 + rank.level,
                username=f"rank_officer_{rank.level}",
                first_name=f"Офицер_{rank.level}",
                last_name=None,
                exp=rank.threshold_exp,
                games_played=10,
                games_won=8,
                games_as_analyst=5,
                games_as_agent=5,
                total_mistakes=2,
                total_hints_used=1,
            )
            card = render_player_profile(user)
            assert_card_xml_valid(card)
            assert_all_required_fields_present(card, user)
            assert rank.badge in card
            assert rank.name_ru in card

    @pytest.mark.parametrize(
        "exp, expected_rank_name, expected_badge, is_max_rank",
        [
            (0, "Рядовой", "🪖", False),
            (99, "Рядовой", "🪖", False),
            (100, "Ефрейтор", "🎗️", False),
            (249, "Ефрейтор", "🎗️", False),
            (250, "Младший сержант", "🥉", False),
            (24999, "Генерал-полковник", "⚔️⭐⭐⭐", False),
            (25000, "Генерал армии", "🦅⭐", False),
            (34999, "Генерал армии", "🦅⭐", False),
            (35000, "Маршал", "👑⭐", True),
            (35001, "Маршал", "👑⭐", True),
            (100_000, "Маршал", "👑⭐", True),
            (10_000_000, "Маршал", "👑⭐", True),
        ],
    )
    def test_exact_boundary_transitions_and_marshal_formatting(
        self, exp: int, expected_rank_name: str, expected_badge: str, is_max_rank: bool
    ):
        """Test exact EXP boundary transitions and confirm Marshal capping text."""
        user = User(
            id=5000 + (exp % 1000),
            username=f"user_{exp}",
            first_name="Тестер",
            last_name="Границ",
            exp=exp,
            games_played=50,
            games_won=40,
            games_as_analyst=20,
            games_as_agent=30,
            total_mistakes=5,
            total_hints_used=3,
        )
        card = render_player_profile(user)
        assert_card_xml_valid(card)
        assert_all_required_fields_present(card, user)

        assert expected_badge in card
        assert expected_rank_name in card

        if is_max_rank:
            assert "(Максимальное звание)" in card
            assert "[██████████] 100%" in card
            assert "/ None" not in card
            assert "/ <b>None</b>" not in card
        else:
            assert "(Максимальное звание)" not in card
            assert user.next_rank_exp is not None
            assert f"/ <b>{user.next_rank_exp}</b> EXP" in card


# ==============================================================================
# 5. Exact Presence Verification of all Required Fields
# ==============================================================================


class TestRequiredFieldsPresence:
    """Systematically enforce presence of all requested fields in diverse scenarios."""

    def test_field_completeness_matrix(self):
        """Check field presence across varying wins, losses, roles, and mistakes."""
        test_matrix = [
            # wins, played, analyst, agent, mistakes, hints, exp
            (0, 0, 0, 0, 0, 0, 0),
            (1, 1, 1, 0, 0, 0, 200),
            (5, 10, 4, 6, 12, 8, 1400),
            (50, 50, 25, 25, 0, 0, 35000),
        ]

        for won, played, analyst, agent, mistakes, hints, exp in test_matrix:
            user = User(
                id=6000 + exp,
                username=f"player_{exp}",
                first_name="Матрица",
                last_name=None,
                exp=exp,
                games_played=played,
                games_won=won,
                games_as_analyst=analyst,
                games_as_agent=agent,
                total_mistakes=mistakes,
                total_hints_used=hints,
            )
            card = render_player_profile(user)
            assert_card_xml_valid(card)
            assert_all_required_fields_present(card, user)


# ==============================================================================
# 6. Telegram Handler & Keyboard Interaction Stress
# ==============================================================================


@pytest.mark.asyncio
class TestAdversarialBotInteractions:
    """Stress test bot command and callback flows with hostile and concurrent inputs."""

    async def test_command_profile_with_hostile_telegram_username(self, bot_env):
        """User with hostile script payload in first_name invokes /profile."""
        dp = bot_env["dp"]
        bot = bot_env["bot"]
        session: MockTelegramSession = bot_env["session"]

        hostile_client = MockPlayerClient(
            dp,
            bot,
            user_id=7001,
            username="evil<script>",
            first_name="<b>XSS</b> <script>alert(1)</script>",
        )

        await hostile_client.send_command("/profile")

        msg = session.get_last_message_for_chat(7001)
        assert msg is not None
        assert_card_xml_valid(msg.text)
        assert "<script>" not in msg.text
        assert "&lt;b&gt;XSS&lt;/b&gt;" in msg.text

    async def test_repeated_in_place_refresh_hammering(self, bot_env):
        """Simulate a user rapidly clicking '🔄 Обновить данные' 20 times with unmodified content."""
        client: MockPlayerClient = bot_env["client1"]
        session: MockTelegramSession = bot_env["session"]

        # Open profile
        await client.send_command("/profile")
        profile_msg = session.get_last_message_for_chat(client.user.id)
        assert profile_msg is not None

        # Turn on Telegram unmodified error simulator
        session.simulate_unmodified_error = True

        # Hammer refresh 20 times rapidly
        refresh_cb = MenuCallback(action="profile").pack()
        for _ in range(20):
            await client.click_button(refresh_cb, message=profile_msg)

        # Confirm all 20 callbacks were answered cleanly
        assert len(session.callback_answers) >= 20

    async def test_navigation_ping_pong_loop(self, bot_env):
        """Repeatedly navigate back and forth between Main Menu and Dossier."""
        client: MockPlayerClient = bot_env["client1"]
        session: MockTelegramSession = bot_env["session"]

        await client.send_command("/start")
        msg = session.get_last_message_for_chat(client.user.id)
        assert msg is not None

        profile_cb = MenuCallback(action="profile").pack()
        main_cb = MenuCallback(action="main").pack()

        for _ in range(5):
            # Click to profile
            await client.click_button(profile_cb, message=msg)
            msg = session.get_last_message_for_chat(client.user.id)
            assert "ЛИЧНОЕ ДЕЛО ВОЕННОСЛУЖАЩЕГО" in msg.text
            assert_card_xml_valid(msg.text)

            # Click to main menu
            await client.click_button(main_cb, message=msg)
            msg = session.get_last_message_for_chat(client.user.id)
            assert "Главное меню" in msg.text or "Шпион и Аналитик" in msg.text

    async def test_concurrent_profile_commands_from_multiple_users(self, file_engine: AsyncEngine):
        """Simulate 10 distinct users concurrently requesting /profile command on a file-backed WAL database."""
        session_factory = create_session_factory(file_engine)
        lock_manager = SessionLockManager()
        service = SessionService(session_factory=session_factory, lock_manager=lock_manager)

        mock_session = MockTelegramSession()
        bot = Bot(token="42:MOCK_TOKEN", session=mock_session)
        dp = create_dispatcher(
            session_service=service,
            lock_manager=lock_manager,
            bot_username="SpyAnalystBot",
            storage=MemoryStorage(),
        )

        users = [
            MockPlayerClient(
                dp,
                bot,
                user_id=8000 + i,
                username=f"user_{i}",
                first_name=f"Курсант_{i}",
            )
            for i in range(10)
        ]

        # Dispatch all commands concurrently
        tasks = [u.send_command("/profile") for u in users]
        await asyncio.gather(*tasks)

        # Verify each user received an individualized, XML-valid profile
        for u in users:
            msg = mock_session.get_last_message_for_chat(u.user.id)
            assert msg is not None
            assert_card_xml_valid(msg.text)
            assert u.user.first_name in msg.text
            assert "Рядовой" in msg.text
            assert msg.reply_markup is not None
            buttons = [b for row in msg.reply_markup.inline_keyboard for b in row]
            assert any("обновить" in b.text.lower() for b in buttons)
