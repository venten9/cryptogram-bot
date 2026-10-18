"""Comprehensive tests for Solo Mode (Одиночная игра).

Verifies solo room creation, automated HQ hints, guessing, EXP awards,
and Telegram bot UI handlers.
"""
import pytest
import pytest_asyncio
from typing import Dict
from aiogram import Bot, types
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from bot.app import create_dispatcher
from bot.callbacks import MenuCallback, HintActionCallback
from bot.keyboards import (
    build_main_menu_keyboard,
    build_solo_difficulty_keyboard,
    build_agent_symbol_keyboard,
)
from database.models import Base, SessionStatus, User, HintType
from database.session import create_session_factory
from services.lock_manager import SessionLockManager
from services.session_service import SessionService
from tests.mock_session import MockPlayerClient, MockTelegramSession


@pytest_asyncio.fixture
async def solo_engine():
    """Isolated in-memory SQLite engine for solo tests."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def solo_service(solo_engine):
    """SessionService bound to solo_engine."""
    session_factory = create_session_factory(solo_engine)
    lock_manager = SessionLockManager()
    return SessionService(session_factory=session_factory, lock_manager=lock_manager)


@pytest.mark.asyncio
class TestSoloServiceEngine:
    """Test suite for SessionService solo mode operations."""

    async def test_create_solo_session_defaults(self, solo_service: SessionService):
        """Verify solo session starts immediately IN_PROGRESS with HQ as analyst."""
        game = await solo_service.create_solo_session(
            player_tg_id=777111,
            username="solo_operative",
            first_name="Агент Соло",
            difficulty="MEDIUM",
        )

        assert game.is_solo is True
        assert game.status == SessionStatus.IN_PROGRESS
        assert game.agent_id == 777111
        assert game.analyst_id == 0
        assert game.total_unique_letters > 0
        assert game.revealed_count == 0
        assert game.hints_used == 0
        assert game.mistakes_count == 0

        # Verify player stats incremented
        player = await solo_service.get_or_create_user(telegram_id=777111)
        assert player is not None
        assert player.games_played == 1
        assert player.games_as_agent == 1

    async def test_create_solo_session_difficulties(self, solo_service: SessionService):
        """Verify solo session works for EASY, MEDIUM, HARD, and RANDOM."""
        for diff in ["EASY", "MEDIUM", "HARD", "RANDOM"]:
            game = await solo_service.create_solo_session(
                player_tg_id=888222,
                difficulty=diff,
            )
            assert game.is_solo is True
            assert game.status == SessionStatus.IN_PROGRESS
            assert game.original_text is not None

    async def test_solo_hint_from_automated_hq(self, solo_service: SessionService):
        """Verify solo agent can receive hint without human analyst."""
        game = await solo_service.create_solo_session(
            player_tg_id=555333,
            difficulty="EASY",
        )

        initial_unrevealed = len(game.cipher_mapping)
        hint_res = await solo_service.give_hint(
            session_id_or_code=game.id,
            user_tg_id=555333,
            hint_type=HintType.REVEAL_LETTER,
        )

        assert hint_res.revealed_code is not None
        assert hint_res.revealed_letter is not None

        reloaded = await solo_service.get_session_by_id(game.id)
        assert reloaded.hints_used == 1
        assert str(hint_res.revealed_code) in reloaded.guessed_mapping

    async def test_solo_guess_and_victory(self, solo_service: SessionService):
        """Verify solo agent solves cryptogram, receives EXP and advances military rank."""
        # Create deterministic quote
        text = "ШТАБ ЦЕНТР"
        game = await solo_service.create_solo_session(
            player_tg_id=999444,
            custom_text=text,
            seed=42,
        )

        char_to_code = game.cipher_mapping
        for char, code in char_to_code.items():
            res = await solo_service.submit_guess(
                session_id_or_code=game.id,
                user_tg_id=999444,
                symbol_code=code,
                letter=char,
            )

        reloaded = await solo_service.get_session_by_id(game.id)
        assert reloaded.status == SessionStatus.SOLVED

        player = await solo_service.get_or_create_user(telegram_id=999444)
        assert player.games_won == 1
        assert player.exp > 0
        assert player.rank_title != ""

    async def test_solo_surrender(self, solo_service: SessionService):
        """Verify solo agent can surrender an in-progress game."""
        game = await solo_service.create_solo_session(
            player_tg_id=111555,
            difficulty="EASY",
        )
        assert game.status == SessionStatus.IN_PROGRESS

        cancelled = await solo_service.cancel_session(game.id, 111555)
        assert cancelled.status == SessionStatus.CANCELLED


class TestSoloUiHandlers:
    """Test suite verifying Telegram UI navigation for solo mode."""

    def test_main_menu_keyboard_has_solo_button(self):
        """Verify main menu has prominent Solo button."""
        kb = build_main_menu_keyboard()
        buttons = [btn for row in kb.inline_keyboard for btn in row]
        solo_btns = [b for b in buttons if "одиночная" in b.text.lower() or "соло" in b.text.lower()]
        assert len(solo_btns) == 1
        assert solo_btns[0].callback_data == MenuCallback(action="solo").pack()

    def test_solo_difficulty_keyboard_structure(self):
        """Verify solo difficulty keyboard contains EASY, MEDIUM, HARD, RANDOM, and Main Menu."""
        kb = build_solo_difficulty_keyboard()
        buttons = [btn for row in kb.inline_keyboard for btn in row]
        callbacks = [b.callback_data for b in buttons]

        assert MenuCallback(action="solo_diff", code="EASY").pack() in callbacks
        assert MenuCallback(action="solo_diff", code="MEDIUM").pack() in callbacks
        assert MenuCallback(action="solo_diff", code="HARD").pack() in callbacks
        assert MenuCallback(action="solo_diff", code="RANDOM").pack() in callbacks
        assert MenuCallback(action="main").pack() in callbacks

    def test_solo_agent_keyboard_has_solo_controls(self, solo_service: SessionService):
        """Verify build_agent_symbol_keyboard renders solo controls for is_solo=True."""
        class MockGame:
            invite_code = "solo1234"
            cipher_mapping = {"А": 1, "Б": 2}
            revealed_letters = set()
            is_solo = True

        kb = build_agent_symbol_keyboard(MockGame())
        buttons = [btn for row in kb.inline_keyboard for btn in row]

        actions = [b.callback_data for b in buttons if b.callback_data]
        assert any("solo_hint" in a for a in actions), "Expected solo_hint button"
        assert any("solo_freq" in a for a in actions), "Expected solo_freq button"
        assert any("solo_surrender" in a for a in actions), "Expected solo_surrender button"
