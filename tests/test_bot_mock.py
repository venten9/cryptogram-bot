"""Tier 4 Mock Integration Test Suite for Aiogram 3.x Cooperative Cryptogram Bot.

Tests all UI mechanics, router workflows, FSM states, cross-player notifications,
error shields, and complete game playthroughs without real Telegram bot tokens or network calls.
"""
from datetime import datetime
from typing import AsyncGenerator, Dict

import pytest
import pytest_asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from bot.app import create_dispatcher
from bot.callbacks import (
    CatalogCallback,
    HintActionCallback,
    LetterGuessCallback,
    MenuCallback,
    SymbolSelectCallback,
)
from bot.utils import safe_edit_message
from cryptogram.catalog import get_catalog
from cryptogram.engine import RUSSIAN_ALPHABET, get_unrevealed_codes
from database.models import Base, GameSession, HintType, SessionStatus
from database.session import create_session_factory
from services.lock_manager import SessionLockManager
from services.session_service import SessionService
from tests.mock_session import MockPlayerClient, MockTelegramSession

# Re-export MockTelegramSession and MockPlayerClient for test runner discovery
__all__ = ["MockTelegramSession", "MockPlayerClient"]


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
    """Pre-configured bot test environment with dispatcher, mock session, and 3 player clients."""
    mock_session = MockTelegramSession()
    bot = Bot(token="42:MOCK_TOKEN", session=mock_session)
    dp = create_dispatcher(
        session_service=test_session_service,
        lock_manager=test_session_service.lock_manager,
        bot_username="SpyAnalystBot",
        storage=MemoryStorage(),
    )

    analyst = MockPlayerClient(
        dp, bot, user_id=1001, username="analyst_hq", first_name="Штабной"
    )
    agent = MockPlayerClient(
        dp, bot, user_id=1002, username="field_agent", first_name="Агент 007"
    )
    intruder = MockPlayerClient(
        dp, bot, user_id=1003, username="intruder", first_name="Взломщик"
    )

    return {
        "dp": dp,
        "bot": bot,
        "session": mock_session,
        "service": test_session_service,
        "analyst": analyst,
        "agent": agent,
        "intruder": intruder,
    }


# ==========================================
# Test Cases
# ==========================================


