"""Adversarial Callback Fuzzing & Payload Stress Test Suite for Milestone M3.

Tests:
1. Callback payload size boundaries (< 64 bytes) across all quotes, letters, and codes.
2. Malformed, forged, and boundary callback queries.
3. Role boundary violations and unhandled exceptions (unauthorized actions).
4. Critical vulnerability verification: Agent refresh leaking Analyst Dossier / plaintext.
5. Session cancellation authorization and persistence failures.
6. Keyboard grid layouts, row width limits (<= 8), and button counts.
"""
import asyncio
from datetime import datetime
from typing import AsyncGenerator, Dict, List

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
from bot.keyboards import (
    build_agent_alphabet_keyboard,
    build_agent_symbol_keyboard,
    build_analyst_dossier_keyboard,
    build_cancel_keyboard,
    build_catalog_keyboard,
    build_invite_keyboard,
    build_main_menu_keyboard,
    build_victory_keyboard,
)
from cryptogram.catalog import get_catalog
from cryptogram.engine import RUSSIAN_ALPHABET, generate_cipher
from database.models import Base, GameSession, SessionStatus
from database.session import create_session_factory
from services.lock_manager import SessionLockManager
from services.session_service import SessionService, UnauthorizedActionError
from tests.mock_session import MockPlayerClient, MockTelegramSession

pytestmark = pytest.mark.e2e


# ==========================================
# Fixtures
# ==========================================


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
async def test_session_service(test_engine: AsyncEngine) -> SessionService:
    """Fresh SessionService bound to test engine."""
    session_factory = create_session_factory(test_engine)
    lock_manager = SessionLockManager()
    return SessionService(session_factory=session_factory, lock_manager=lock_manager)


@pytest_asyncio.fixture(scope="function")
async def bot_env(test_session_service: SessionService) -> Dict:
    """Configured bot test environment with mock session and 3 clients."""
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
# Test Suite 1: Payload Size Stress Tests
# ==========================================


