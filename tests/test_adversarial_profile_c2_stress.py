"""Challenger 2 Empirical Stress Test Suite for Milestone 3 Bot Handlers and Callbacks.

Verifies:
1. Repeated rapid clicks on refresh callback ('safe_edit_callback' stability under
   TelegramBadRequest 'message is not modified' and high frequency).
2. Deep navigation loops between main menu and profile dossier.
3. Auto-registration on /profile command and inline callbacks for brand new users.
4. Edge conditions: missing message context, dynamic EXP mutations, and special characters.
"""
import asyncio
from datetime import datetime, timezone
from typing import AsyncGenerator, Dict, List

import pytest
import pytest_asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

from bot.app import create_dispatcher
from bot.callbacks import MenuCallback
from bot.handlers.common import render_player_profile
from bot.keyboards import build_main_menu_keyboard, build_profile_keyboard
from bot.utils import safe_edit_callback
from database.models import Base, User
from database.session import create_session_factory
from services.lock_manager import SessionLockManager
from services.session_service import SessionService
from tests.mock_session import MockPlayerClient, MockTelegramSession


# ==============================================================================
# Fixtures
# ==============================================================================


@pytest_asyncio.fixture(scope="function")
async def c2_engine() -> AsyncGenerator[AsyncEngine, None]:
    """In-memory SQLite async engine with StaticPool for test isolation."""
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
async def c2_session_service(c2_engine: AsyncEngine) -> SessionService:
    """Fresh SessionService instance backed by in-memory SQLite."""
    session_factory = create_session_factory(c2_engine)
    lock_manager = SessionLockManager()
    return SessionService(session_factory=session_factory, lock_manager=lock_manager)


@pytest_asyncio.fixture(scope="function")
async def c2_bot_env(c2_session_service: SessionService) -> Dict:
    """Configured bot test harness with mock session, dispatcher, and clients."""
    mock_session = MockTelegramSession()
    bot = Bot(token="42:MOCK_C2_TOKEN", session=mock_session)
    dp = create_dispatcher(
        session_service=c2_session_service,
        lock_manager=c2_session_service.lock_manager,
        bot_username="SpyAnalystBot",
        storage=MemoryStorage(),
    )

    client_a = MockPlayerClient(
        dp, bot, user_id=4001, username="c2_alpha", first_name="Зорге_2"
    )
    client_b = MockPlayerClient(
        dp, bot, user_id=4002, username="c2_beta", first_name="Абель_2"
    )

    return {
        "dp": dp,
        "bot": bot,
        "session": mock_session,
        "service": c2_session_service,
        "alpha": client_a,
        "beta": client_b,
    }


# ==============================================================================
# 1. Repeated Rapid Clicks on Refresh Callback (safe_edit_callback)
# ==============================================================================


