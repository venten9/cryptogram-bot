"""Adversarial stress testing and empirical multi-player simulation for Milestone M3.

Tests:
1. Multi-player concurrent games simulation: multiple complete games played
   simultaneously by different user pairs via MockPlayerClient and dp.feed_update.
2. Edge case testing:
   - User trying to guess on finished (SOLVED) or CANCELLED game.
   - User joining invalid, malformed, or expired (solved/cancelled) game codes.
   - Self-join via deep link (Analyst attempting to join own game as Agent).
   - Rapid double-clicks / multi-clicks on inline buttons (guesses, hints, SOS).
   - Identical message edits where text and markup are identical (verifying
     safe_edit_message and safe_edit_callback suppression of TelegramBadRequest).
3. Concurrency, role permissions, and cross-session isolation.
"""
import asyncio
from datetime import datetime
from typing import AsyncGenerator, Dict, List

import pytest
import pytest_asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import event, select
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
)
from bot.utils import NotifierService, safe_edit_callback, safe_edit_message
from cryptogram.catalog import get_catalog
from cryptogram.engine import get_unrevealed_codes
from cryptogram.models import GuessStatus
from database.models import Base, GameSession, HintType, SessionStatus
from database.session import create_session_factory
from services.lock_manager import SessionLockManager
from services.session_service import (
    SessionFinishedError,
    SessionNotFoundError,
    SessionService,
    UnauthorizedActionError,
)
from tests.mock_session import MockPlayerClient, MockTelegramSession


# ==========================================
# Fixtures
# ==========================================