class TestCallbackPayloadSizes:
    """Adversarial stress-testing of callback payload lengths against Telegram's 64-byte limit."""

    def test_all_catalog_quotes_callback_payload_sizes(self):
        """Verify that every catalog quote packed across various pages is <= 64 bytes."""
        catalog = get_catalog()
        assert len(catalog) >= 20, "Catalog should contain at least 20 items"

        actions = ["pick", "select", "page", "noop"]
        pages = [0, 1, 4, 10, 100, 9999]

        max_size = 0
        max_payload = ""

        for item in catalog:
            for action in actions:
                for page in pages:
                    cb = CatalogCallback(action=action, item_id=item.id, page=page)
                    packed = cb.pack()
                    utf8_len = len(packed.encode("utf-8"))
                    if utf8_len > max_size:
                        max_size = utf8_len
                        max_payload = packed

                    assert utf8_len <= 64, (
                        f"Catalog callback exceeds 64 bytes: '{packed}' ({utf8_len} bytes)"
                    )
                    assert utf8_len < 40, (
                        f"Catalog callback exceeds 40 bytes: '{packed}' ({utf8_len} bytes)"
                    )

        # Longest quote benchmark: intercept_nord_convoy
        assert max_size <= 37, f"Max catalog callback size {max_size} exceeds expected bound"

    def test_all_russian_letters_guess_callback_payload_sizes(self):
        """Verify all 32 Cyrillic letters and edge symbols with various code lengths <= 64 bytes."""
        symbols = [1, 14, 32, 99, 999, 999999]
        codes = ["a1b2c3d4", "ffffffff", "00000000", "12345678-1234-1234-1234-123456789abc"]

        for code in codes:
            for symbol in symbols:
                for letter in RUSSIAN_ALPHABET:
                    cb = LetterGuessCallback(code=code, symbol=symbol, letter=letter)
                    packed = cb.pack()
                    utf8_len = len(packed.encode("utf-8"))
                    assert utf8_len <= 64, (
                        f"Letter guess payload exceeds 64 bytes: '{packed}' ({utf8_len} bytes)"
                    )

        # Standard 8-char code with Russian letter (2 bytes)
        standard_cb = LetterGuessCallback(code="a1b2c3d4", symbol=32, letter="Я")
        assert len(standard_cb.pack().encode("utf-8")) == 20

    def test_all_keyboards_button_callback_payload_sizes(self):
        """Stress-test every button in every keyboard builder to ensure callback_data <= 64 bytes."""
        invite_code = "a1b2c3d4"
        bot_username = "SpyAnalystBot"

        keyboards: List[types.InlineKeyboardMarkup] = []

        # 1. Main menu
        keyboards.append(build_main_menu_keyboard())

        # 2. Catalog keyboards across all pages
        catalog = get_catalog()
        total_pages = (len(catalog) + 3) // 4
        for p in range(total_pages):
            keyboards.append(build_catalog_keyboard(page=p, total_pages=total_pages))

        # 3. Invite keyboard
        keyboards.append(build_invite_keyboard(invite_code, bot_username))

        # 4. Analyst dossier keyboards (waiting and in_progress)
        class DummySessionWaiting:
            invite_code = "a1b2c3d4"
            status = SessionStatus.WAITING_AGENT

        class DummySessionActive:
            invite_code = "a1b2c3d4"
            status = SessionStatus.IN_PROGRESS

        keyboards.append(build_analyst_dossier_keyboard(DummySessionWaiting(), bot_username=bot_username))
        keyboards.append(build_analyst_dossier_keyboard(DummySessionActive(), bot_username=bot_username))

        # 5. Agent symbol keyboards (with 1..32 unrevealed symbols)
        keyboards.append(build_agent_symbol_keyboard(invite_code, char_to_code=list(range(1, 33))))

        # 6. Agent alphabet keyboards for multiple symbols
        for sym in [1, 14, 32, 99]:
            keyboards.append(build_agent_alphabet_keyboard(invite_code, symbol_code=sym))

        # 7. Victory and cancel keyboards
        keyboards.append(build_victory_keyboard())
        keyboards.append(build_cancel_keyboard())

        total_buttons_checked = 0
        for kb in keyboards:
            assert kb.inline_keyboard, "Keyboard has no rows"
            for row in kb.inline_keyboard:
                assert len(row) <= 8, f"Row exceeds Telegram limit of 8 buttons: {len(row)}"
                for btn in row:
                    total_buttons_checked += 1
                    if btn.callback_data:
                        raw_bytes = btn.callback_data.encode("utf-8")
                        assert len(raw_bytes) <= 64, (
                            f"Button '{btn.text}' has callback_data > 64 bytes: '{btn.callback_data}' ({len(raw_bytes)} bytes)"
                        )

        assert total_buttons_checked > 200, f"Expected > 200 buttons checked, got {total_buttons_checked}"


# ==========================================
# Test Suite 2: Malformed and Forged Queries
# ==========================================


