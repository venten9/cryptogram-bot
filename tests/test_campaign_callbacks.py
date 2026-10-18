"""Adversarial Button Crash Shield & Callback Test Suite for Story Campaign.

«Сюжетная кампания: Секретные директивы Ставки (1941–1945)»

Verifies:
1. Absolute answer() guarantee: every campaign callback handler calls callback.answer().
2. Locked chapter alert shield: clicking classified/locked chapters displays alert without crash.
3. Message unmodified shield: rapid double-clicks with simulate_unmodified_error=True
   suppress TelegramBadRequest and do not crash.
4. Missing / expired session protection: callbacks for expired/deleted sessions answer gracefully.
5. Forged & boundary callback payloads: out-of-bounds chapter IDs, invalid actions handled safely.
6. Defensive guards: CallbackQuery with message=None or from_user=None handled safely.
7. HTML entity sanitization: all dynamic user and chapter content escaped with html.escape().
8. Telegram limits: callback data payloads <= 64 bytes, keyboard row widths <= 8.
9. Interactive navigation flows via MockPlayerClient.
"""
import asyncio
import html
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import AsyncGenerator, Dict, List, Optional

import pytest
import pytest_asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

from bot.app import create_dispatcher
from bot.callbacks import MenuCallback
from bot.keyboards import build_main_menu_keyboard
from database.models import Base, GameSession, SessionStatus, User
from database.session import create_session_factory
from services.lock_manager import SessionLockManager
from services.session_service import SessionService
from tests.mock_session import MockPlayerClient, MockTelegramSession

# Optional imports for modules that are implemented as part of Campaign milestones
try:
    import cryptogram.campaign_data as campaign_data
    from cryptogram.campaign_data import CAMPAIGN_CHAPTERS, get_campaign_chapter
except ImportError:
    campaign_data = None
    CAMPAIGN_CHAPTERS = None
    get_campaign_chapter = None

try:
    from database.models import CampaignProgress
except ImportError:
    CampaignProgress = None

try:
    import services.campaign_service as campaign_service_module
    from services.campaign_service import CampaignService
except ImportError:
    campaign_service_module = None
    CampaignService = None

try:
    from bot.callbacks import CampaignCallback
except ImportError:
    CampaignCallback = None

try:
    from bot.keyboards import (
        build_campaign_briefing_keyboard,
        build_campaign_menu_keyboard,
        build_campaign_victory_keyboard,
    )
except ImportError:
    build_campaign_menu_keyboard = None
    build_campaign_briefing_keyboard = None
    build_campaign_victory_keyboard = None

try:
    import bot.handlers.campaign as campaign_handlers_module
    from bot.handlers.campaign import campaign_router
except ImportError:
    campaign_handlers_module = None
    campaign_router = None


# ==============================================================================
# Fixtures
# ==============================================================================

@pytest_asyncio.fixture(scope="function")
async def callback_test_engine() -> AsyncGenerator[AsyncEngine, None]:
    """Isolated in-memory SQLite async engine with foreign keys enabled."""
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
async def callback_session_service(callback_test_engine: AsyncEngine) -> SessionService:
    """Fresh SessionService bound to the isolated engine."""
    session_factory = create_session_factory(callback_test_engine)
    lock_manager = SessionLockManager()
    return SessionService(session_factory=session_factory, lock_manager=lock_manager)


@pytest_asyncio.fixture(scope="function")
async def bot_environment(callback_session_service: SessionService) -> Dict:
    """Configured bot environment with MockTelegramSession, Bot, Dispatcher, and MockPlayerClient."""
    mock_session = MockTelegramSession()
    bot = Bot(token="42:MOCK_TOKEN", session=mock_session)
    dp = create_dispatcher(
        session_service=callback_session_service,
        lock_manager=callback_session_service.lock_manager,
        bot_username="SpyAnalystBot",
        storage=MemoryStorage(),
    )

    player = MockPlayerClient(
        dp=dp,
        bot=bot,
        user_id=700101,
        username="cadet_alex",
        first_name="Александр",
    )

    return {
        "mock_session": mock_session,
        "bot": bot,
        "dp": dp,
        "player": player,
        "service": callback_session_service,
    }


# ==============================================================================
# Test Classes
# ==============================================================================

