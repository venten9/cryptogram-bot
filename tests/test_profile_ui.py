"""Mock Integration and Unit Test Suite for Player Military Profile Telegram UI.

Tests:
- Main menu and profile keyboard structures and callback payloads.
- Stylized military dossier formatting across different ranks (Private, Captain, Marshal).
- /profile command dispatch, automatic user creation in DB, and formatted dossier response.
- Inline callback query interaction from main menu and in-place dossier refresh.
- Safe in-place editing handling TelegramBadRequest 'message is not modified' gracefully.
- Back-navigation from profile dossier to HQ main menu.
"""
from typing import AsyncGenerator, Dict

import pytest
import pytest_asyncio
from aiogram import Bot, types
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

from bot.app import create_dispatcher
from bot.callbacks import MenuCallback
from bot.handlers.common import render_player_profile
from bot.keyboards import (
    build_main_menu_keyboard,
    build_profile_keyboard,
    get_profile_keyboard,
)
from database.models import Base, User
from database.session import create_session_factory
from services.lock_manager import SessionLockManager
from services.session_service import SessionService
from tests.mock_session import MockPlayerClient, MockTelegramSession


# ==========================================
# Test Fixtures
# ==========================================


@pytest_asyncio.fixture(scope="function")
async def test_engine() -> AsyncGenerator[AsyncEngine, None]:
    """In-memory SQLite async engine with StaticPool for isolated in-memory DB."""
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
async def test_session_service(test_engine: AsyncEngine) -> SessionService:
    """Provides a fresh SessionService bound to the test engine."""
    session_factory = create_session_factory(test_engine)
    lock_manager = SessionLockManager()
    return SessionService(session_factory=session_factory, lock_manager=lock_manager)


@pytest_asyncio.fixture(scope="function")
async def bot_env(test_session_service: SessionService) -> Dict:
    """Pre-configured bot test environment with dispatcher, mock session, and player clients."""
    mock_session = MockTelegramSession()
    bot = Bot(token="42:MOCK_TOKEN", session=mock_session)
    dp = create_dispatcher(
        session_service=test_session_service,
        lock_manager=test_session_service.lock_manager,
        bot_username="SpyAnalystBot",
        storage=MemoryStorage(),
    )

    analyst = MockPlayerClient(
        dp, bot, user_id=2001, username="analyst_z", first_name="Зорге"
    )
    agent = MockPlayerClient(
        dp, bot, user_id=2002, username="agent_bond", first_name="Бонд"
    )

    return {
        "dp": dp,
        "bot": bot,
        "session": mock_session,
        "service": test_session_service,
        "analyst": analyst,
        "agent": agent,
    }


# ==========================================
# Keyboard Structure Tests
# ==========================================


class TestProfileKeyboardStructure:

    def test_main_menu_keyboard_has_profile_button(self):
        """Verify build_main_menu_keyboard includes 'Мое досье / Профиль' button."""
        kb = build_main_menu_keyboard()
        buttons = [btn for row in kb.inline_keyboard for btn in row]

        profile_btns = [b for b in buttons if "досье" in b.text.lower() or "профиль" in b.text.lower()]
        assert len(profile_btns) == 1, "Expected exactly one profile button in main menu"

        btn = profile_btns[0]
        assert "🎖" in btn.text
        assert btn.callback_data == MenuCallback(action="profile").pack()

    def test_profile_keyboard_structure_and_alias(self):
        """Verify build_profile_keyboard contains refresh and main menu buttons."""
        kb = build_profile_keyboard()
        assert kb == get_profile_keyboard()

        buttons = [btn for row in kb.inline_keyboard for btn in row]
        assert len(buttons) == 2, "Profile keyboard must contain 2 action buttons"

        refresh_btn = buttons[0]
        assert "обновить" in refresh_btn.text.lower()
        assert "🔄" in refresh_btn.text
        assert refresh_btn.callback_data == MenuCallback(action="profile").pack()

        back_btn = buttons[1]
        assert "главное меню" in back_btn.text.lower()
        assert "◀️" in back_btn.text or "🏠" in back_btn.text
        assert back_btn.callback_data == MenuCallback(action="main").pack()


# ==========================================
# Dossier Card Formatting Tests
# ==========================================