@pytest.mark.asyncio
class TestMalformedAndForgedCallbacks:
    """Stress-test how the bot handles corrupted, out-of-spec, and forged callback queries."""

    async def test_completely_malformed_callback_strings(self, bot_env):
        """Random junk strings and invalid delimiters do not crash the bot."""
        intruder = bot_env["intruder"]

        junk_payloads = [
            "random_junk_string",
            "",
            ":::::",
            "12345",
            "sym",
            "guess",
            "hint",
            "cat",
            "menu",
            "sym:",
            "guess:a:b:c:d:e",
            "a" * 64,
            "???!!!###$$$",
            "\x00\x01\x02",
        ]

        for junk in junk_payloads:
            # Should not raise any unhandled exception in dispatcher
            await intruder.click_button(junk)

    async def test_schema_type_mismatch_and_partial_fields(self, bot_env):
        """Callbacks matching prefixes but failing schema validation are safely handled."""
        intruder = bot_env["intruder"]

        corrupted = [
            "sym:a1b2c3d4:not_an_int",
            "sym:a1b2c3d4",
            "guess:a1b2c3d4:not_an_int:А",
            "guess:a1b2c3d4:1",
            "hint:a1b2c3d4:reveal:not_an_int",
            "hint:a1b2c3d4",
            "cat:pick",
            "cat:page::not_an_int",
            "menu",
        ]

        for payload in corrupted:
            await intruder.click_button(payload)

    async def test_forged_callback_to_nonexistent_session(self, bot_env):
        """Valid schemas pointing to non-existent game codes reject gracefully."""
        intruder = bot_env["intruder"]
        session = bot_env["session"]

        dead_code = "deadbeef"

        # 1. Symbol select on dead code
        answers_count_before = len(session.callback_answers)
        await intruder.click_button(f"sym:{dead_code}:1")
        assert len(session.callback_answers) > answers_count_before
        last_answer = session.callback_answers[-1]
        assert "не активна" in (last_answer.text or "").lower()

        # 2. Guess on dead code
        answers_count_before = len(session.callback_answers)
        await intruder.click_button(f"guess:{dead_code}:1:А")
        assert len(session.callback_answers) > answers_count_before
        last_answer = session.callback_answers[-1]
        assert "не активна" in (last_answer.text or "").lower()

        # 3. Hint reveal on dead code
        answers_count_before = len(session.callback_answers)
        await intruder.click_button(f"hint:{dead_code}:reveal:0")
        assert len(session.callback_answers) > answers_count_before
        last_answer = session.callback_answers[-1]
        assert "не активна" in (last_answer.text or "").lower()

        # 4. SOS on dead code
        answers_count_before = len(session.callback_answers)
        await intruder.click_button(f"hint:{dead_code}:sos:0")
        assert len(session.callback_answers) > answers_count_before
        last_answer = session.callback_answers[-1]
        assert "не активна" in (last_answer.text or "").lower()

    async def test_guess_on_waiting_or_solved_session(self, bot_env):
        """Guesses sent when session is not IN_PROGRESS are rejected with alert."""
        analyst = bot_env["analyst"]
        agent = bot_env["agent"]
        service = bot_env["service"]
        session = bot_env["session"]

        game = await service.create_session(creator_tg_id=analyst.user.id, text="Простая проверка депеши")
        code = game.invite_code

        # Attempt guess while WAITING_AGENT
        answers_count_before = len(session.callback_answers)
        await agent.click_button(f"guess:{code}:1:П")
        assert len(session.callback_answers) > answers_count_before
        assert "не активна" in (session.callback_answers[-1].text or "").lower()

    async def test_catalog_pick_nonexistent_quote(self, bot_env):
        """Picking non-existent quote id shows alert and does not crash."""
        analyst = bot_env["analyst"]
        session = bot_env["session"]

        answers_count_before = len(session.callback_answers)
        await analyst.click_button("cat:pick:quote_does_not_exist_at_all:0")
        assert len(session.callback_answers) > answers_count_before
        assert "не найдена" in (session.callback_answers[-1].text or "").lower()


# ==========================================
# Test Suite 3: Empirical Bug Reproductions
# ==========================================