@pytest_asyncio.fixture(scope="function")
async def stress_engine(tmp_path) -> AsyncGenerator[AsyncEngine, None]:
    """File-backed SQLite async engine in WAL mode to support multi-coroutine concurrent transactions."""
    import secrets
    from database.session import create_sqlite_async_engine
    db_file = tmp_path / f"stress_{secrets.token_hex(4)}.db"
    engine = create_sqlite_async_engine(f"sqlite+aiosqlite:///{db_file}", echo=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    await engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def stress_session_service(stress_engine: AsyncEngine) -> SessionService:
    """Provides a fresh SessionService bound to the test engine."""
    session_factory = create_session_factory(stress_engine)
    lock_manager = SessionLockManager()
    return SessionService(session_factory=session_factory, lock_manager=lock_manager)


@pytest_asyncio.fixture(scope="function")
async def stress_bot_env(stress_session_service: SessionService) -> Dict:
    """Configures bot environment with MockTelegramSession, Dispatcher, and SessionService."""
    mock_session = MockTelegramSession(simulate_unmodified_error=False)
    bot = Bot(token="42:MOCK_TOKEN", session=mock_session)
    dp = create_dispatcher(
        session_service=stress_session_service,
        lock_manager=stress_session_service.lock_manager,
        bot_username="SpyAnalystBot",
        storage=MemoryStorage(),
    )
    return {
        "dp": dp,
        "bot": bot,
        "session": mock_session,
        "service": stress_session_service,
    }


# ==========================================
# Test Suite: M3 Multi-Player & State Machine Stress
# ==========================================


@pytest.mark.e2e
@pytest.mark.asyncio
class TestAdversarialM3Stress:

    # ------------------------------------------------------------------
    # 1. Multi-Player Concurrent Complete Game Simulations
    # ------------------------------------------------------------------

    async def test_concurrent_complete_games_simulation(self, stress_bot_env):
        """Simulate 4 distinct complete games played simultaneously by 4 user pairs.

        Each pair:
        - Analyst creates game (2 catalog, 2 custom phrases).
        - Agent joins via /start game_<code>.
        - Concurrently guesses all letters and receives hints until SOLVED.
        - Verifies no cross-talk, correct stats, and SOLVED status.
        """
        dp = stress_bot_env["dp"]
        bot = stress_bot_env["bot"]
        session = stress_bot_env["session"]
        service = stress_bot_env["service"]

        phrases = [
            ("catalog", get_catalog()[0].id),
            ("catalog", get_catalog()[1].id),
            ("custom", "ПОЕХАЛИ В КОСМОС"),
            ("custom", "СЕКРЕТНЫЙ РАЗВЕДЦЕНТР НА СВЯЗИ"),
        ]

        async def play_one_game(pair_idx: int, ptype: str, payload: str):
            analyst_id = 2000 + pair_idx * 10 + 1
            agent_id = 2000 + pair_idx * 10 + 2

            analyst = MockPlayerClient(
                dp, bot, user_id=analyst_id, username=f"analyst_{pair_idx}", first_name=f"HQ_{pair_idx}"
            )
            agent = MockPlayerClient(
                dp, bot, user_id=agent_id, username=f"agent_{pair_idx}", first_name=f"Field_{pair_idx}"
            )

            # 1. Analyst creates game
            await analyst.send_command("/start")
            start_msg = session.get_last_message_for_chat(analyst_id)
            assert start_msg is not None

            if ptype == "catalog":
                await analyst.click_button_by_text("каталог", start_msg)
                cat_msg = session.get_last_message_for_chat(analyst_id)
                pick_cb = CatalogCallback(action="pick", item_id=payload, page=0).pack()
                await analyst.click_button(pick_cb, message=cat_msg)
            else:
                await analyst.click_button_by_text("свой текст", start_msg)
                await analyst.send_message(payload)

            dossier_msg = session.get_last_message_for_chat(analyst_id)
            assert "ДОСЬЕ СПЕЦОПЕРАЦИИ" in dossier_msg.text
            invite_code = dossier_msg.text.split("game_")[1][:8]

            # 2. Agent joins via deep link
            await agent.send_command("/start", f"game_{invite_code}")
            agent_terminal_msg = session.get_last_message_for_chat(agent_id)
            assert agent_terminal_msg is not None
            assert "ПОЛЕВОЙ ТЕРМИНАЛ" in agent_terminal_msg.text

            # Reload game from DB
            game = await service.get_session_by_code(invite_code)
            assert game is not None
            assert game.status == SessionStatus.IN_PROGRESS
            assert game.agent_id == agent_id
            assert game.analyst_id == analyst_id

            code_to_char = game.code_to_char_map
            all_codes = sorted(code_to_char.keys())

            # 3. Interleaved play: Agent guesses first half, Analyst gives a hint, Agent guesses rest
            mid = len(all_codes) // 2

            for sym in all_codes[:mid]:
                letter = code_to_char[sym]
                guess_cb = LetterGuessCallback(code=invite_code, symbol=sym, letter=letter).pack()
                await agent.click_button(guess_cb, message=agent_terminal_msg)

            # Analyst sends a reveal hint for one symbol if available
            reloaded_game = await service.get_session_by_code(invite_code)
            if reloaded_game.status != SessionStatus.SOLVED and mid < len(all_codes):
                hint_sym = all_codes[mid]
                hint_cb = HintActionCallback(code=invite_code, action="reveal", symbol=hint_sym).pack()
                await analyst.click_button(hint_cb, message=dossier_msg)
                mid += 1

            # Agent guesses all remaining unrevealed symbols
            for sym in all_codes[mid:]:
                reloaded = await service.get_session_by_code(invite_code)
                if reloaded.status == SessionStatus.SOLVED:
                    break
                if str(sym) not in reloaded.guessed_mapping:
                    letter = code_to_char[sym]
                    guess_cb = LetterGuessCallback(code=invite_code, symbol=sym, letter=letter).pack()
                    await agent.click_button(guess_cb, message=agent_terminal_msg)

            # 4. Verify game reached SOLVED
            final_game = await service.get_session_by_code(invite_code)
            assert final_game.status == SessionStatus.SOLVED
            assert len(final_game.revealed_letters) == final_game.total_unique_letters

            # Verify Analyst and Agent both received victory feedback
            analyst_dossier_final = session.messages[dossier_msg.message_id]
            agent_last_msg = session.get_last_message_for_chat(agent_id)
            assert "ПОБЕДА" in agent_last_msg.text or "ЗАВЕРШЕНА" in agent_last_msg.text
            assert "ЗАВЕРШЕНА" in analyst_dossier_final.text or "ПОБЕДА" in analyst_dossier_final.text

            return invite_code

        # Run all 4 games concurrently!
        tasks = [play_one_game(i, p[0], p[1]) for i, p in enumerate(phrases)]
        resolved_codes = await asyncio.gather(*tasks)

        assert len(resolved_codes) == 4
        assert len(set(resolved_codes)) == 4  # 4 distinct game codes

    async def test_concurrent_games_with_mistakes_and_sos(self, stress_bot_env):
        """Simulate 3 concurrent games where players submit wrong guesses and trigger SOS."""
        dp = stress_bot_env["dp"]
        bot = stress_bot_env["bot"]
        session = stress_bot_env["session"]
        service = stress_bot_env["service"]

        async def play_game_with_chaos(idx: int):
            analyst_id = 3000 + idx * 10 + 1
            agent_id = 3000 + idx * 10 + 2

            analyst = MockPlayerClient(dp, bot, user_id=analyst_id, username=f"an_ch_{idx}")
            agent = MockPlayerClient(dp, bot, user_id=agent_id, username=f"ag_ch_{idx}")

            phrase = f"ОПЕРАЦИЯ ШТОРМ НОМЕР {idx}"
            game = await service.create_session(analyst_id, phrase, is_custom=True)
            invite_code = game.invite_code

            # Record dummy analyst coordinates
            dossier_msg = types.Message(
                message_id=4000 + idx,
                date=datetime.now(),
                chat=analyst.chat,
                from_user=analyst.user,
                text="Dossier",
            )
            session.messages[dossier_msg.message_id] = dossier_msg
            await service.update_message_ids(
                game.id, analyst_chat_id=analyst_id, analyst_message_id=dossier_msg.message_id
            )

            # Agent joins
            await agent.send_command("/start", f"game_{invite_code}")
            agent_term = session.get_last_message_for_chat(agent_id)

            # Agent submits WRONG guess
            code_to_char = game.code_to_char_map
            correct_1 = code_to_char[1]
            wrong_1 = "Ю" if correct_1 != "Ю" else "Я"
            wrong_cb = LetterGuessCallback(code=invite_code, symbol=1, letter=wrong_1).pack()
            await agent.click_button(wrong_cb, message=agent_term)

            # Agent triggers SOS
            sos_cb = HintActionCallback(code=invite_code, action="sos", symbol=0).pack()
            await agent.click_button(sos_cb, message=agent_term)

            # Analyst sends frequency report
            freq_cb = HintActionCallback(code=invite_code, action="freq", symbol=0).pack()
            await analyst.click_button(freq_cb, message=dossier_msg)

            # Solve the game correctly
            for sym, char in code_to_char.items():
                guess_cb = LetterGuessCallback(code=invite_code, symbol=sym, letter=char).pack()
                await agent.click_button(guess_cb, message=agent_term)

            final_game = await service.get_session_by_code(invite_code)
            assert final_game.status == SessionStatus.SOLVED
            assert final_game.mistakes_count >= 1
            assert final_game.hints_used >= 1
            return final_game

        games = await asyncio.gather(*[play_game_with_chaos(i) for i in range(3)])
        assert len(games) == 3
        for g in games:
            assert g.status == SessionStatus.SOLVED

    # ------------------------------------------------------------------
    # 2. Edge Cases on Finished and Cancelled Games
    # ------------------------------------------------------------------

    async def test_guess_on_solved_game_rejected(self, stress_bot_env):
        """Guess submitted on an already SOLVED game is rejected with alert."""
        dp = stress_bot_env["dp"]
        bot = stress_bot_env["bot"]
        session = stress_bot_env["session"]
        service = stress_bot_env["service"]

        analyst = MockPlayerClient(dp, bot, user_id=4001, username="analyst_f")
        agent = MockPlayerClient(dp, bot, user_id=4002, username="agent_f")

        game = await service.create_session(analyst.user.id, "БАЗА ПРИЕМ")
        code = game.invite_code
        await service.join_session(code, agent.user.id)

        # Solve all symbols
        for sym, char in game.code_to_char_map.items():
            await service.submit_guess(game.id, agent.user.id, sym, char)

        solved_game = await service.get_session_by_id(game.id)
        assert solved_game.status == SessionStatus.SOLVED

        # Agent clicks guess on already solved game
        terminal_msg = types.Message(
            message_id=4500, date=datetime.now(), chat=agent.chat, from_user=agent.user, text="Terminal"
        )
        session.messages[4500] = terminal_msg

        guess_cb = LetterGuessCallback(code=code, symbol=1, letter="А").pack()
        session.callback_answers.clear()
        await agent.click_button(guess_cb, message=terminal_msg)

        assert len(session.callback_answers) > 0
        last_ans = session.callback_answers[-1]
        assert "Операция не активна" in last_ans.text
        assert last_ans.show_alert is True

        # Direct service call raises SessionFinishedError
        with pytest.raises(SessionFinishedError):
            await service.submit_guess(game.id, agent.user.id, 1, "А")

    async def test_symbol_select_on_solved_game_rejected(self, stress_bot_env):
        """SymbolSelectCallback on a SOLVED game answers with 'Операция не активна!'."""
        dp = stress_bot_env["dp"]
        bot = stress_bot_env["bot"]
        session = stress_bot_env["session"]
        service = stress_bot_env["service"]

        analyst = MockPlayerClient(dp, bot, user_id=4101, username="an_sym_f")
        agent = MockPlayerClient(dp, bot, user_id=4102, username="ag_sym_f")

        game = await service.create_session(analyst.user.id, "БАЗА ПРИЕМ")
        code = game.invite_code
        await service.join_session(code, agent.user.id)

        for sym, char in game.code_to_char_map.items():
            await service.submit_guess(game.id, agent.user.id, sym, char)

        terminal_msg = types.Message(
            message_id=4510, date=datetime.now(), chat=agent.chat, from_user=agent.user, text="Terminal"
        )
        session.messages[4510] = terminal_msg

        sym_cb = SymbolSelectCallback(code=code, symbol=1).pack()
        session.callback_answers.clear()
        await agent.click_button(sym_cb, message=terminal_msg)

        assert len(session.callback_answers) > 0
        last_ans = session.callback_answers[-1]
        assert "Операция не активна" in last_ans.text
        assert last_ans.show_alert is True

    async def test_guess_on_cancelled_game_rejected(self, stress_bot_env):
        """Guess submitted on a CANCELLED game is rejected."""
        dp = stress_bot_env["dp"]
        bot = stress_bot_env["bot"]
        session = stress_bot_env["session"]
        service = stress_bot_env["service"]

        analyst = MockPlayerClient(dp, bot, user_id=4201, username="an_canc")
        agent = MockPlayerClient(dp, bot, user_id=4202, username="ag_canc")

        game = await service.create_session(analyst.user.id, "ОТМЕНА МИССИИ")
        code = game.invite_code

        # Transition game to CANCELLED in DB
        async with service.session_factory() as db:
            db_game = await service._resolve_session(db, code)
            db_game.status = SessionStatus.CANCELLED
            await db.commit()

        terminal_msg = types.Message(
            message_id=4520, date=datetime.now(), chat=agent.chat, from_user=agent.user, text="Terminal"
        )
        session.messages[4520] = terminal_msg

        guess_cb = LetterGuessCallback(code=code, symbol=1, letter="А").pack()
        session.callback_answers.clear()
        await agent.click_button(guess_cb, message=terminal_msg)

        assert len(session.callback_answers) > 0
        last_ans = session.callback_answers[-1]
        assert "Операция не активна" in last_ans.text
        assert last_ans.show_alert is True

        # Direct service call raises SessionFinishedError
        with pytest.raises(SessionFinishedError):
            await service.submit_guess(game.id, agent.user.id, 1, "А")

    async def test_hints_on_finished_game_rejected(self, stress_bot_env):
        """Analyst hints and Agent SOS on finished game answer with inactive alert."""
        dp = stress_bot_env["dp"]
        bot = stress_bot_env["bot"]
        session = stress_bot_env["session"]
        service = stress_bot_env["service"]

        analyst = MockPlayerClient(dp, bot, user_id=4301, username="an_hint_f")
        agent = MockPlayerClient(dp, bot, user_id=4302, username="ag_hint_f")

        game = await service.create_session(analyst.user.id, "ФИНИШ ИГРЫ")
        code = game.invite_code
        await service.join_session(code, agent.user.id)

        for sym, char in game.code_to_char_map.items():
            await service.submit_guess(game.id, agent.user.id, sym, char)

        dossier_msg = types.Message(
            message_id=4530, date=datetime.now(), chat=analyst.chat, from_user=analyst.user, text="Dossier"
        )
        session.messages[4530] = dossier_msg

        # 1. Reveal hint
        session.callback_answers.clear()
        hint_cb = HintActionCallback(code=code, action="reveal", symbol=0).pack()
        await analyst.click_button(hint_cb, message=dossier_msg)
        assert len(session.callback_answers) > 0
        assert "Операция не активна" in session.callback_answers[-1].text

        # 2. Frequency report hint
        session.callback_answers.clear()
        freq_cb = HintActionCallback(code=code, action="freq", symbol=0).pack()
        await analyst.click_button(freq_cb, message=dossier_msg)
        assert len(session.callback_answers) > 0
        assert "Операция не активна" in session.callback_answers[-1].text

        # 3. Agent SOS
        session.callback_answers.clear()
        sos_cb = HintActionCallback(code=code, action="sos", symbol=0).pack()
        await agent.click_button(sos_cb, message=dossier_msg)
        assert len(session.callback_answers) > 0
        assert "Операция не активна" in session.callback_answers[-1].text

    # ------------------------------------------------------------------
    # 3. Invalid, Malformed, and Expired Game Codes
    # ------------------------------------------------------------------

    async def test_join_nonexistent_code(self, stress_bot_env):
        """User joining nonexistent game code receives error message and main menu."""
        dp = stress_bot_env["dp"]
        bot = stress_bot_env["bot"]
        session = stress_bot_env["session"]

        agent = MockPlayerClient(dp, bot, user_id=5001, username="lost_agent")
        await agent.send_command("/start", "game_deadbeef")

        msg = session.get_last_message_for_chat(agent.user.id)
        assert msg is not None
        assert "Операция не найдена" in msg.text
        assert msg.reply_markup is not None

    async def test_join_malformed_codes(self, stress_bot_env):
        """User sending malformed deep-links is handled without crashes."""
        dp = stress_bot_env["dp"]
        bot = stress_bot_env["bot"]
        session = stress_bot_env["session"]

        agent = MockPlayerClient(dp, bot, user_id=5002, username="malformed_agent")

        # 1. Empty code: /start game_
        await agent.send_command("/start", "game_")
        msg1 = session.get_last_message_for_chat(agent.user.id)
        assert msg1 is not None
        assert "Операция не найдена" in msg1.text or "Шпион и Аналитик" in msg1.text

        # 2. Special characters: /start game_!@#$%
        await agent.send_command("/start", "game_!@#$%")
        msg2 = session.get_last_message_for_chat(agent.user.id)
        assert msg2 is not None
        assert "Операция не найдена" in msg2.text

        # 3. Extremely long token: /start game_abc...
        await agent.send_command("/start", "game_" + "x" * 200)
        msg3 = session.get_last_message_for_chat(agent.user.id)
        assert msg3 is not None
        assert "Операция не найдена" in msg3.text

    async def test_join_solved_game_code(self, stress_bot_env):
        """User joining a SOLVED game code receives 'Операция уже завершена'."""
        dp = stress_bot_env["dp"]
        bot = stress_bot_env["bot"]
        session = stress_bot_env["session"]
        service = stress_bot_env["service"]

        analyst = MockPlayerClient(dp, bot, user_id=5101, username="an_solv")
        agent1 = MockPlayerClient(dp, bot, user_id=5102, username="ag1_solv")
        agent2 = MockPlayerClient(dp, bot, user_id=5103, username="ag2_late")

        game = await service.create_session(analyst.user.id, "ЗАВЕРШЕНА")
        code = game.invite_code
        await service.join_session(code, agent1.user.id)

        for sym, char in game.code_to_char_map.items():
            await service.submit_guess(game.id, agent1.user.id, sym, char)

        # Agent 2 tries to join solved game
        await agent2.send_command("/start", f"game_{code}")
        msg = session.get_last_message_for_chat(agent2.user.id)
        assert msg is not None
        assert "уже завершена" in msg.text.lower()

    async def test_join_cancelled_game_code(self, stress_bot_env):
        """User joining a CANCELLED game code receives 'Операция уже завершена'."""
        dp = stress_bot_env["dp"]
        bot = stress_bot_env["bot"]
        session = stress_bot_env["session"]
        service = stress_bot_env["service"]

        analyst = MockPlayerClient(dp, bot, user_id=5201, username="an_canc2")
        agent = MockPlayerClient(dp, bot, user_id=5202, username="ag_late2")

        game = await service.create_session(analyst.user.id, "ОТМЕНЕННАЯ МИССИЯ")
        code = game.invite_code

        async with service.session_factory() as db:
            db_game = await service._resolve_session(db, code)
            db_game.status = SessionStatus.CANCELLED
            await db.commit()

        await agent.send_command("/start", f"game_{code}")
        msg = session.get_last_message_for_chat(agent.user.id)
        assert msg is not None
        assert "уже завершена" in msg.text.lower()

    # ------------------------------------------------------------------
    # 4. Self-Join via Deep Link
    # ------------------------------------------------------------------

    async def test_self_join_deep_link_rejected_and_subsequent_valid_join(self, stress_bot_env):
        """Analyst attempting to join own game via deep-link is blocked; real Agent can join after."""
        dp = stress_bot_env["dp"]
        bot = stress_bot_env["bot"]
        session = stress_bot_env["session"]
        service = stress_bot_env["service"]

        analyst = MockPlayerClient(dp, bot, user_id=6001, username="analyst_self", first_name="Аналитик")
        agent = MockPlayerClient(dp, bot, user_id=6002, username="real_agent", first_name="Агент")

        # 1. Analyst creates game
        game = await service.create_session(analyst.user.id, "СЕКРЕТНЫЙ ШТАБ")
        code = game.invite_code

        # 2. Analyst attempts self-join via deep link
        await analyst.send_command("/start", f"game_{code}")

        msg = session.get_last_message_for_chat(analyst.user.id)
        assert msg is not None
        assert "Штабным Аналитиком" in msg.text
        assert "не может выступать в роли Агента" in msg.text

        # Verify DB: status is still WAITING_AGENT, agent_id is still None
        reloaded = await service.get_session_by_code(code)
        assert reloaded.status == SessionStatus.WAITING_AGENT
        assert reloaded.agent_id is None

        # 3. Legitimate agent joins
        await agent.send_command("/start", f"game_{code}")
        agent_msg = session.get_last_message_for_chat(agent.user.id)
        assert agent_msg is not None
        assert "ПОЛЕВОЙ ТЕРМИНАЛ" in agent_msg.text

        # Verify DB: status transitioned to IN_PROGRESS
        reloaded2 = await service.get_session_by_code(code)
        assert reloaded2.status == SessionStatus.IN_PROGRESS
        assert reloaded2.agent_id == agent.user.id

    # ------------------------------------------------------------------
    # 5. Rapid Double-Clicks & Button Spam Concurrency
    # ------------------------------------------------------------------

    async def test_rapid_double_clicks_guess_same_letter(self, stress_bot_env):
        """Rapid double/multi-clicks on the same LetterGuess button are handled cleanly without deadlocks."""
        dp = stress_bot_env["dp"]
        bot = stress_bot_env["bot"]
        session = stress_bot_env["session"]
        service = stress_bot_env["service"]

        analyst = MockPlayerClient(dp, bot, user_id=7001, username="an_sp")
        agent = MockPlayerClient(dp, bot, user_id=7002, username="ag_sp")

        game = await service.create_session(analyst.user.id, "БЫСТРЫЙ КЛИК")
        code = game.invite_code
        await service.join_session(code, agent.user.id)

        term_msg = types.Message(
            message_id=7100, date=datetime.now(), chat=agent.chat, from_user=agent.user, text="Terminal"
        )
        session.messages[7100] = term_msg
        await service.update_message_ids(game.id, agent_chat_id=agent.user.id, agent_message_id=7100)

        correct_char = game.code_to_char_map[1]
        guess_cb = LetterGuessCallback(code=code, symbol=1, letter=correct_char).pack()

        # Fire 5 concurrent clicks on the exact same button!
        clicks = [agent.click_button(guess_cb, message=term_msg) for _ in range(5)]
        await asyncio.gather(*clicks)

        # Verify symbol 1 was recorded in guessed_mapping
        reloaded = await service.get_session_by_id(game.id)
        assert "1" in reloaded.guessed_mapping
        assert reloaded.guessed_mapping["1"] == correct_char

        # All 5 callback answers should have been answered without unhandled exceptions
        assert len(session.callback_answers) >= 5

    async def test_rapid_double_clicks_wrong_guess(self, stress_bot_env):
        """Rapid multi-clicks on a WRONG guess increment mistake count without database corruption."""
        dp = stress_bot_env["dp"]
        bot = stress_bot_env["bot"]
        session = stress_bot_env["session"]
        service = stress_bot_env["service"]

        analyst = MockPlayerClient(dp, bot, user_id=7101, username="an_wr")
        agent = MockPlayerClient(dp, bot, user_id=7102, username="ag_wr")

        game = await service.create_session(analyst.user.id, "ТОЧНЫЙ УДАР")
        code = game.invite_code
        await service.join_session(code, agent.user.id)

        term_msg = types.Message(
            message_id=7200, date=datetime.now(), chat=agent.chat, from_user=agent.user, text="Terminal"
        )
        session.messages[7200] = term_msg
        await service.update_message_ids(game.id, agent_chat_id=agent.user.id, agent_message_id=7200)

        correct_char = game.code_to_char_map[1]
        wrong_char = "Ю" if correct_char != "Ю" else "Я"
        wrong_cb = LetterGuessCallback(code=code, symbol=1, letter=wrong_char).pack()

        # Fire 4 concurrent wrong clicks
        clicks = [agent.click_button(wrong_cb, message=term_msg) for _ in range(4)]
        await asyncio.gather(*clicks)

        reloaded = await service.get_session_by_id(game.id)
        # All 4 wrong clicks were processed atomically
        assert reloaded.mistakes_count == 4

    async def test_rapid_double_clicks_reveal_hint(self, stress_bot_env):
        """Rapid concurrent clicks on Reveal Hint are executed sequentially under lock without errors."""
        dp = stress_bot_env["dp"]
        bot = stress_bot_env["bot"]
        session = stress_bot_env["session"]
        service = stress_bot_env["service"]

        analyst = MockPlayerClient(dp, bot, user_id=7201, username="an_rh")
        agent = MockPlayerClient(dp, bot, user_id=7202, username="ag_rh")

        game = await service.create_session(analyst.user.id, "ШИФРОВКА БУКВ")
        code = game.invite_code
        await service.join_session(code, agent.user.id)

        dossier_msg = types.Message(
            message_id=7300, date=datetime.now(), chat=analyst.chat, from_user=analyst.user, text="Dossier"
        )
        term_msg = types.Message(
            message_id=7301, date=datetime.now(), chat=agent.chat, from_user=agent.user, text="Terminal"
        )
        session.messages[7300] = dossier_msg
        session.messages[7301] = term_msg
        await service.update_message_ids(
            game.id,
            analyst_chat_id=analyst.user.id,
            analyst_message_id=7300,
            agent_chat_id=agent.user.id,
            agent_message_id=7301,
        )

        hint_cb = HintActionCallback(code=code, action="reveal", symbol=0).pack()

        # Analyst clicks reveal hint 3 times rapidly
        clicks = [analyst.click_button(hint_cb, message=dossier_msg) for _ in range(3)]
        await asyncio.gather(*clicks)

        reloaded = await service.get_session_by_id(game.id)
        # 3 hints were used and 3 symbols revealed
        assert reloaded.hints_used == 3
        assert len(reloaded.guessed_mapping) == 3

    async def test_rapid_double_clicks_symbol_select(self, stress_bot_env):
        """Rapid multi-clicks on SymbolSelectCallback switch cleanly to Alphabet picker."""
        dp = stress_bot_env["dp"]
        bot = stress_bot_env["bot"]
        session = stress_bot_env["session"]
        service = stress_bot_env["service"]

        analyst = MockPlayerClient(dp, bot, user_id=7301, username="an_sym_sp")
        agent = MockPlayerClient(dp, bot, user_id=7302, username="ag_sym_sp")

        game = await service.create_session(analyst.user.id, "ПРОВЕРКА ВВОДА")
        code = game.invite_code
        await service.join_session(code, agent.user.id)

        term_msg = types.Message(
            message_id=7400, date=datetime.now(), chat=agent.chat, from_user=agent.user, text="Terminal"
        )
        session.messages[7400] = term_msg

        sym_cb = SymbolSelectCallback(code=code, symbol=1).pack()

        clicks = [agent.click_button(sym_cb, message=term_msg) for _ in range(4)]
        await asyncio.gather(*clicks)

        edited = session.messages[7400]
        assert "Подстановка для символа [1]" in edited.text

    # ------------------------------------------------------------------
    # 6. Identical Message Edit Suppression (TelegramBadRequest Shield)
    # ------------------------------------------------------------------

    async def test_safe_edit_message_unmodified_suppression(self, stress_bot_env):
        """safe_edit_message returns False without raising TelegramBadRequest on unmodified text."""
        bot = stress_bot_env["bot"]
        session = stress_bot_env["session"]
        session.simulate_unmodified_error = True

        msg_id = 8001
        session.messages[msg_id] = types.Message(
            message_id=msg_id,
            date=datetime.now(),
            chat=types.Chat(id=9001, type="private"),
            from_user=types.User(id=bot.id, is_bot=True, first_name="Bot"),
            text="Unchanged text",
            reply_markup=None,
        )

        res = await safe_edit_message(
            bot=bot,
            chat_id=9001,
            message_id=msg_id,
            text="Unchanged text",
            reply_markup=None,
        )
        assert res is False

    async def test_safe_edit_callback_unmodified_suppression(self, stress_bot_env):
        """safe_edit_callback returns False without raising TelegramBadRequest on unmodified text."""
        bot = stress_bot_env["bot"]
        session = stress_bot_env["session"]
        session.simulate_unmodified_error = True

        msg_id = 8002
        orig_msg = types.Message(
            message_id=msg_id,
            date=datetime.now(),
            chat=types.Chat(id=9002, type="private"),
            from_user=types.User(id=bot.id, is_bot=True, first_name="Bot"),
            text="Unchanged text",
            reply_markup=None,
        )
        session.messages[msg_id] = orig_msg

        call = types.CallbackQuery(
            id="call_test",
            from_user=types.User(id=9002, is_bot=False, first_name="Player"),
            chat_instance="chat_9002",
            message=orig_msg,
        )
        # Bind bot to message for edit_text
        orig_msg._bot = bot

        res = await safe_edit_callback(call, text="Unchanged text", reply_markup=None)
        assert res is False

    async def test_analyst_refresh_dossier_unmodified_shield(self, stress_bot_env):
        """Analyst clicking refresh when content is unchanged does not crash the bot with simulate_unmodified_error."""
        dp = stress_bot_env["dp"]
        bot = stress_bot_env["bot"]
        session = stress_bot_env["session"]
        service = stress_bot_env["service"]

        analyst = MockPlayerClient(dp, bot, user_id=8101, username="an_ref")
        game = await service.create_session(analyst.user.id, "ОБНОВЛЕНИЕ ТАБЛО")
        code = game.invite_code

        notifier = NotifierService(bot=bot, session_service=service)
        invite_link = f"https://t.me/SpyAnalystBot?start=game_{code}"
        dossier_text = notifier.render_analyst_dossier(game, invite_link=invite_link)
        kb = build_analyst_dossier_keyboard(game, invite_link=invite_link, bot_username="SpyAnalystBot")

        dossier_msg = types.Message(
            message_id=8100,
            date=datetime.now(),
            chat=analyst.chat,
            from_user=types.User(id=bot.id, is_bot=True, first_name="Bot"),
            text=dossier_text,
            reply_markup=kb,
        )
        session.messages[8100] = dossier_msg

        # Enable unmodified error simulation
        session.simulate_unmodified_error = True

        refresh_cb = HintActionCallback(code=code, action="refresh", symbol=0).pack()
        session.callback_answers.clear()

        # Click refresh twice in a row!
        await analyst.click_button(refresh_cb, message=dossier_msg)
        await analyst.click_button(refresh_cb, message=dossier_msg)

        # Both clicks must succeed and answer callback
        assert len(session.callback_answers) == 2
        assert all("Табло обновлено" in ans.text for ans in session.callback_answers)

    async def test_agent_refresh_terminal_unmodified_shield(self, stress_bot_env):
        """Agent clicking refresh when terminal is unchanged does not crash the bot with simulate_unmodified_error."""
        dp = stress_bot_env["dp"]
        bot = stress_bot_env["bot"]
        session = stress_bot_env["session"]
        service = stress_bot_env["service"]

        analyst = MockPlayerClient(dp, bot, user_id=8201, username="an_ref_ag")
        agent = MockPlayerClient(dp, bot, user_id=8202, username="ag_ref_ag")

        game = await service.create_session(analyst.user.id, "ТЕРМИНАЛ ОБНОВЛЕНИЕ")
        code = game.invite_code
        await service.join_session(code, agent.user.id)

        notifier = NotifierService(bot=bot, session_service=service)
        term_text = notifier.render_agent_terminal(game)
        term_kb = build_agent_symbol_keyboard(game)

        term_msg = types.Message(
            message_id=8200,
            date=datetime.now(),
            chat=agent.chat,
            from_user=types.User(id=bot.id, is_bot=True, first_name="Bot"),
            text=term_text,
            reply_markup=term_kb,
        )
        session.messages[8200] = term_msg
        await service.update_message_ids(game.id, agent_chat_id=agent.user.id, agent_message_id=8200)

        # Enable unmodified error simulation
        session.simulate_unmodified_error = True

        refresh_cb = HintActionCallback(code=code, action="refresh", symbol=0).pack()
        session.callback_answers.clear()

        await agent.click_button(refresh_cb, message=term_msg)
        await agent.click_button(refresh_cb, message=term_msg)

        assert len(session.callback_answers) == 2

    # ------------------------------------------------------------------
    # 7. Cross-Role Concurrency & Mutual Protection
    # ------------------------------------------------------------------

    async def test_simultaneous_guess_and_reveal_hint_on_distinct_symbols(self, stress_bot_env):
        """Simultaneous Agent guess on symbol A and Analyst hint on symbol B execute concurrently and safely."""
        dp = stress_bot_env["dp"]
        bot = stress_bot_env["bot"]
        session = stress_bot_env["session"]
        service = stress_bot_env["service"]

        analyst = MockPlayerClient(dp, bot, user_id=9001, username="an_cr")
        agent = MockPlayerClient(dp, bot, user_id=9002, username="ag_cr")

        game = await service.create_session(analyst.user.id, "СИНХРОНИЗАЦИЯ СЕТИ")
        code = game.invite_code
        await service.join_session(code, agent.user.id)

        dossier_msg = types.Message(
            message_id=9100, date=datetime.now(), chat=analyst.chat, from_user=analyst.user, text="Dossier"
        )
        term_msg = types.Message(
            message_id=9101, date=datetime.now(), chat=agent.chat, from_user=agent.user, text="Terminal"
        )
        session.messages[9100] = dossier_msg
        session.messages[9101] = term_msg
        await service.update_message_ids(
            game.id,
            analyst_chat_id=analyst.user.id,
            analyst_message_id=9100,
            agent_chat_id=agent.user.id,
            agent_message_id=9101,
        )

        sym1, char1 = 1, game.code_to_char_map[1]
        sym2 = 2

        agent_guess_cb = LetterGuessCallback(code=code, symbol=sym1, letter=char1).pack()
        analyst_hint_cb = HintActionCallback(code=code, action="reveal", symbol=sym2).pack()

        # Fire simultaneously!
        await asyncio.gather(
            agent.click_button(agent_guess_cb, message=term_msg),
            analyst.click_button(analyst_hint_cb, message=dossier_msg),
        )

        reloaded = await service.get_session_by_id(game.id)
        assert str(sym1) in reloaded.guessed_mapping
        assert str(sym2) in reloaded.guessed_mapping
        assert reloaded.hints_used == 1

    async def test_victory_triggered_by_analyst_hint(self, stress_bot_env):
        """When the last unrevealed symbol is revealed by an Analyst hint, victory is triggered."""
        dp = stress_bot_env["dp"]
        bot = stress_bot_env["bot"]
        session = stress_bot_env["session"]
        service = stress_bot_env["service"]

        analyst = MockPlayerClient(dp, bot, user_id=9201, username="an_vic_h")
        agent = MockPlayerClient(dp, bot, user_id=9202, username="ag_vic_h")

        game = await service.create_session(analyst.user.id, "ПОБЕДА")
        code = game.invite_code
        await service.join_session(code, agent.user.id)

        dossier_msg = types.Message(
            message_id=9300, date=datetime.now(), chat=analyst.chat, from_user=analyst.user, text="Dossier"
        )
        term_msg = types.Message(
            message_id=9301, date=datetime.now(), chat=agent.chat, from_user=agent.user, text="Terminal"
        )
        session.messages[9300] = dossier_msg
        session.messages[9301] = term_msg
        await service.update_message_ids(
            game.id,
            analyst_chat_id=analyst.user.id,
            analyst_message_id=9300,
            agent_chat_id=agent.user.id,
            agent_message_id=9301,
        )

        codes = sorted(game.code_to_char_map.keys())

        # Agent solves all except the last symbol
        for c in codes[:-1]:
            char = game.code_to_char_map[c]
            guess_cb = LetterGuessCallback(code=code, symbol=c, letter=char).pack()
            await agent.click_button(guess_cb, message=term_msg)

        # Analyst reveals the last symbol
        last_sym = codes[-1]
        hint_cb = HintActionCallback(code=code, action="reveal", symbol=last_sym).pack()
        await analyst.click_button(hint_cb, message=dossier_msg)

        final_game = await service.get_session_by_id(game.id)
        assert final_game.status == SessionStatus.SOLVED

        # Both terminal and dossier must celebrate victory
        term_final = session.messages[9301]
        dossier_final = session.messages[9300]
        assert "ПОБЕДА" in term_final.text or "ЗАВЕРШЕНА" in term_final.text
        assert "ЗАВЕРШЕНА" in dossier_final.text or "ПОБЕДА" in dossier_final.text

    # ------------------------------------------------------------------
    # 8. Role Enforcement & Boundary Operations
    # ------------------------------------------------------------------

    async def test_case_insensitive_deep_link_join(self, stress_bot_env):
        """Joining via uppercase / mixed case deep-link token succeeds."""
        dp = stress_bot_env["dp"]
        bot = stress_bot_env["bot"]
        session = stress_bot_env["session"]
        service = stress_bot_env["service"]

        analyst = MockPlayerClient(dp, bot, user_id=9401, username="an_case")
        agent = MockPlayerClient(dp, bot, user_id=9402, username="ag_case")

        game = await service.create_session(analyst.user.id, "РЕГИСТР СИМВОЛОВ")
        code = game.invite_code

        # Join with uppercase code
        await agent.send_command("/start", f"game_{code.upper()}")

        agent_msg = session.get_last_message_for_chat(agent.user.id)
        assert agent_msg is not None
        assert "ПОЛЕВОЙ ТЕРМИНАЛ" in agent_msg.text

        reloaded = await service.get_session_by_code(code)
        assert reloaded.status == SessionStatus.IN_PROGRESS
        assert reloaded.agent_id == agent.user.id

    async def test_cancel_session_in_waiting_vs_in_progress(self, stress_bot_env):
        """Cancel callback succeeds for WAITING_AGENT and is rejected for IN_PROGRESS."""
        dp = stress_bot_env["dp"]
        bot = stress_bot_env["bot"]
        session = stress_bot_env["session"]
        service = stress_bot_env["service"]

        analyst = MockPlayerClient(dp, bot, user_id=9501, username="an_canc_fl")
        agent = MockPlayerClient(dp, bot, user_id=9502, username="ag_canc_fl")

        # 1. WAITING_AGENT session
        game1 = await service.create_session(analyst.user.id, "ОТМЕНА В ОЖИДАНИИ")
        code1 = game1.invite_code

        dossier1 = types.Message(
            message_id=9510, date=datetime.now(), chat=analyst.chat, from_user=analyst.user, text="Dossier 1"
        )
        session.messages[9510] = dossier1

        cancel_cb1 = HintActionCallback(code=code1, action="cancel", symbol=0).pack()
        session.callback_answers.clear()
        await analyst.click_button(cancel_cb1, message=dossier1)

        assert len(session.callback_answers) > 0
        assert "Операция отменена" in session.callback_answers[-1].text

        # 2. IN_PROGRESS session: cancel should be rejected
        game2 = await service.create_session(analyst.user.id, "ОТМЕНА В ПРОЦЕССЕ")
        code2 = game2.invite_code
        await service.join_session(code2, agent.user.id)

        dossier2 = types.Message(
            message_id=9520, date=datetime.now(), chat=analyst.chat, from_user=analyst.user, text="Dossier 2"
        )
        session.messages[9520] = dossier2

        cancel_cb2 = HintActionCallback(code=code2, action="cancel", symbol=0).pack()
        session.callback_answers.clear()
        await analyst.click_button(cancel_cb2, message=dossier2)

        assert len(session.callback_answers) > 0
        assert "Невозможно отменить" in session.callback_answers[-1].text

    async def test_missing_coordinates_graceful_degradation(self, stress_bot_env):
        """NotifierService handles missing analyst_message_id or agent_message_id gracefully."""
        dp = stress_bot_env["dp"]
        bot = stress_bot_env["bot"]
        service = stress_bot_env["service"]

        game = await service.create_session(9601, "ДЕГРАДАЦИЯ КООРДИНАТ")
        await service.join_session(game.invite_code, 9602)

        # Explicitly clear coordinates
        reloaded = await service.get_session_by_code(game.invite_code)
        reloaded.analyst_chat_id = None
        reloaded.analyst_message_id = None
        reloaded.agent_chat_id = None
        reloaded.agent_message_id = None

        notifier = NotifierService(bot=bot, session_service=service)

        # These must not throw any exception
        await notifier.sync_game_state(reloaded)
        await notifier.sync_agent_terminal(reloaded)
        await notifier.notify_victory(reloaded)
        await notifier.send_frequency_report(reloaded, "Частоты")