@pytest.mark.asyncio
class TestC2RapidRefreshCallbacks:
    """Empirically stress-test repeated rapid clicks on refresh callback."""

    async def test_rapid_sequential_clicks_unmodified(self, c2_bot_env):
        """50 sequential clicks on refresh when stats have not changed."""
        alpha: MockPlayerClient = c2_bot_env["alpha"]
        session: MockTelegramSession = c2_bot_env["session"]

        # Step 1: Open profile
        await alpha.send_command("/profile")
        initial_msg = session.get_last_message_for_chat(alpha.user.id)
        assert initial_msg is not None

        # Turn on Telegram unmodified error simulation
        session.simulate_unmodified_error = True
        refresh_cb = MenuCallback(action="profile").pack()

        # Step 2: Spam 50 clicks
        for _ in range(50):
            await alpha.click_button(refresh_cb, message=initial_msg)

        # Verification: All 50 callback queries answered, no unhandled exceptions
        assert len(session.callback_answers) == 50
        # Message content is preserved
        current_msg = session.get_last_message_for_chat(alpha.user.id)
        assert current_msg is not None
        assert "ЛИЧНОЕ ДЕЛО ВОЕННОСЛУЖАЩЕГО" in current_msg.text
        assert "Зорге_2" in current_msg.text

    async def test_concurrent_rapid_clicks_unmodified(self, c2_bot_env):
        """20 concurrent clicks on refresh callback via asyncio.gather."""
        alpha: MockPlayerClient = c2_bot_env["alpha"]
        session: MockTelegramSession = c2_bot_env["session"]

        await alpha.send_command("/profile")
        initial_msg = session.get_last_message_for_chat(alpha.user.id)
        assert initial_msg is not None

        session.simulate_unmodified_error = True
        refresh_cb = MenuCallback(action="profile").pack()

        # Fire 20 concurrent clicks
        tasks = [alpha.click_button(refresh_cb, message=initial_msg) for _ in range(20)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for res in results:
            assert not isinstance(res, Exception), f"Concurrent click failed: {res}"

        assert len(session.callback_answers) == 20

    async def test_interleaved_refresh_clicks_with_dynamic_exp_growth(self, c2_bot_env):
        """Simulate rapid clicks interspersed with dynamic EXP increases."""
        alpha: MockPlayerClient = c2_bot_env["alpha"]
        session: MockTelegramSession = c2_bot_env["session"]
        service: SessionService = c2_bot_env["service"]

        await alpha.send_command("/profile")
        initial_msg = session.get_last_message_for_chat(alpha.user.id)
        assert initial_msg is not None
        session.simulate_unmodified_error = True
        refresh_cb = MenuCallback(action="profile").pack()

        # Iterate over rank promotions: Private (0) -> Corporal (100) -> Junior Sgt (250) -> Sgt (450)
        exp_steps = [120, 300, 500, 1500]
        expected_ranks = ["Ефрейтор", "Младший сержант", "Сержант", "Прапорщик"]

        for exp_val, rank_name in zip(exp_steps, expected_ranks):
            # 3 clicks when unmodified
            for _ in range(3):
                await alpha.click_button(refresh_cb, message=initial_msg)

            # Mutate EXP in DB directly
            async with service.session_factory() as db:
                stmt = select(User).where(User.id == alpha.user.id)
                res = await db.execute(stmt)
                user = res.scalar_one()
                user.exp = exp_val
                await db.commit()

            # Click refresh: content changed, update must apply in-place
            await alpha.click_button(refresh_cb, message=initial_msg)

            updated_msg = session.get_last_message_for_chat(alpha.user.id)
            assert updated_msg is not None
            assert rank_name in updated_msg.text
            assert f"Опыт: <b>{exp_val}</b>" in updated_msg.text

    async def test_safe_edit_callback_handles_missing_message(self):
        """Ensure safe_edit_callback returns False gracefully when callback.message is None."""
        user = types.User(id=1, is_bot=False, first_name="Test")
        cb = types.CallbackQuery(
            id="cb_nomessage",
            from_user=user,
            chat_instance="ci",
            data="menu:profile",
            message=None,
        )
        res = await safe_edit_callback(cb, "New text")
        assert res is False


# ==============================================================================
# 2. Navigation Loops Between Main Menu and Profile
# ==============================================================================


@pytest.mark.asyncio
class TestC2NavigationLoops:
    """Empirically test deep navigation loops between Main Menu and Profile."""

    async def test_deep_loop_main_to_profile(self, c2_bot_env):
        """Loop 25 times: Main Menu -> Profile Dossier -> Main Menu -> Profile Dossier."""
        alpha: MockPlayerClient = c2_bot_env["alpha"]
        session: MockTelegramSession = c2_bot_env["session"]

        # Step 1: Open main menu
        await alpha.send_command("/start")
        msg = session.get_last_message_for_chat(alpha.user.id)
        assert msg is not None

        profile_cb = MenuCallback(action="profile").pack()
        main_cb = MenuCallback(action="main").pack()

        # Loop 25 full cycles
        for _ in range(25):
            # Navigate to Profile
            await alpha.click_button(profile_cb, message=msg)
            current_msg = session.get_last_message_for_chat(alpha.user.id)
            assert "ЛИЧНОЕ ДЕЛО ВОЕННОСЛУЖАЩЕГО" in current_msg.text
            assert any(
                "обновить" in b.text.lower()
                for row in current_msg.reply_markup.inline_keyboard
                for b in row
            )

            # Navigate back to Main Menu
            await alpha.click_button(main_cb, message=current_msg)
            menu_msg = session.get_last_message_for_chat(alpha.user.id)
            assert "Главное меню" in menu_msg.text
            assert any(
                "досье" in b.text.lower()
                for row in menu_msg.reply_markup.inline_keyboard
                for b in row
            )
            msg = menu_msg

        # Total callback answers = 50 (25 * 2)
        assert len(session.callback_answers) == 50
        # In the existing bot architecture, handle_main_menu sends a fresh menu
        # while handle_profile_callback edits in-place.
        # Exactly 1 initial message + 25 returned main menu messages = 26 messages.
        total_chat_msgs = session.get_messages_for_chat(alpha.user.id)
        assert len(total_chat_msgs) == 26

    async def test_mixed_command_and_callback_navigation(self, c2_bot_env):
        """Mixed traversal: /start -> /profile -> main button -> profile button -> /help -> /profile."""
        alpha: MockPlayerClient = c2_bot_env["alpha"]
        session: MockTelegramSession = c2_bot_env["session"]

        # 1. /start
        await alpha.send_command("/start")
        # 2. /profile
        await alpha.send_command("/profile")
        p_msg = session.get_last_message_for_chat(alpha.user.id)
        assert "ЛИЧНОЕ ДЕЛО ВОЕННОСЛУЖАЩЕГО" in p_msg.text

        # 3. Click back to main
        main_cb = MenuCallback(action="main").pack()
        await alpha.click_button(main_cb, message=p_msg)
        m_msg = session.get_last_message_for_chat(alpha.user.id)
        assert "Главное меню" in m_msg.text

        # 4. Click profile button from main
        profile_cb = MenuCallback(action="profile").pack()
        await alpha.click_button(profile_cb, message=m_msg)
        p_msg2 = session.get_last_message_for_chat(alpha.user.id)
        assert "ЛИЧНОЕ ДЕЛО ВОЕННОСЛУЖАЩЕГО" in p_msg2.text

        # 5. Send /help
        await alpha.send_command("/help")
        h_msg = session.get_last_message_for_chat(alpha.user.id)
        assert "правила" in h_msg.text.lower() or "шпион" in h_msg.text.lower()

        # 6. Return to profile via /profile
        await alpha.send_command("/profile")
        p_msg3 = session.get_last_message_for_chat(alpha.user.id)
        assert "ЛИЧНОЕ ДЕЛО ВОЕННОСЛУЖАЩЕГО" in p_msg3.text


# ==============================================================================
# 3. Auto-Registration for Brand New Users
# ==============================================================================


@pytest.mark.asyncio
class TestC2AutoRegistrationNewUsers:
    """Empirically test auto-registration behaviors across diverse user profiles."""

    async def test_new_user_minimal_attributes(self, c2_bot_env):
        """Brand new user with no username, no last_name, and empty first_name."""
        dp: Dispatcher = c2_bot_env["dp"]
        bot: Bot = c2_bot_env["bot"]
        session: MockTelegramSession = c2_bot_env["session"]
        service: SessionService = c2_bot_env["service"]

        raw_user = types.User(id=999111, is_bot=False, first_name="", username=None)
        client = MockPlayerClient(dp, bot, user_id=999111)
        client.user = raw_user

        # Send /profile
        await client.send_command("/profile")

        # Verify DB registration
        user = await service.get_or_create_user(999111)
        assert user is not None
        assert user.id == 999111
        assert user.exp == 0
        assert user.games_played == 0
        assert user.display_name == "Agent_999111"

        # Verify response text
        msg = session.get_last_message_for_chat(999111)
        assert msg is not None
        assert "Agent_999111" in msg.text
        assert "🪖 Рядовой" in msg.text
        assert "• Опыт: <b>0</b> / <b>100</b> EXP" in msg.text
        assert "<code>[░░░░░░░░░░] 0%</code>" in msg.text

    async def test_new_user_special_html_characters(self, c2_bot_env):
        """Brand new user with HTML formatting and injection strings."""
        dp: Dispatcher = c2_bot_env["dp"]
        bot: Bot = c2_bot_env["bot"]
        session: MockTelegramSession = c2_bot_env["session"]

        raw_user = types.User(
            id=999222,
            is_bot=False,
            first_name="<b>Agent<script></b>",
            last_name="& 'Exploit' \"",
            username="hack_&_slash",
        )
        client = MockPlayerClient(dp, bot, user_id=999222)
        client.user = raw_user

        await client.send_command("/profile")

        msg = session.get_last_message_for_chat(999222)
        assert msg is not None
        assert "<script>" not in msg.text
        assert "&lt;script&gt;" in msg.text
        assert "&amp;" in msg.text

    async def test_new_user_first_interaction_via_inline_callback(self, c2_bot_env):
        """Brand new user whose first interaction is clicking inline profile button."""
        dp: Dispatcher = c2_bot_env["dp"]
        bot: Bot = c2_bot_env["bot"]
        session: MockTelegramSession = c2_bot_env["session"]
        service: SessionService = c2_bot_env["service"]

        client = MockPlayerClient(
            dp, bot, user_id=999333, username="inliner", first_name="Инлайн"
        )

        simulated_msg = types.Message(
            message_id=5000,
            date=datetime.now(timezone.utc),
            chat=client.chat,
            from_user=types.User(
                id=bot.id,
                is_bot=True,
                first_name="CryptogramBot",
                username="SpyAnalystBot",
            ),
            text="Вам переслано сообщение",
        )
        session.messages[5000] = simulated_msg

        profile_cb = MenuCallback(action="profile").pack()
        await client.click_button(profile_cb, message=simulated_msg)

        user = await service.get_or_create_user(999333)
        assert user is not None
        assert user.username == "inliner"
        assert user.first_name == "Инлайн"

        updated = session.messages[5000]
        assert "ЛИЧНОЕ ДЕЛО ВОЕННОСЛУЖАЩЕГО" in updated.text
        assert "Инлайн" in updated.text

    async def test_max_rank_marshal_card_details(self, c2_bot_env):
        """User with extreme EXP (50,000 EXP) displays Marshal with full bar and no next EXP."""
        alpha: MockPlayerClient = c2_bot_env["alpha"]
        session: MockTelegramSession = c2_bot_env["session"]
        service: SessionService = c2_bot_env["service"]

        async with service.session_factory() as db:
            u = User(
                id=alpha.user.id,
                username=alpha.user.username,
                first_name=alpha.user.first_name,
                exp=50000,
                games_played=120,
                games_won=115,
                games_as_analyst=60,
                games_as_agent=60,
                total_mistakes=10,
                total_hints_used=5,
            )
            db.add(u)
            await db.commit()

        await alpha.send_command("/profile")
        msg = session.get_last_message_for_chat(alpha.user.id)
        assert msg is not None
        assert "👑⭐ Маршал" in msg.text
        assert "• Опыт: <b>50000</b> EXP (Максимальное звание)" in msg.text
        assert "<code>[██████████] 100%</code>" in msg.text
        assert "Винрейт: <b>95.8%</b>" in msg.text