class TestProfileCardFormatting:

    def test_novice_player_card_formatting(self):
        """Verify dossier formatting for a newly registered player (0 EXP, Private)."""
        user = User(
            id=101,
            username="recruit",
            first_name="Иван",
            last_name="Новичков",
            exp=0,
            games_played=0,
            games_won=0,
            games_as_analyst=0,
            games_as_agent=0,
            total_mistakes=0,
            total_hints_used=0,
        )

        card = render_player_profile(user)
        assert "ЛИЧНОЕ ДЕЛО ВОЕННОСЛУЖАЩЕГО" in card
        assert "Иван Новичков" in card
        assert "🪖" in card
        assert "Рядовой" in card
        assert "<b>0</b> / <b>100</b> EXP" in card
        assert "[░░░░░░░░░░] 0%" in card
        assert "Всего игр: <b>0</b>" in card
        assert "Побед: <b>0</b>" in card
        assert "Винрейт: <b>0.0%</b>" in card
        assert "В роли Аналитика: <b>0</b>" in card
        assert "В роли Агента: <b>0</b>" in card
        assert "Ошибок допущено: <b>0</b>" in card
        assert "Подсказок использовано: <b>0</b>" in card

    def test_veteran_player_card_formatting(self):
        """Verify dossier formatting for an experienced player (Captain tier)."""
        user = User(
            id=102,
            username="captain_cold",
            first_name="Алексей",
            exp=4500,
            games_played=20,
            games_won=15,
            games_as_analyst=10,
            games_as_agent=10,
            total_mistakes=5,
            total_hints_used=3,
        )

        card = render_player_profile(user)
        assert "ЛИЧНОЕ ДЕЛО ВОЕННОСЛУЖАЩЕГО" in card
        assert "Алексей" in card
        assert "⭐⭐⭐⭐" in card
        assert "Капитан" in card
        assert "<b>4500</b> / <b>5000</b> EXP" in card
        assert "50%" in card
        assert "Всего игр: <b>20</b>" in card
        assert "Побед: <b>15</b> (Винрейт: <b>75.0%</b>)" in card
        assert "В роли Аналитика: <b>10</b>" in card
        assert "В роли Агента: <b>10</b>" in card
        assert "Ошибок допущено: <b>5</b>" in card
        assert "Подсказок использовано: <b>3</b>" in card

    def test_marshal_max_rank_card_formatting(self):
        """Verify dossier formatting for max rank Marshal (35000+ EXP)."""
        user = User(
            id=103,
            username="supreme_marshal",
            first_name="Георгий",
            exp=40000,
            games_played=100,
            games_won=95,
            games_as_analyst=50,
            games_as_agent=50,
            total_mistakes=2,
            total_hints_used=1,
        )

        card = render_player_profile(user)
        assert "ЛИЧНОЕ ДЕЛО ВОЕННОСЛУЖАЩЕГО" in card
        assert "Георгий" in card
        assert "👑⭐" in card
        assert "Маршал" in card
        assert "40000" in card
        assert "Максимальное звание" in card
        assert "[██████████] 100%" in card
        assert "Винрейт: <b>95.0%</b>" in card


# ==========================================
# Bot UI Handler Integration Tests
# ==========================================