@pytest.mark.e2e
@pytest.mark.asyncio
class TestBotMockSuite:

    async def test_start_welcome_menu(self, bot_env):
        """S1: Plain /start command returns welcome menu with interactive buttons."""
        analyst = bot_env["analyst"]
        session = bot_env["session"]

        await analyst.send_command("/start")

        msg = session.get_last_message_for_chat(analyst.user.id)
        assert msg is not None
        assert "Шпион и Аналитик" in msg.text
        assert msg.reply_markup is not None
        # Verify main menu buttons present
        button_texts = [
            btn.text
            for row in msg.reply_markup.inline_keyboard
            for btn in row
        ]
        assert any("каталог" in t.lower() for t in button_texts)
        assert any("свой текст" in t.lower() for t in button_texts)
        assert any("правила" in t.lower() for t in button_texts)

    async def test_help_command_and_callback(self, bot_env):
        """S2: /help command and menu:help callback display operational rules."""
        analyst = bot_env["analyst"]
        session = bot_env["session"]

        await analyst.send_command("/help")
        msg = session.get_last_message_for_chat(analyst.user.id)
        assert msg is not None
        assert "ПРАВИЛА ИГРЫ" in msg.text
        assert "Штабного Аналитика" in msg.text
        assert "Полевого Агента" in msg.text

        # Test callback query path as well
        await analyst.click_button(MenuCallback(action="help").pack(), message=msg)
        msg2 = session.get_last_message_for_chat(analyst.user.id)
        assert "ПРАВИЛА ИГРЫ" in msg2.text

    async def test_cancel_command_clears_state(self, bot_env):
        """S3: /cancel command resets FSM state and presents main menu."""
        analyst = bot_env["analyst"]
        session = bot_env["session"]

        await analyst.send_command("/start")
        start_msg = session.get_last_message_for_chat(analyst.user.id)

        # Trigger custom text prompt to enter FSM state
        await analyst.click_button_by_text("свой текст", start_msg)
        prompt_msg = session.get_last_message_for_chat(analyst.user.id)
        assert "Ввод депеши" in prompt_msg.text

        # Now cancel
        await analyst.send_command("/cancel")
        cancel_msg = session.get_last_message_for_chat(analyst.user.id)
        assert "отменено" in cancel_msg.text.lower()
        assert cancel_msg.reply_markup is not None

    async def test_analyst_select_from_catalog(self, bot_env):
        """S4: Analyst browses catalog, selects quote, and generates invite link."""
        analyst = bot_env["analyst"]
        session = bot_env["session"]
        service = bot_env["service"]

        await analyst.send_command("/start")
        start_msg = session.get_last_message_for_chat(analyst.user.id)

        # Click catalog
        await analyst.click_button_by_text("каталог", start_msg)
        catalog_msg = session.get_last_message_for_chat(analyst.user.id)
        assert "КАТАЛОГ СЕКРЕТНЫХ ДЕПЕШ" in catalog_msg.text

        # Select first quote
        first_item = get_catalog()[0]
        pick_cb = CatalogCallback(action="pick", item_id=first_item.id, page=0).pack()
        await analyst.click_button(pick_cb, message=catalog_msg)

        dossier_msg = session.get_last_message_for_chat(analyst.user.id)
        assert "ДОСЬЕ СПЕЦОПЕРАЦИИ" in dossier_msg.text
        assert "t.me/SpyAnalystBot?start=game_" in dossier_msg.text

        # Verify game created in DB
        code = dossier_msg.text.split("game_")[1][:8]
        game = await service.get_session_by_code(code)
        assert game is not None
        assert game.status == SessionStatus.WAITING_AGENT
        assert game.analyst_id == analyst.user.id
        assert game.catalog_id == first_item.id

    async def test_catalog_pagination(self, bot_env):
        """S5: Catalog pagination navigates forward and backward."""
        analyst = bot_env["analyst"]
        session = bot_env["session"]

        await analyst.send_command("/start")
        start_msg = session.get_last_message_for_chat(analyst.user.id)
        await analyst.click_button_by_text("каталог", start_msg)
        cat_msg = session.get_last_message_for_chat(analyst.user.id)
        assert "Стр. 1/" in cat_msg.text

        # Go to page 2
        await analyst.click_button_by_text("вперед", cat_msg)
        cat_msg2 = session.get_last_message_for_chat(analyst.user.id)
        assert "Стр. 2/" in cat_msg2.text

        # Go back to page 1
        await analyst.click_button_by_text("назад", cat_msg2)
        cat_msg3 = session.get_last_message_for_chat(analyst.user.id)
        assert "Стр. 1/" in cat_msg3.text

    async def test_analyst_custom_text_fsm(self, bot_env):
        """S6: Analyst inputs custom phrase through FSM with validation."""
        analyst = bot_env["analyst"]
        session = bot_env["session"]
        service = bot_env["service"]

        await analyst.send_command("/start")
        start_msg = session.get_last_message_for_chat(analyst.user.id)
        await analyst.click_button_by_text("свой текст", start_msg)

        # 1. Invalid input: too short (< 10 chars)
        await analyst.send_message("Коротко")
        err_msg = session.get_last_message_for_chat(analyst.user.id)
        assert "Ошибка валидации" in err_msg.text

        # 2. Invalid input: Latin letters
        await analyst.send_message("English dispatch secret 12345")
        err_msg2 = session.get_last_message_for_chat(analyst.user.id)
        assert "Ошибка валидации" in err_msg2.text

        # 3. Valid input
        valid_phrase = "СЕКРЕТНЫЙ РАЗВЕДЦЕНТР НА СВЯЗИ."
        await analyst.send_message(valid_phrase)
        dossier_msg = session.get_last_message_for_chat(analyst.user.id)
        assert "ДОСЬЕ СПЕЦОПЕРАЦИИ" in dossier_msg.text
        assert "t.me/SpyAnalystBot?start=game_" in dossier_msg.text

        # Verify DB
        code = dossier_msg.text.split("game_")[1][:8]
        game = await service.get_session_by_code(code)
        assert game is not None
        assert game.is_custom_quote is True
        assert game.original_text == valid_phrase
        assert game.status == SessionStatus.WAITING_AGENT

    async def test_agent_join_deeplink_and_notification(self, bot_env):
        """S7: Agent joins via deep-link; Analyst notified; Agent receives Field Terminal."""
        analyst = bot_env["analyst"]
        agent = bot_env["agent"]
        session = bot_env["session"]
        service = bot_env["service"]

        game = await service.create_session(analyst.user.id, "СЕКРЕТНАЯ ДЕПЕША")
        code = game.invite_code

        # Save analyst message coordinates to receive update
        dummy_msg = types.Message(
            message_id=500,
            date=datetime.now(),
            chat=analyst.chat,
            from_user=analyst.user,
            text="Analyst Dossier",
        )
        session.messages[500] = dummy_msg
        await service.update_message_ids(
            game.id, analyst_chat_id=analyst.chat.id, analyst_message_id=500
        )

        # Agent joins via deep link
        await agent.send_command("/start", f"game_{code}")

        # Verify Agent received terminal
        agent_msg = session.get_last_message_for_chat(agent.user.id)
        assert agent_msg is not None
        assert "ПОЛЕВОЙ ТЕРМИНАЛ" in agent_msg.text
        assert "<pre>" in agent_msg.text
        assert agent_msg.reply_markup is not None

        # Verify Analyst was notified
        analyst_last = session.get_last_message_for_chat(analyst.user.id)
        assert "подключился к операции" in analyst_last.text

        # Verify DB status
        reloaded = await service.get_session_by_code(code)
        assert reloaded.status == SessionStatus.IN_PROGRESS
        assert reloaded.agent_id == agent.user.id

    async def test_creator_cannot_join_own_game(self, bot_env):
        """S8: Analyst cannot join own game as Field Agent."""
        analyst = bot_env["analyst"]
        session = bot_env["session"]
        service = bot_env["service"]

        game = await service.create_session(analyst.user.id, "ТЕСТОВАЯ ДЕПЕША")
        await analyst.send_command("/start", f"game_{game.invite_code}")

        msg = session.get_last_message_for_chat(analyst.user.id)
        assert "Штабным Аналитиком" in msg.text
        assert "не может выступать в роли Агента" in msg.text

    async def test_third_player_cannot_join_full_game(self, bot_env):
        """S9: Third player joining full game is rejected."""
        analyst = bot_env["analyst"]
        agent = bot_env["agent"]
        intruder = bot_env["intruder"]
        session = bot_env["session"]
        service = bot_env["service"]

        game = await service.create_session(analyst.user.id, "ОПЕРАЦИЯ БАРС")
        await service.join_session(game.invite_code, agent.user.id)

        await intruder.send_command("/start", f"game_{game.invite_code}")
        msg = session.get_last_message_for_chat(intruder.user.id)
        assert "уже подключился другой Агент" in msg.text

    async def test_agent_symbol_and_alphabet_view(self, bot_env):
        """S10: Agent selects cipher symbol -> displays 32 Cyrillic alphabet picker."""
        analyst = bot_env["analyst"]
        agent = bot_env["agent"]
        session = bot_env["session"]
        service = bot_env["service"]

        game = await service.create_session(analyst.user.id, "СНЕГ ИДЕТ")
        await service.join_session(game.invite_code, agent.user.id)

        # Setup virtual message for Agent
        terminal_msg = types.Message(
            message_id=700,
            date=datetime.now(),
            chat=agent.chat,
            from_user=agent.user,
            text="Initial Terminal",
        )
        session.messages[700] = terminal_msg

        # Agent clicks symbol 1
        sym_cb = SymbolSelectCallback(code=game.invite_code, symbol=1).pack()
        await agent.click_button(sym_cb, message=terminal_msg)

        edited_msg = session.messages[700]
        assert "Подстановка для символа [1]" in edited_msg.text
        assert edited_msg.reply_markup is not None

        # Verify all 32 Cyrillic letters are present on the keyboard
        alphabet_buttons = [
            btn.text
            for row in edited_msg.reply_markup.inline_keyboard
            for btn in row
        ]
        for letter in RUSSIAN_ALPHABET:
            assert letter in alphabet_buttons, f"Letter {letter} missing from alphabet picker"

    async def test_agent_back_to_symbol_grid(self, bot_env):
        """S11: Agent clicks back button -> returns to symbol grid."""
        analyst = bot_env["analyst"]
        agent = bot_env["agent"]
        session = bot_env["session"]
        service = bot_env["service"]

        game = await service.create_session(analyst.user.id, "СНЕГ ИДЕТ")
        await service.join_session(game.invite_code, agent.user.id)

        terminal_msg = types.Message(
            message_id=701,
            date=datetime.now(),
            chat=agent.chat,
            from_user=agent.user,
            text="Alphabet View",
        )
        session.messages[701] = terminal_msg
        await service.update_message_ids(game.id, agent_chat_id=agent.chat.id, agent_message_id=701)

        # Click back button (symbol=0)
        back_cb = SymbolSelectCallback(code=game.invite_code, symbol=0).pack()
        await agent.click_button(back_cb, message=terminal_msg)

        edited = session.messages[701]
        assert "ПОЛЕВОЙ ТЕРМИНАЛ" in edited.text
        assert "<pre>" in edited.text

    async def test_agent_wrong_guess(self, bot_env):
        """S12: Agent submits wrong guess -> alert feedback and mistake count increment."""
        analyst = bot_env["analyst"]
        agent = bot_env["agent"]
        session = bot_env["session"]
        service = bot_env["service"]

        game = await service.create_session(analyst.user.id, "ПОЕХАЛИ")
        await service.join_session(game.invite_code, agent.user.id)

        terminal_msg = types.Message(
            message_id=702,
            date=datetime.now(),
            chat=agent.chat,
            from_user=agent.user,
            text="Terminal",
        )
        session.messages[702] = terminal_msg
        await service.update_message_ids(game.id, agent_chat_id=agent.chat.id, agent_message_id=702)

        # Symbol 1 true char
        code_to_char = game.code_to_char_map
        correct_letter = code_to_char[1]
        wrong_letter = "Я" if correct_letter != "Я" else "Ю"

        guess_cb = LetterGuessCallback(
            code=game.invite_code, symbol=1, letter=wrong_letter
        ).pack()
        await agent.click_button(guess_cb, message=terminal_msg)

        # Verify callback answer was recorded
        assert len(session.callback_answers) > 0
        last_answer = session.callback_answers[-1]
        assert "Неверно" in last_answer.text

        # Verify DB mistakes incremented
        reloaded = await service.get_session_by_id(game.id)
        assert reloaded.mistakes_count == 1

    async def test_agent_correct_guess(self, bot_env):
        """S13: Agent submits correct guess -> symbol revealed across board and syncs."""
        analyst = bot_env["analyst"]
        agent = bot_env["agent"]
        session = bot_env["session"]
        service = bot_env["service"]

        game = await service.create_session(analyst.user.id, "ШТАБ ЦЕНТР")
        await service.join_session(game.invite_code, agent.user.id)

        terminal_msg = types.Message(
            message_id=703,
            date=datetime.now(),
            chat=agent.chat,
            from_user=agent.user,
            text="Terminal",
        )
        dossier_msg = types.Message(
            message_id=503,
            date=datetime.now(),
            chat=analyst.chat,
            from_user=analyst.user,
            text="Dossier",
        )
        session.messages[703] = terminal_msg
        session.messages[503] = dossier_msg
        await service.update_message_ids(
            game.id,
            analyst_chat_id=analyst.chat.id,
            analyst_message_id=503,
            agent_chat_id=agent.chat.id,
            agent_message_id=703,
        )

        code_to_char = game.code_to_char_map
        correct_letter = code_to_char[1]

        guess_cb = LetterGuessCallback(
            code=game.invite_code, symbol=1, letter=correct_letter
        ).pack()
        await agent.click_button(guess_cb, message=terminal_msg)

        # Check DB
        reloaded = await service.get_session_by_id(game.id)
        assert "1" in reloaded.guessed_mapping
        assert reloaded.guessed_mapping["1"] == correct_letter

        # Check Agent terminal updated
        updated_terminal = session.messages[703]
        assert correct_letter in updated_terminal.text

    async def test_analyst_letter_reveal_hint(self, bot_env):
        """S14: Analyst sends letter reveal hint -> updates Agent terminal and Analyst dossier."""
        analyst = bot_env["analyst"]
        agent = bot_env["agent"]
        session = bot_env["session"]
        service = bot_env["service"]

        game = await service.create_session(analyst.user.id, "СИГНАЛ ПРИНЯТ")
        await service.join_session(game.invite_code, agent.user.id)

        session.messages[704] = types.Message(
            message_id=704, date=datetime.now(), chat=agent.chat, from_user=agent.user, text="Terminal"
        )
        session.messages[504] = types.Message(
            message_id=504, date=datetime.now(), chat=analyst.chat, from_user=analyst.user, text="Dossier"
        )
        await service.update_message_ids(
            game.id,
            analyst_chat_id=analyst.chat.id,
            analyst_message_id=504,
            agent_chat_id=agent.chat.id,
            agent_message_id=704,
        )

        # Analyst clicks reveal hint
        hint_cb = HintActionCallback(code=game.invite_code, action="reveal", symbol=0).pack()
        await analyst.click_button(hint_cb, message=session.messages[504])

        reloaded = await service.get_session_by_id(game.id)
        assert reloaded.hints_used == 1
        assert len(reloaded.guessed_mapping) == 1

        # Agent terminal shows hint alert
        agent_terminal = session.messages[704]
        assert "Штаб передал разведданные" in agent_terminal.text

    async def test_analyst_frequency_report_hint(self, bot_env):
        """S15: Analyst sends frequency report -> separate message delivered to Agent."""
        analyst = bot_env["analyst"]
        agent = bot_env["agent"]
        session = bot_env["session"]
        service = bot_env["service"]

        game = await service.create_session(analyst.user.id, "ШИФРОВАННЫЙ ТЕКСТ")
        await service.join_session(game.invite_code, agent.user.id)

        session.messages[505] = types.Message(
            message_id=505, date=datetime.now(), chat=analyst.chat, from_user=analyst.user, text="Dossier"
        )
        await service.update_message_ids(
            game.id, analyst_chat_id=analyst.chat.id, analyst_message_id=505, agent_chat_id=agent.chat.id
        )

        freq_cb = HintActionCallback(code=game.invite_code, action="freq", symbol=0).pack()
        await analyst.click_button(freq_cb, message=session.messages[505])

        # Verify radio message sent to agent chat
        agent_msgs = session.get_messages_for_chat(agent.user.id)
        assert any("РАДИОГРАММА ИЗ ШТАБА" in m.text for m in agent_msgs)

    async def test_agent_emergency_sos(self, bot_env):
        """S16: Agent requests SOS -> Analyst receives alert message and DB logs request."""
        analyst = bot_env["analyst"]
        agent = bot_env["agent"]
        session = bot_env["session"]
        service = bot_env["service"]

        game = await service.create_session(analyst.user.id, "СОС ПРОВЕРКА")
        await service.join_session(game.invite_code, agent.user.id)

        session.messages[706] = types.Message(
            message_id=706, date=datetime.now(), chat=agent.chat, from_user=agent.user, text="Terminal"
        )
        await service.update_message_ids(
            game.id, analyst_chat_id=analyst.chat.id, agent_chat_id=agent.chat.id, agent_message_id=706
        )

        sos_cb = HintActionCallback(code=game.invite_code, action="sos", symbol=0).pack()
        await agent.click_button(sos_cb, message=session.messages[706])

        # Verify Analyst received SOS alert
        analyst_msgs = session.get_messages_for_chat(analyst.user.id)
        assert any("СИГНАЛ SOS" in m.text for m in analyst_msgs)

        # Verify HintLog in DB
        reloaded = await service.get_session_by_id(game.id)
        assert any(h.hint_type == HintType.AGENT_SOS for h in reloaded.hints)

    async def test_safe_edit_message_unmodified_shield(self, bot_env):
        """S17: safe_edit_message gracefully suppresses TelegramBadRequest: message is not modified."""
        bot = bot_env["bot"]
        session = bot_env["session"]
        session.simulate_unmodified_error = True

        # Pre-seed a message
        session.messages[888] = types.Message(
            message_id=888,
            date=datetime.now(),
            chat=types.Chat(id=1001, type="private"),
            from_user=types.User(id=bot.id, is_bot=True, first_name="Bot"),
            text="Exact same text",
            reply_markup=None,
        )

        # Edit with identical content
        result = await safe_edit_message(
            bot=bot,
            chat_id=1001,
            message_id=888,
            text="Exact same text",
            reply_markup=None,
        )

        # Must return False and NOT raise an exception
        assert result is False

    async def test_full_cooperative_playthrough_to_victory(self, bot_env):
        """S18: Full end-to-end cooperative game from creation to victory!"""
        analyst = bot_env["analyst"]
        agent = bot_env["agent"]
        session = bot_env["session"]
        service = bot_env["service"]

        phrase = "ПОЕХАЛИ"
        # 1. Analyst creates game
        game = await service.create_session(analyst.user.id, phrase)
        code = game.invite_code

        # Seed Analyst dossier message in session store
        analyst_dossier_msg = types.Message(
            message_id=555,
            date=datetime.now(),
            chat=analyst.chat,
            from_user=analyst.user,
            text="Dossier Initial",
        )
        session.messages[555] = analyst_dossier_msg
        await service.update_message_ids(
            game.id, analyst_chat_id=analyst.chat.id, analyst_message_id=555
        )

        # 2. Agent joins via deep link
        await agent.send_command("/start", f"game_{code}")

        # Re-fetch game to get updated message IDs
        game = await service.get_session_by_code(code)
        assert game.status == SessionStatus.IN_PROGRESS
        agent_msg = session.get_last_message_for_chat(agent.user.id)

        # 3. Step through all unique cipher codes and guess correctly
        code_to_char = game.code_to_char_map
        all_codes = sorted(code_to_char.keys())

        # Solve all except the last one via Agent guesses
        for sym_code in all_codes[:-1]:
            letter = code_to_char[sym_code]
            guess_cb = LetterGuessCallback(code=code, symbol=sym_code, letter=letter).pack()
            await agent.click_button(guess_cb, message=agent_msg)

        # Solve final symbol via Analyst hint to test victory trigger from hint flow
        last_code = all_codes[-1]
        hint_cb = HintActionCallback(code=code, action="reveal", symbol=last_code).pack()
        await analyst.click_button(hint_cb, message=session.messages[555])

        # 4. Assert victory state
        final_game = await service.get_session_by_code(code)
        assert final_game.status == SessionStatus.SOLVED

        analyst_dossier = session.messages[555]
        agent_last = session.get_last_message_for_chat(agent.user.id)

        assert "ПОБЕДА" in agent_last.text or "ЗАВЕРШЕНА" in agent_last.text
        assert "УСПЕШНО ЗАВЕРШЕНА" in analyst_dossier.text or "ПОБЕДА" in analyst_dossier.text
        assert phrase in agent_last.text
        assert phrase in analyst_dossier.text