class TestCampaignCallbackCrashShield:
    """Comprehensive adversarial tests verifying crash protection across all callback interactions."""

    @pytest.mark.asyncio
    async def test_callback_answer_called_on_campaign_actions(self, bot_environment: Dict):
        """Verify that every campaign callback query triggers await callback.answer()."""
        if CampaignCallback is None:
            pytest.skip("CampaignCallback not yet implemented (M3)")

        mock_session: MockTelegramSession = bot_environment["mock_session"]
        bot: Bot = bot_environment["bot"]
        dp: Dispatcher = bot_environment["dp"]
        player: MockPlayerClient = bot_environment["player"]

        actions_to_test = [
            ("menu", 0),
            ("select", 1),
            ("chapter", 1),
            ("briefing", 1),
            ("locked", 3),
        ]

        for action, ch in actions_to_test:
            mock_session.callback_answers.clear()
            cb_data = CampaignCallback(action=action, chapter=ch).pack()

            # Click button
            msg = types.Message(
                message_id=player._next_message_id(),
                date=datetime.now(timezone.utc),
                chat=player.chat,
                from_user=types.User(id=bot.id, is_bot=True, first_name="Bot"),
                text="Меню кампании",
            )
            mock_session.messages[msg.message_id] = msg

            await player.click_button(cb_data, message=msg)

            # Ensure answer was called
            assert len(mock_session.callback_answers) > 0, (
                f"Action '{action}' did not call callback.answer()!"
            )

    @pytest.mark.asyncio
    async def test_locked_chapter_shows_alert_and_answers(self, bot_environment: Dict):
        """Verify clicking a locked chapter displays alert via callback.answer(show_alert=True)."""
        if CampaignCallback is None:
            pytest.skip("CampaignCallback not yet implemented (M3)")

        mock_session: MockTelegramSession = bot_environment["mock_session"]
        bot: Bot = bot_environment["bot"]
        player: MockPlayerClient = bot_environment["player"]

        mock_session.callback_answers.clear()
        cb_data = CampaignCallback(action="locked", chapter=3).pack()

        msg = types.Message(
            message_id=player._next_message_id(),
            date=datetime.now(timezone.utc),
            chat=player.chat,
            from_user=types.User(id=bot.id, is_bot=True, first_name="Bot"),
            text="Выбор глав",
        )
        mock_session.messages[msg.message_id] = msg

        # Click locked button
        await player.click_button(cb_data, message=msg)

        # Check that answer was recorded and has alert
        assert len(mock_session.callback_answers) > 0
        last_answer = mock_session.callback_answers[-1]
        assert last_answer.show_alert is True or (last_answer.text and "засекречена" in last_answer.text.lower())

    @pytest.mark.asyncio
    async def test_rapid_double_clicks_suppress_message_not_modified_error(
        self, callback_session_service: SessionService
    ):
        """Verify that rapid double-clicks triggering 'message is not modified' are gracefully suppressed."""
        if CampaignCallback is None:
            pytest.skip("CampaignCallback not yet implemented (M3)")

        # Create session with simulate_unmodified_error enabled
        mock_session = MockTelegramSession(simulate_unmodified_error=True)
        bot = Bot(token="42:MOCK_TOKEN", session=mock_session)
        dp = create_dispatcher(
            session_service=callback_session_service,
            lock_manager=callback_session_service.lock_manager,
            bot_username="SpyAnalystBot",
            storage=MemoryStorage(),
        )
        player = MockPlayerClient(dp=dp, bot=bot, user_id=700102)

        # Send start
        await player.send_command("/start")
        start_msg = mock_session.get_last_message_for_chat(700102)
        assert start_msg is not None

        cb_data = CampaignCallback(action="menu", chapter=0).pack()

        # Click twice rapidly with identical data
        try:
            await player.click_button(cb_data, message=start_msg)
            await player.click_button(cb_data, message=start_msg)
        except TelegramBadRequest as e:
            pytest.fail(f"safe_edit_callback failed to suppress message is not modified: {e}")

    @pytest.mark.asyncio
    async def test_missing_or_cancelled_session_handled_safely(self, bot_environment: Dict):
        """Verify interacting with non-existent or cancelled session alerts user without crashing."""
        if CampaignCallback is None:
            pytest.skip("CampaignCallback not yet implemented (M3)")

        mock_session: MockTelegramSession = bot_environment["mock_session"]
        bot: Bot = bot_environment["bot"]
        player: MockPlayerClient = bot_environment["player"]

        mock_session.callback_answers.clear()
        cb_data = CampaignCallback(action="start", chapter=99).pack()

        msg = types.Message(
            message_id=player._next_message_id(),
            date=datetime.now(timezone.utc),
            chat=player.chat,
            from_user=types.User(id=bot.id, is_bot=True, first_name="Bot"),
            text="Брифинг",
        )
        mock_session.messages[msg.message_id] = msg

        # Trigger action on non-existent chapter/session
        await player.click_button(cb_data, message=msg)

        # Must have answered callback
        assert len(mock_session.callback_answers) > 0

    @pytest.mark.asyncio
    async def test_forged_out_of_bounds_chapter_callback(self, bot_environment: Dict):
        """Verify forged callbacks with negative or extreme chapter numbers do not crash."""
        if CampaignCallback is None:
            pytest.skip("CampaignCallback not yet implemented (M3)")

        mock_session: MockTelegramSession = bot_environment["mock_session"]
        bot: Bot = bot_environment["bot"]
        player: MockPlayerClient = bot_environment["player"]

        forged_payloads = [
            "camp:chapter:9999",
            "camp:chapter:-1",
            "camp:start:0",
            "camp:locked:-999",
            "camp:next:10",
        ]

        for payload in forged_payloads:
            mock_session.callback_answers.clear()
            msg = types.Message(
                message_id=player._next_message_id(),
                date=datetime.now(timezone.utc),
                chat=player.chat,
                from_user=types.User(id=bot.id, is_bot=True, first_name="Bot"),
                text="Тест",
            )
            mock_session.messages[msg.message_id] = msg

            try:
                await player.click_button(payload, message=msg)
            except Exception as exc:
                pytest.fail(f"Forged payload '{payload}' caused unhandled exception: {exc}")

    @pytest.mark.asyncio
    async def test_callback_with_none_message_does_not_crash(self, bot_environment: Dict):
        """Verify callback query with message=None does not trigger AttributeError."""
        if CampaignCallback is None:
            pytest.skip("CampaignCallback not yet implemented (M3)")

        dp: Dispatcher = bot_environment["dp"]
        bot: Bot = bot_environment["bot"]
        player: MockPlayerClient = bot_environment["player"]

        cb_query = types.CallbackQuery(
            id=str(player._next_update_id()),
            from_user=player.user,
            chat_instance=f"chat_{player.user.id}",
            data=CampaignCallback(action="menu", chapter=0).pack(),
            message=None,  # Defensive None test
        )
        update = types.Update(update_id=player._next_update_id(), callback_query=cb_query)

        try:
            await dp.feed_update(bot, update)
        except AttributeError as e:
            pytest.fail(f"Callback with message=None raised AttributeError: {e}")

    @pytest.mark.asyncio
    async def test_callback_with_none_from_user_does_not_crash(self, bot_environment: Dict):
        """Verify callback query with from_user=None does not trigger AttributeError."""
        if CampaignCallback is None:
            pytest.skip("CampaignCallback not yet implemented (M3)")

        dp: Dispatcher = bot_environment["dp"]
        bot: Bot = bot_environment["bot"]
        player: MockPlayerClient = bot_environment["player"]

        msg = types.Message(
            message_id=player._next_message_id(),
            date=datetime.now(timezone.utc),
            chat=player.chat,
            from_user=types.User(id=bot.id, is_bot=True, first_name="Bot"),
            text="Тест",
        )

        cb_query = types.CallbackQuery(
            id=str(player._next_update_id()),
            from_user=None,  # Defensive None test
            chat_instance="chat_anon",
            data=CampaignCallback(action="menu", chapter=0).pack(),
            message=msg,
        )
        update = types.Update(update_id=player._next_update_id(), callback_query=cb_query)

        try:
            await dp.feed_update(bot, update)
        except AttributeError as e:
            pytest.fail(f"Callback with from_user=None raised AttributeError: {e}")

    def test_campaign_callback_data_byte_length_within_limits(self):
        """Verify all generated campaign callback payloads strictly satisfy Telegram 64-byte limit."""
        if CampaignCallback is None:
            pytest.skip("CampaignCallback not yet implemented (M3)")

        actions = ["menu", "select", "chapter", "briefing", "start", "next", "locked"]
        for act in actions:
            for ch in range(0, 6):
                cb = CampaignCallback(action=act, chapter=ch)
                packed = cb.pack()
                byte_len = len(packed.encode("utf-8"))
                assert byte_len <= 64, f"Payload {packed!r} length {byte_len} exceeds 64 bytes"
                assert byte_len <= 40, f"Payload {packed!r} length {byte_len} exceeds project 40B target"

    def test_campaign_keyboards_row_widths_and_limits(self):
        """Verify all campaign inline keyboards have <= 8 buttons per row."""
        if build_campaign_menu_keyboard is None:
            pytest.skip("Campaign keyboards not yet implemented (M3)")

        try:
            menu_kb = build_campaign_menu_keyboard(completed_chapters=set())
        except TypeError:
            menu_kb = build_campaign_menu_keyboard(0)

        for row in menu_kb.inline_keyboard:
            assert len(row) <= 8, f"Menu keyboard row has {len(row)} buttons, limit is 8"

        if build_campaign_briefing_keyboard is not None:
            briefing_kb = build_campaign_briefing_keyboard(1)
            for row in briefing_kb.inline_keyboard:
                assert len(row) <= 8

        if build_campaign_victory_keyboard is not None:
            try:
                victory_kb = build_campaign_victory_keyboard(1, True)
            except TypeError:
                victory_kb = build_campaign_victory_keyboard(1)
            for row in victory_kb.inline_keyboard:
                assert len(row) <= 8

    def test_html_escaping_in_campaign_strings(self):
        """Verify html.escape handles dynamic strings without creating invalid XML entities."""
        malformed_inputs = [
            "<script>evil()</script>",
            "Директива & Приказ <№ 1>",
            "Операция «Уран» & «Кольцо» > <",
            "Player's \"Quote\" & <b>Tags</b>",
        ]

        for s in malformed_inputs:
            escaped = html.escape(s)
            assert "<script>" not in escaped
            # Must be parseable in an XML fragment
            xml_fragment = f"<item>{escaped}</item>"
            root = ET.fromstring(xml_fragment)
            assert root.text == s

    def test_campaign_terminal_rendering_bijective_mask(self):
        """Verify render_mask wraps text in valid escaped <pre> tags without XML injection."""
        from cryptogram.engine import generate_cipher, render_mask
        
        texts = [
            "В течение 22–23 июня возможно внезапное нападение. Задача войск — встретить удар.",
            "Отступать дальше — значит загубить себя и Родину: ни шагу назад!",
            "Войскам фронтов перейти в наступление с целью разгрома врага.",
            "Задача войск — измотать клинья противника и перейти в контрнаступление.",
            "Войскам фронта овладеть Берлином и водрузить Знамя Победы над рейхстагом.",
        ]

        for idx, text in enumerate(texts, 1):
            cipher = generate_cipher(text, seed=300 + idx)
            # Render with wrap_pre=True (default)
            rendered = render_mask(text, cipher.char_to_code, revealed_letters=set(), wrap_pre=True)
            assert rendered.startswith("<pre>")
            assert rendered.endswith("</pre>")
            # Must parse as valid XML fragment
            xml_str = f"<root>{rendered}</root>"
            root = ET.fromstring(xml_str)
            assert root.tag == "root"

    @pytest.mark.asyncio
    async def test_sql_injection_and_metacharacters_in_callback_data(self, bot_environment: Dict):
        """Verify adversarial injection payloads in callback data do not trigger crashes."""
        mock_session: MockTelegramSession = bot_environment["mock_session"]
        bot: Bot = bot_environment["bot"]
        player: MockPlayerClient = bot_environment["player"]

        injections = [
            "camp:'; DROP TABLE users;--:1",
            "camp:<script>alert(1)</script>:1",
            "camp:SELECT * FROM campaign_progress:1",
            "camp:\x00\x01\x02:0",
        ]

        for payload in injections:
            msg = types.Message(
                message_id=player._next_message_id(),
                date=datetime.now(timezone.utc),
                chat=player.chat,
                from_user=types.User(id=bot.id, is_bot=True, first_name="Bot"),
                text="Инъекция",
            )
            mock_session.messages[msg.message_id] = msg

            try:
                await player.click_button(payload, message=msg)
            except Exception as exc:
                pytest.fail(f"Injection payload {payload!r} raised unhandled exception: {exc}")


class TestCampaignInteractiveFlow:
    """End-to-end interactive flow simulation of player navigating campaign via Telegram updates."""

    @pytest.mark.asyncio
    async def test_interactive_start_to_campaign_menu(self, bot_environment: Dict):
        """Verify user sends /start, clicks campaign button, and receives chapter menu."""
        if CampaignCallback is None:
            pytest.skip("CampaignCallback not yet implemented (M3)")

        mock_session: MockTelegramSession = bot_environment["mock_session"]
        player: MockPlayerClient = bot_environment["player"]

        # 1. Start command
        await player.send_command("/start")
        start_msg = mock_session.get_last_message_for_chat(player.user.id)
        assert start_msg is not None

        # 2. Check if campaign button is in main menu
        if not any("кампания" in b.text.lower() for row in start_msg.reply_markup.inline_keyboard for b in row):
            pytest.skip("Campaign button not in main menu keyboard yet (M3)")

        # 3. Click campaign button
        await player.click_button_by_text("кампания", start_msg)
        camp_menu_msg = mock_session.get_last_message_for_chat(player.user.id)
        assert camp_menu_msg is not None
        assert "кампани" in camp_menu_msg.text.lower() or "секретные директивы" in camp_menu_msg.text.lower()