@pytest.mark.asyncio
class TestProfileBotHandlers:

    async def test_profile_command_creates_user_and_renders_card(self, bot_env):
        """Verify /profile command creates user in database and returns dossier with keyboard."""
        analyst = bot_env["analyst"]
        session = bot_env["session"]
        service: SessionService = bot_env["service"]

        # User does not exist in DB yet
        await analyst.send_command("/profile")

        # Verify user was created in database
        db_user = await service.get_or_create_user(analyst.user.id)
        assert db_user is not None
        assert db_user.first_name == "Зорге"
        assert db_user.username == "analyst_z"
        assert db_user.exp == 0

        # Verify response message
        msg = session.get_last_message_for_chat(analyst.user.id)
        assert msg is not None
        assert "ЛИЧНОЕ ДЕЛО ВОЕННОСЛУЖАЩЕГО" in msg.text
        assert "Зорге" in msg.text
        assert "🪖" in msg.text
        assert "Рядовой" in msg.text
        assert "<b>0</b> / <b>100</b> EXP" in msg.text
        assert "[░░░░░░░░░░] 0%" in msg.text

        # Verify attached keyboard is profile keyboard
        assert msg.reply_markup is not None
        buttons = [b for row in msg.reply_markup.inline_keyboard for b in row]
        assert any("обновить" in b.text.lower() for b in buttons)
        assert any("главное меню" in b.text.lower() for b in buttons)

    async def test_main_menu_profile_button_transitions_to_dossier(self, bot_env):
        """Verify clicking 'Мое досье' from /start edits message to profile dossier."""
        analyst = bot_env["analyst"]
        session = bot_env["session"]

        # Step 1: /start
        await analyst.send_command("/start")
        start_msg = session.get_last_message_for_chat(analyst.user.id)
        assert start_msg is not None
        assert "Главное меню" in start_msg.text or "Шпион и Аналитик" in start_msg.text

        # Step 2: Click profile button
        profile_cb = MenuCallback(action="profile").pack()
        await analyst.click_button(profile_cb, message=start_msg)

        # Message must be edited in-place
        edited_msg = session.get_last_message_for_chat(analyst.user.id)
        assert edited_msg is not None
        assert "ЛИЧНОЕ ДЕЛО ВОЕННОСЛУЖАЩЕГО" in edited_msg.text
        assert "Зорге" in edited_msg.text

        # Callback query answered
        assert len(session.callback_answers) >= 1

    async def test_profile_refresh_with_unmodified_shield(self, bot_env):
        """Verify clicking '🔄 Обновить данные' does not crash when content is unmodified."""
        analyst = bot_env["analyst"]
        session = bot_env["session"]

        # Step 1: Open profile via command
        await analyst.send_command("/profile")
        profile_msg = session.get_last_message_for_chat(analyst.user.id)
        assert profile_msg is not None

        # Step 2: Turn on unmodified error simulation in mock session
        session.simulate_unmodified_error = True

        # Step 3: Click refresh button
        refresh_cb = MenuCallback(action="profile").pack()
        # Should NOT raise TelegramBadRequest thanks to safe_edit_callback
        await analyst.click_button(refresh_cb, message=profile_msg)

        # Verify callback was acknowledged
        assert len(session.callback_answers) >= 1

    async def test_profile_back_to_main_menu_navigation(self, bot_env):
        """Verify clicking '◀️ Главное меню' from profile returns user to main menu."""
        analyst = bot_env["analyst"]
        session = bot_env["session"]

        # Open profile
        await analyst.send_command("/profile")
        profile_msg = session.get_last_message_for_chat(analyst.user.id)
        assert profile_msg is not None

        # Click main menu back button
        main_cb = MenuCallback(action="main").pack()
        await analyst.click_button(main_cb, message=profile_msg)

        # Verify main menu appears
        last_msg = session.get_last_message_for_chat(analyst.user.id)
        assert last_msg is not None
        assert "Главное меню" in last_msg.text
        assert any(
            "депешу" in b.text.lower()
            for row in last_msg.reply_markup.inline_keyboard
            for b in row
        )

    async def test_profile_html_escaping_for_special_characters(self, bot_env):
        """Verify user display name with HTML entities (<, >, &, \") is safely escaped."""
        from aiogram import types
        analyst = bot_env["analyst"]
        service: SessionService = bot_env["service"]

        db_user = await service.get_or_create_user(
            telegram_id=9999,
            username="hacker<one>",
            first_name="<b>Agent</b> & Co",
        )
        card = render_player_profile(db_user)
        assert "<script>" not in card
        assert "&lt;b&gt;Agent&lt;/b&gt; &amp; Co" in card

    async def test_profile_refresh_reflects_updated_stats_in_place(self, bot_env):
        """Verify that refreshing profile reflects newly accrued EXP and rank promotion in-place."""
        analyst = bot_env["analyst"]
        session = bot_env["session"]
        service: SessionService = bot_env["service"]

        # Open initial profile (Private, 0 EXP)
        await analyst.send_command("/profile")
        msg = session.get_last_message_for_chat(analyst.user.id)
        assert "Рядовой" in msg.text
        assert "🪖" in msg.text

        # Simulate gameplay progress: promote to Lieutenant (2500 EXP)
        async with service.session_factory() as db:
            from sqlalchemy import select
            res = await db.execute(select(User).where(User.id == analyst.user.id))
            u = res.scalar_one()
            u.exp = 2600
            u.games_played = 5
            u.games_won = 5
            u.games_as_analyst = 3
            u.games_as_agent = 2
            u.total_mistakes = 1
            u.total_hints_used = 0
            await db.commit()

        # Click refresh button
        refresh_cb = MenuCallback(action="profile").pack()
        await analyst.click_button(refresh_cb, message=msg)

        # Message is updated with new rank info
        updated_msg = session.get_last_message_for_chat(analyst.user.id)
        assert "Лейтенант" in updated_msg.text
        assert "⭐⭐" in updated_msg.text
        assert "<b>2600</b> / <b>3200</b> EXP" in updated_msg.text
        assert "Винрейт: <b>100.0%</b>" in updated_msg.text
        assert "В роли Аналитика: <b>3</b>" in updated_msg.text
        assert "В роли Агента: <b>2</b>" in updated_msg.text

    async def test_profile_handlers_defensive_against_missing_user_or_message(self, bot_env):
        """Verify handlers safely return without exceptions if Telegram user or message is None."""
        from unittest.mock import AsyncMock, MagicMock
        from bot.handlers.common import handle_profile_command, handle_profile_callback
        service: SessionService = bot_env["service"]

        # 1. Message without from_user
        mock_msg = MagicMock(spec=types.Message)
        mock_msg.from_user = None
        mock_msg.answer = AsyncMock()
        await handle_profile_command(mock_msg, session_service=service)
        mock_msg.answer.assert_not_called()

        # 2. CallbackQuery without from_user
        mock_cb = MagicMock(spec=types.CallbackQuery)
        mock_cb.from_user = None
        mock_cb.answer = AsyncMock()
        await handle_profile_callback(mock_cb, session_service=service)
        mock_cb.answer.assert_not_called()

        # 3. CallbackQuery with user but message is None
        mock_cb2 = MagicMock(spec=types.CallbackQuery)
        mock_cb2.from_user = types.User(id=3001, is_bot=False, first_name="Ghost")
        mock_cb2.message = None
        mock_cb2.answer = AsyncMock()
        await handle_profile_callback(mock_cb2, session_service=service)
        mock_cb2.answer.assert_awaited_once()