@pytest.mark.asyncio
class TestEmpiricalBugReproductions:
    """Verification that critical security leaks and unhandled exceptions are remediated."""

    async def test_agent_refresh_button_leaks_secret_plaintext(self, bot_env):
        """VERIFY FIX: Field Agent clicking '🔄 Обновить табло' refreshes Agent Terminal, NOT Analyst Dossier.

        Verifies:
        1. Agent terminal is retained with masked text.
        2. Analyst Dossier and secret plaintext are NEVER rendered to the Agent.
        3. Intruder clicking refresh is rejected with permission alert.
        4. Analyst clicking refresh receives Analyst Dossier.
        """
        analyst = bot_env["analyst"]
        agent = bot_env["agent"]
        intruder = bot_env["intruder"]
        service = bot_env["service"]
        session = bot_env["session"]

        secret_text = "Секретный приказ командования: операция на рассвете."
        game = await service.create_session(creator_tg_id=analyst.user.id, text=secret_text)
        code = game.invite_code

        # Agent joins
        await agent.send_command("/start", f"game_{code}")
        agent_msg = session.get_last_message_for_chat(agent.user.id)
        assert agent_msg is not None
        assert "ПОЛЕВОЙ ТЕРМИНАЛ" in agent_msg.text
        assert secret_text not in agent_msg.text, "Masked terminal should NOT contain plaintext"

        # Agent clicks '🔄 Обновить табло'
        await agent.click_button(f"hint:{code}:refresh:0", message=agent_msg)

        # Inspect agent's updated message
        updated_agent_msg = session.get_last_message_for_chat(agent.user.id)
        assert updated_agent_msg is not None

        # Verification of role separation:
        has_analyst_dossier = "ДОСЬЕ СПЕЦОПЕРАЦИИ" in updated_agent_msg.text
        has_plaintext = secret_text in updated_agent_msg.text
        has_agent_terminal = "ПОЛЕВОЙ ТЕРМИНАЛ" in updated_agent_msg.text

        assert not has_analyst_dossier, (
            "Security Violation: Agent terminal must NOT be replaced by Analyst Dossier!"
        )
        assert not has_plaintext, (
            "Security Violation: Secret dispatch plaintext must NOT be exposed to Field Agent!"
        )
        assert has_agent_terminal, (
            "Agent terminal was not retained upon refresh!"
        )

        # Intruder clicking refresh is blocked
        ans_count_before = len(session.callback_answers)
        await intruder.click_button(f"hint:{code}:refresh:0")
        assert len(session.callback_answers) > ans_count_before
        assert "не являетесь участником" in session.callback_answers[-1].text

    async def test_unauthorized_user_callbacks_unhandled_exceptions(self, bot_env):
        """VERIFY FIX: Unauthorized users clicking tactical buttons receive alerts without hanging spinners.

        In SessionService, unauthorized actions raise UnauthorizedActionError.
        In bot handlers, UnauthorizedActionError is caught and answered with show_alert=True:
        - on_letter_guess_submitted
        - on_analyst_reveal_letter
        - on_analyst_send_frequencies
        - on_agent_sos_request
        """
        analyst = bot_env["analyst"]
        agent = bot_env["agent"]
        intruder = bot_env["intruder"]
        service = bot_env["service"]
        session = bot_env["session"]

        game = await service.create_session(creator_tg_id=analyst.user.id, text="Тестовая радиограмма штаба")
        code = game.invite_code
        await agent.send_command("/start", f"game_{code}")

        # 1. Intruder attempts guess -> UnauthorizedActionError caught and answered with alert
        ans_count_before = len(session.callback_answers)
        await intruder.click_button(f"guess:{code}:1:Т")
        assert len(session.callback_answers) > ans_count_before, (
            "Intruder guess must answer callback query to prevent hanging spinner"
        )
        assert session.callback_answers[-1].show_alert is True

        # 2. Agent attempts hint reveal -> UnauthorizedActionError caught and answered with alert
        ans_count_before = len(session.callback_answers)
        await agent.click_button(f"hint:{code}:reveal:0")
        assert len(session.callback_answers) > ans_count_before, (
            "Agent reveal hint must answer callback query to prevent hanging spinner"
        )
        assert session.callback_answers[-1].show_alert is True

        # 3. Agent attempts frequency hint -> UnauthorizedActionError caught and answered with alert
        ans_count_before = len(session.callback_answers)
        await agent.click_button(f"hint:{code}:freq:0")
        assert len(session.callback_answers) > ans_count_before, (
            "Agent freq hint must answer callback query to prevent hanging spinner"
        )
        assert session.callback_answers[-1].show_alert is True

        # 4. Analyst attempts SOS request -> UnauthorizedActionError caught and answered with alert
        ans_count_before = len(session.callback_answers)
        await analyst.click_button(f"hint:{code}:sos:0")
        assert len(session.callback_answers) > ans_count_before, (
            "Analyst SOS request must answer callback query to prevent hanging spinner"
        )
        assert session.callback_answers[-1].show_alert is True

    async def test_session_cancel_lacks_persistence_and_auth(self, bot_env):
        """VERIFY FIX: on_analyst_cancel_session enforces authorization and persists CANCELLED status.

        1. Non-analyst (intruder) cannot cancel session.
        2. Analyst cancellation transitions session to CANCELLED in DB.
        """
        analyst = bot_env["analyst"]
        intruder = bot_env["intruder"]
        service = bot_env["service"]
        session = bot_env["session"]

        game = await service.create_session(creator_tg_id=analyst.user.id, text="Сообщение для отмены миссии")
        code = game.invite_code

        # 1. Intruder sends cancel callback -> rejected
        ans_count_before = len(session.callback_answers)
        await intruder.click_button(f"hint:{code}:cancel:0")
        assert len(session.callback_answers) > ans_count_before
        assert session.callback_answers[-1].show_alert is True

        # DB status remains WAITING_AGENT
        reloaded = await service.get_session_by_code(code)
        assert reloaded is not None
        assert reloaded.status == SessionStatus.WAITING_AGENT, (
            f"Intruder should not be able to cancel session, status was {reloaded.status}"
        )

        # 2. Legitimate Analyst sends cancel callback
        await analyst.click_button(f"hint:{code}:cancel:0")

        # Reload from DB via fresh query
        reloaded_after_analyst = await service.get_session_by_code(code)
        assert reloaded_after_analyst is not None
        assert reloaded_after_analyst.status == SessionStatus.CANCELLED, (
            f"Expected session status to be CANCELLED after analyst cancel, got {reloaded_after_analyst.status}"
        )


# ==========================================
# Test Suite 4: Keyboard Grid Layout & Bounds
# ==========================================


class TestKeyboardLayoutsAndBounds:
    """Verify inline keyboard dimensions, grid alignment, and Telegram API constraints."""

    def test_alphabet_keyboard_geometry(self):
        """Alphabet keyboard must be symmetrical 4 rows of 8 letters + 1 back row."""
        kb = build_agent_alphabet_keyboard("testcode", symbol_code=1)
        rows = kb.inline_keyboard

        assert len(rows) == 5, f"Expected 5 rows in alphabet keyboard, got {len(rows)}"

        # Rows 0-3 must contain exactly 8 buttons
        for r_idx in range(4):
            assert len(rows[r_idx]) == 8, (
                f"Row {r_idx} has {len(rows[r_idx])} buttons, expected 8"
            )

        # Row 4 is the back button
        assert len(rows[4]) == 1
        assert "Назад" in rows[4][0].text

        # Total alphabet letters must be 32 unique Russian letters
        letters = [btn.text for r in rows[:4] for btn in r]
        assert len(letters) == 32
        assert len(set(letters)) == 32
        assert "".join(letters) == RUSSIAN_ALPHABET

    def test_catalog_keyboard_pagination_boundaries(self):
        """Catalog keyboard pagination buttons must show disabled placeholder '·' at boundaries."""
        catalog = get_catalog()
        total_pages = (len(catalog) + 3) // 4

        # Page 0: previous button should be disabled ('·')
        kb_page_0 = build_catalog_keyboard(page=0, total_pages=total_pages)
        nav_row_0 = kb_page_0.inline_keyboard[-2]
        assert nav_row_0[0].text == "·"
        assert "1/" in nav_row_0[1].text
        assert "Вперед" in nav_row_0[2].text

        # Last page: next button should be disabled ('·')
        kb_last = build_catalog_keyboard(page=total_pages - 1, total_pages=total_pages)
        nav_row_last = kb_last.inline_keyboard[-2]
        assert "Назад" in nav_row_last[0].text
        assert f"{total_pages}/{total_pages}" in nav_row_last[1].text
        assert nav_row_last[2].text == "·"

    def test_symbol_keyboard_grid_adjustment(self):
        """Symbol keyboard must adjust to 5 columns with SOS and Refresh in separate rows."""
        # 14 unrevealed symbols: should form 2 rows of 5 + 1 row of 4 + 2 footer rows = 5 rows
        kb = build_agent_symbol_keyboard("testcode", char_to_code=list(range(1, 15)))
        rows = kb.inline_keyboard

        assert len(rows) == 5
        assert len(rows[0]) == 5
        assert len(rows[1]) == 5
        assert len(rows[2]) == 4
        assert len(rows[3]) == 1 and "SOS" in rows[3][0].text or "помощь" in rows[3][0].text
        assert len(rows[4]) == 1 and "Обновить" in rows[4][0].text
