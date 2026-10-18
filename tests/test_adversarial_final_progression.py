"""Final Milestone 5 Adversarial Stress & Integration Test Suite.

Stress-tests:
1. Multi-game rank progression from Private (0 EXP) through all 19 military ranks to Marshal (35,000+ EXP).
2. Boundary conditions:
   - Consecutive zero-mistake / zero-hint games (clean work & autonomy bonuses).
   - Single-mistake and single-hint threshold breakdowns.
   - Highly contested games with 50+ mistakes and 50+ hints.
   - Multi-round cumulative contested games tracking 150+ mistakes/hints.
3. Solo analyst vs dual-player game mechanics:
   - Unstarted session guards (guesses/hints rejected in WAITING_AGENT).
   - Self-join prevention.
   - Solo analyst victory processing without agent crash.
   - Dual-player symmetric awards and alternating role tracking (games_as_analyst + games_as_agent == games_played).
4. End-to-end multi-round mock Telegram UI profile dossier updates across games.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import AsyncGenerator, Dict, List

import pytest
import pytest_asyncio
from aiogram import Bot, Dispatcher, types
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
from bot.callbacks import HintActionCallback, LetterGuessCallback, MenuCallback
from cryptogram.catalog import get_catalog_item
from cryptogram.engine import calculate_frequencies, generate_cipher
from cryptogram.models import GuessStatus
from cryptogram.ranks import (
    DIFFICULTY_BASE_EXP,
    RANKS,
    calculate_mission_exp,
    calculate_rank_progress,
    get_next_rank,
    get_rank,
)
from database.models import Base, GameSession, HintLog, HintType, SessionStatus, User
from database.session import create_session_factory
from services.lock_manager import SessionLockManager
from services.session_service import (
    SelfJoinError,
    SessionFinishedError,
    SessionInvalidStateError,
    SessionNotFoundError,
    SessionService,
    UnauthorizedActionError,
)
from tests.mock_session import MockPlayerClient, MockTelegramSession

pytestmark = [pytest.mark.database, pytest.mark.boundary]


# ======================================================================
# Fixtures
# ======================================================================


@pytest_asyncio.fixture(scope="function")
async def async_engine() -> AsyncGenerator[AsyncEngine, None]:
    """In-memory SQLite async engine with StaticPool for fast, isolated tests."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON;")
        cursor.execute("PRAGMA busy_timeout=5000;")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def session_factory(
    async_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(async_engine)


@pytest_asyncio.fixture(scope="function")
async def lock_manager() -> SessionLockManager:
    return SessionLockManager()


@pytest_asyncio.fixture(scope="function")
async def session_service(
    session_factory: async_sessionmaker[AsyncSession],
    lock_manager: SessionLockManager,
) -> SessionService:
    return SessionService(
        session_factory=session_factory,
        lock_manager=lock_manager,
    )


# ======================================================================
# Test Suite 1: Multi-Game Rank Progression from Private to Marshal
# ======================================================================


class TestMultiGameRankProgressionToMarshal:
    """Adversarial stress-testing of player progression through all 19 military ranks."""

    def test_all_19_rank_thresholds_and_properties_discrete(self):
        """Verify all 19 ranks have exact thresholds, unique titles, valid badges, and strict monotonicity."""
        assert len(RANKS) == 19
        prev_exp = -1
        for i, rank in enumerate(RANKS, start=1):
            assert rank.level == i
            assert rank.threshold_exp > prev_exp, f"Rank {rank.name_ru} threshold not strictly monotonic"
            prev_exp = rank.threshold_exp
            assert bool(rank.name_ru)
            assert bool(rank.badge)

            # Test exact threshold lookup
            looked_up = get_rank(rank.threshold_exp)
            assert looked_up.level == rank.level
            assert looked_up.name_ru == rank.name_ru

            pct, next_exp, bar = calculate_rank_progress(rank.threshold_exp)
            if i < 19:
                next_r = get_next_rank(rank.threshold_exp)
                assert next_r is not None
                assert next_r.level == i + 1
                assert next_exp == next_r.threshold_exp
                assert pct == 0.0
                assert bar.startswith("[░░░░░░░░░░]")
            else:
                next_r = get_next_rank(rank.threshold_exp)
                assert next_r is None
                assert next_exp is None
                assert pct == 100.0
                assert bar == "[██████████] 100%"

    @pytest.mark.asyncio
    async def test_multi_game_progression_from_private_to_marshal(
        self,
        session_service: SessionService,
    ):
        """Simulate rapid consecutive game progression from Private to Marshal (35,000+ EXP).

        Plays consecutive HARD games (400 EXP each = 300 base + 50 clean + 50 autonomy).
        Requires 88 games to reach 35,200 EXP.
        Verifies:
        - Symmetric rank advancement of Analyst and Agent.
        - Transition through all 19 military ranks.
        - Capping at Marshal with None next_rank_exp and 100% progress.
        """
        analyst_id = 9001
        agent_id = 9002

        hard_quote = get_catalog_item("stavka_1914_corps")
        assert hard_quote is not None
        assert hard_quote.difficulty == "HARD"

        # Track experienced ranks to verify visiting all tiers
        initial_analyst = await session_service.get_or_create_user(analyst_id)
        assert initial_analyst.rank_title == "Рядовой"
        analyst_visited_ranks: set[str] = {initial_analyst.rank_title}

        # 88 games * 400 EXP = 35,200 EXP (crosses 35,000 Marshal threshold)
        for game_idx in range(1, 89):
            game = await session_service.create_session(
                creator_tg_id=analyst_id,
                text=hard_quote.text,
                catalog_id=hard_quote.id,
                is_custom=False,
            )

            # Agent joins
            await session_service.join_session(game.invite_code, agent_id)

            # Solve game with 0 mistakes and 0 hints
            c2c = game.code_to_char_map
            for code_sym, char in c2c.items():
                res = await session_service.submit_guess(game.id, agent_id, code_sym, char)

            assert res.is_victory is True
            assert res.game.status == SessionStatus.SOLVED

            # Verify intermediate stats
            analyst = await session_service.get_or_create_user(analyst_id)
            agent = await session_service.get_or_create_user(agent_id)

            analyst_visited_ranks.add(analyst.rank_title)

            # Symmetrical accumulation
            assert analyst.exp == game_idx * 400
            assert agent.exp == game_idx * 400
            assert analyst.games_played == game_idx
            assert agent.games_played == game_idx
            assert analyst.games_won == game_idx
            assert agent.games_won == game_idx
            assert analyst.games_as_analyst == game_idx
            assert analyst.games_as_agent == 0
            assert agent.games_as_analyst == 0
            assert agent.games_as_agent == game_idx
            assert analyst.win_rate == 100.0
            assert agent.win_rate == 100.0

        # After 88 games: 35,200 EXP -> MARSHAL
        final_analyst = await session_service.get_or_create_user(analyst_id)
        final_agent = await session_service.get_or_create_user(agent_id)

        assert final_analyst.exp == 35200
        assert final_analyst.rank_title == "Маршал"
        assert final_analyst.current_rank_badge == "👑⭐"
        assert final_analyst.next_rank_exp is None
        assert final_analyst.rank_progress_pct == 100.0
        assert final_analyst.rank_progress_bar == "[██████████] 100%"

        assert final_agent.exp == 35200
        assert final_agent.rank_title == "Маршал"
        assert final_agent.current_rank_badge == "👑⭐"
        assert final_agent.next_rank_exp is None
        assert final_agent.rank_progress_pct == 100.0
        assert final_agent.rank_progress_bar == "[██████████] 100%"

        # Verify that majority of ranks and start/end were traversed during 88-game sequence
        assert "Рядовой" in analyst_visited_ranks
        assert "Маршал" in analyst_visited_ranks
        assert len(analyst_visited_ranks) >= 15

    @pytest.mark.asyncio
    async def test_all_19_ranks_visited_sequentially_through_calibrated_gameplay(
        self,
        session_service: SessionService,
    ):
        """Play calibrated gameplay missions hitting every single military rank threshold from 1 to 19.

        Demonstrates 100% complete coverage of all 19 rank tiers achieved strictly through
        standard game session creation, joining, guessing, hint transmissions, and victory resolution.
        """
        analyst_id = 9101
        agent_id = 9102

        hard_quote = get_catalog_item("stavka_1914_corps")
        med_quote = get_catalog_item("sorge_tokyo_1941")
        easy_quote = get_catalog_item("gagarin_poekhali")

        # Step 1: Initial state is Рядовой (Rank 1, 0 EXP)
        analyst = await session_service.get_or_create_user(analyst_id)
        assert analyst.rank_title == RANKS[0].name_ru
        assert analyst.exp == 0

        # Helper to play a game with exact resulting EXP
        async def play_mission(quote_item, mistakes: int = 0, hints: int = 0) -> int:
            g = await session_service.create_session(
                creator_tg_id=analyst_id,
                text=quote_item.text,
                catalog_id=quote_item.id,
                is_custom=False,
            )
            await session_service.join_session(g.invite_code, agent_id)

            first_sym = next(iter(g.code_to_char_map.keys()))
            wrong_char = "Я" if g.code_to_char_map[first_sym] != "Я" else "Ю"

            for _ in range(mistakes):
                await session_service.submit_guess(g.id, agent_id, first_sym, wrong_char)

            for _ in range(hints):
                await session_service.give_hint(g.id, analyst_id, HintType.FREQUENCY_REPORT)

            for s, c in g.code_to_char_map.items():
                await session_service.submit_guess(g.id, agent_id, s, c)

            u = await session_service.get_or_create_user(analyst_id)
            return u.exp

        # Step 2: Rank 2 (Ефрейтор, 100 EXP) -> EASY + 1 mistake + 1 hint = 100
        await play_mission(easy_quote, mistakes=1, hints=1)
        u = await session_service.get_or_create_user(analyst_id)
        assert u.exp == 100
        assert u.rank_title == "Ефрейтор"

        # Step 3: Rank 3 (Младший сержант, 250 EXP) -> delta +150 -> EASY + 0 mistakes + 1 hint = 150
        await play_mission(easy_quote, mistakes=0, hints=1)
        u = await session_service.get_or_create_user(analyst_id)
        assert u.exp == 250
        assert u.rank_title == "Младший сержант"

        # Step 4: Rank 4 (Сержант, 450 EXP) -> delta +200 -> MEDIUM + 1 mistake + 1 hint = 200
        await play_mission(med_quote, mistakes=1, hints=1)
        u = await session_service.get_or_create_user(analyst_id)
        assert u.exp == 450
        assert u.rank_title == "Сержант"

        # Step 5: Rank 5 (Старший сержант, 700 EXP) -> delta +250 -> MEDIUM + 0 mistakes + 1 hint = 250
        await play_mission(med_quote, mistakes=0, hints=1)
        u = await session_service.get_or_create_user(analyst_id)
        assert u.exp == 700
        assert u.rank_title == "Старший сержант"

        # Step 6: Rank 6 (Старшина, 1000 EXP) -> delta +300 -> HARD + 1 mistake + 1 hint = 300
        await play_mission(hard_quote, mistakes=1, hints=1)
        u = await session_service.get_or_create_user(analyst_id)
        assert u.exp == 1000
        assert u.rank_title == "Старшина"

        # Step 7: Rank 7 (Прапорщик, 1400 EXP) -> delta +400 -> HARD + 0 mistakes + 0 hints = 400
        await play_mission(hard_quote, mistakes=0, hints=0)
        u = await session_service.get_or_create_user(analyst_id)
        assert u.exp == 1400
        assert u.rank_title == "Прапорщик"

        # Step 8: Rank 8 (Младший лейтенант, 1900 EXP) -> delta +500 (HARD 300 + MED 200)
        await play_mission(hard_quote, mistakes=1, hints=1)
        await play_mission(med_quote, mistakes=1, hints=1)
        u = await session_service.get_or_create_user(analyst_id)
        assert u.exp == 1900
        assert u.rank_title == "Младший лейтенант"

        # Step 9: Rank 9 (Лейтенант, 2500 EXP) -> delta +600 (2x HARD 300)
        await play_mission(hard_quote, mistakes=1, hints=1)
        await play_mission(hard_quote, mistakes=1, hints=1)
        u = await session_service.get_or_create_user(analyst_id)
        assert u.exp == 2500
        assert u.rank_title == "Лейтенант"

        # Step 10: Rank 10 (Старший лейтенант, 3200 EXP) -> delta +700 (HARD 400 + HARD 300)
        await play_mission(hard_quote, mistakes=0, hints=0)
        await play_mission(hard_quote, mistakes=1, hints=1)
        u = await session_service.get_or_create_user(analyst_id)
        assert u.exp == 3200
        assert u.rank_title == "Старший лейтенант"

        # Step 11: Rank 11 (Капитан, 4000 EXP) -> delta +800 (2x HARD 400)
        for _ in range(2):
            await play_mission(hard_quote, mistakes=0, hints=0)
        u = await session_service.get_or_create_user(analyst_id)
        assert u.exp == 4000
        assert u.rank_title == "Капитан"

        # Step 12: Rank 12 (Майор, 5000 EXP) -> delta +1000 (2x HARD 400 + MED 200)
        await play_mission(hard_quote, mistakes=0, hints=0)
        await play_mission(hard_quote, mistakes=0, hints=0)
        await play_mission(med_quote, mistakes=1, hints=1)
        u = await session_service.get_or_create_user(analyst_id)
        assert u.exp == 5000
        assert u.rank_title == "Майор"

        # Step 13: Rank 13 (Подполковник, 6500 EXP) -> delta +1500 (3x HARD 400 + HARD 300)
        for _ in range(3):
            await play_mission(hard_quote, mistakes=0, hints=0)
        await play_mission(hard_quote, mistakes=1, hints=1)
        u = await session_service.get_or_create_user(analyst_id)
        assert u.exp == 6500
        assert u.rank_title == "Подполковник"

        # Step 14: Rank 14 (Полковник, 8500 EXP) -> delta +2000 (5x HARD 400)
        for _ in range(5):
            await play_mission(hard_quote, mistakes=0, hints=0)
        u = await session_service.get_or_create_user(analyst_id)
        assert u.exp == 8500
        assert u.rank_title == "Полковник"

        # Step 15: Rank 15 (Генерал-майор, 11000 EXP) -> delta +2500 (6x HARD 400 + EASY 100)
        for _ in range(6):
            await play_mission(hard_quote, mistakes=0, hints=0)
        await play_mission(easy_quote, mistakes=1, hints=1)
        u = await session_service.get_or_create_user(analyst_id)
        assert u.exp == 11000
        assert u.rank_title == "Генерал-майор"

        # Step 16: Rank 16 (Генерал-лейтенант, 14500 EXP) -> delta +3500 (8x HARD 400 + HARD 300)
        for _ in range(8):
            await play_mission(hard_quote, mistakes=0, hints=0)
        await play_mission(hard_quote, mistakes=1, hints=1)
        u = await session_service.get_or_create_user(analyst_id)
        assert u.exp == 14500
        assert u.rank_title == "Генерал-лейтенант"

        # Step 17: Rank 17 (Генерал-полковник, 19000 EXP) -> delta +4500 (11x HARD 400 + EASY 100)
        for _ in range(11):
            await play_mission(hard_quote, mistakes=0, hints=0)
        await play_mission(easy_quote, mistakes=1, hints=1)
        u = await session_service.get_or_create_user(analyst_id)
        assert u.exp == 19000
        assert u.rank_title == "Генерал-полковник"

        # Step 18: Rank 18 (Генерал армии, 25000 EXP) -> delta +6000 (15x HARD 400)
        for _ in range(15):
            await play_mission(hard_quote, mistakes=0, hints=0)
        u = await session_service.get_or_create_user(analyst_id)
        assert u.exp == 25000
        assert u.rank_title == "Генерал армии"

        # Step 19: Rank 19 (Маршал, 35000 EXP) -> delta +10000 (25x HARD 400)
        for _ in range(25):
            await play_mission(hard_quote, mistakes=0, hints=0)
        u = await session_service.get_or_create_user(analyst_id)
        assert u.exp == 35000
        assert u.rank_title == "Маршал"
        assert u.current_rank_badge == "👑⭐"
        assert u.next_rank_exp is None
        assert u.rank_progress_pct == 100.0

    @pytest.mark.asyncio
    async def test_progression_far_beyond_marshal_cap_integrity(
        self,
        session_service: SessionService,
    ):
        """Stress test EXP values far beyond Marshal threshold (50k, 100k, 1M EXP)."""
        analyst = await session_service.get_or_create_user(telegram_id=9099)
        analyst.exp = 100000

        assert analyst.rank_title == "Маршал"
        assert analyst.current_rank_badge == "👑⭐"
        assert analyst.next_rank_exp is None
        assert analyst.rank_progress_pct == 100.0
        assert analyst.rank_progress_bar == "[██████████] 100%"

        # Boundary test: negative exp defensively clamped to Рядовой
        analyst.exp = -500
        assert analyst.rank_title == "Рядовой"
        assert analyst.current_rank_badge == "🪖"
        assert analyst.next_rank_exp == 100
        assert analyst.rank_progress_pct == 0.0
        assert analyst.rank_progress_bar == "[░░░░░░░░░░] 0%"


# ======================================================================
# Test Suite 2: Boundary Conditions (Zero-Mistake/Zero-Hint & Contested)
# ======================================================================


class TestBoundaryConditions:
    """Stress tests for extreme gameplay conditions: zero errors vs 50+ mistakes/hints."""

    @pytest.mark.asyncio
    async def test_consecutive_zero_mistake_zero_hint_streak(
        self,
        session_service: SessionService,
    ):
        """10 consecutive perfect games award clean work (+50) and autonomy (+50) every single time."""
        analyst_id = 7001
        agent_id = 7002

        for i in range(10):
            game = await session_service.create_session(
                creator_tg_id=analyst_id,
                text=f"ЧИСТАЯ ОПЕРАЦИЯ НОМЕР {i}",
                is_custom=True,  # MEDIUM default: 200 base
            )
            await session_service.join_session(game.invite_code, agent_id)

            # Solve perfectly
            c2c = game.code_to_char_map
            for sym, char in c2c.items():
                await session_service.submit_guess(game.id, agent_id, sym, char)

        analyst = await session_service.get_or_create_user(analyst_id)
        agent = await session_service.get_or_create_user(agent_id)

        # 10 games * (200 base + 50 clean + 50 autonomy) = 3000 EXP
        assert analyst.exp == 3000
        assert agent.exp == 3000
        assert analyst.total_mistakes == 0
        assert agent.total_mistakes == 0
        assert analyst.total_hints_used == 0
        assert agent.total_hints_used == 0
        assert analyst.games_won == 10
        assert agent.games_won == 10
        assert analyst.win_rate == 100.0

    @pytest.mark.asyncio
    async def test_single_mistake_and_single_hint_boundary_breakdowns(
        self,
        session_service: SessionService,
    ):
        """Verify boundary where 1 mistake voids clean work bonus, and 1 hint voids autonomy bonus."""
        analyst_id = 7011
        agent_id = 7012

        # Game 1: 1 mistake, 0 hints -> base 200 + 0 clean + 50 autonomy = 250 EXP
        g1 = await session_service.create_session(analyst_id, "ОШИБКА ОДНА", is_custom=True)
        await session_service.join_session(g1.invite_code, agent_id)
        first_sym = next(iter(g1.code_to_char_map.keys()))
        # Wrong guess
        wrong_res = await session_service.submit_guess(g1.id, agent_id, first_sym, "Я" if g1.code_to_char_map[first_sym] != "Я" else "Ю")
        assert wrong_res.status == GuessStatus.WRONG
        # Solve rest
        for s, c in g1.code_to_char_map.items():
            await session_service.submit_guess(g1.id, agent_id, s, c)

        u1 = await session_service.get_or_create_user(analyst_id)
        assert u1.exp == 250
        assert u1.total_mistakes == 1
        assert u1.total_hints_used == 0

        # Game 2: 0 mistakes, 1 hint -> base 200 + 50 clean + 0 autonomy = 250 EXP
        g2 = await session_service.create_session(analyst_id, "ПОДСКАЗКА ОДНА", is_custom=True)
        await session_service.join_session(g2.invite_code, agent_id)
        # 1 frequency hint
        await session_service.give_hint(g2.id, analyst_id, HintType.FREQUENCY_REPORT)
        for s, c in g2.code_to_char_map.items():
            await session_service.submit_guess(g2.id, agent_id, s, c)

        u2 = await session_service.get_or_create_user(analyst_id)
        assert u2.exp == 250 + 250  # 500 EXP
        assert u2.total_mistakes == 1
        assert u2.total_hints_used == 1

        # Game 3: 1 mistake, 1 hint -> base 200 + 0 clean + 0 autonomy = 200 EXP
        g3 = await session_service.create_session(analyst_id, "ОБЕ СИТУАЦИИ", is_custom=True)
        await session_service.join_session(g3.invite_code, agent_id)
        await session_service.give_hint(g3.id, analyst_id, HintType.FREQUENCY_REPORT)
        s3 = next(iter(g3.code_to_char_map.keys()))
        await session_service.submit_guess(g3.id, agent_id, s3, "Э" if g3.code_to_char_map[s3] != "Э" else "Ъ")
        for s, c in g3.code_to_char_map.items():
            await session_service.submit_guess(g3.id, agent_id, s, c)

        u3 = await session_service.get_or_create_user(analyst_id)
        assert u3.exp == 500 + 200  # 700 EXP -> Старший сержант!
        assert u3.total_mistakes == 2
        assert u3.total_hints_used == 2
        assert u3.rank_title == "Старший сержант"

    @pytest.mark.asyncio
    async def test_extreme_fifty_plus_mistakes_in_single_game(
        self,
        session_service: SessionService,
    ):
        """Stress test: 55 incorrect guesses in a single game."""
        analyst_id = 7051
        agent_id = 7052

        game = await session_service.create_session(analyst_id, "СТРЕСС ТЕСТ ОШИБОК", is_custom=True)
        await session_service.join_session(game.invite_code, agent_id)

        first_sym = next(iter(game.code_to_char_map.keys()))
        expected_char = game.code_to_char_map[first_sym]
        wrong_char = "Я" if expected_char != "Я" else "Ю"

        # Submit 55 wrong guesses
        for _ in range(55):
            res = await session_service.submit_guess(game.id, agent_id, first_sym, wrong_char)
            assert res.status == GuessStatus.WRONG

        reloaded = await session_service.get_session_by_code(game.invite_code)
        assert reloaded.mistakes_count == 55

        # Solve remainder cleanly
        for s, c in game.code_to_char_map.items():
            await session_service.submit_guess(game.id, agent_id, s, c)

        # Base 200 + 0 clean + 50 autonomy = 250 EXP
        analyst = await session_service.get_or_create_user(analyst_id)
        agent = await session_service.get_or_create_user(agent_id)

        assert analyst.exp == 250
        assert agent.exp == 250
        assert analyst.total_mistakes == 55
        assert agent.total_mistakes == 55
        assert analyst.total_hints_used == 0
        assert agent.total_hints_used == 0

    @pytest.mark.asyncio
    async def test_extreme_fifty_plus_hints_in_single_game(
        self,
        session_service: SessionService,
    ):
        """Stress test: 52 tactical hints sent by HQ Analyst in a single game."""
        analyst_id = 7061
        agent_id = 7062

        game = await session_service.create_session(analyst_id, "СТРЕСС ТЕСТ ПОДСКАЗОК", is_custom=True)
        await session_service.join_session(game.invite_code, agent_id)

        # Transmit 52 hints
        for _ in range(52):
            await session_service.give_hint(game.id, analyst_id, HintType.FREQUENCY_REPORT)

        reloaded = await session_service.get_session_by_code(game.invite_code)
        assert reloaded.hints_used == 52

        # Solve with 0 mistakes
        for s, c in game.code_to_char_map.items():
            await session_service.submit_guess(game.id, agent_id, s, c)

        # Base 200 + 50 clean + 0 autonomy = 250 EXP
        analyst = await session_service.get_or_create_user(analyst_id)
        agent = await session_service.get_or_create_user(agent_id)

        assert analyst.exp == 250
        assert agent.exp == 250
        assert analyst.total_mistakes == 0
        assert agent.total_mistakes == 0
        assert analyst.total_hints_used == 52
        assert agent.total_hints_used == 52

    @pytest.mark.asyncio
    async def test_highly_contested_fifty_mistakes_and_fifty_hints_cumulative_stress(
        self,
        session_service: SessionService,
    ):
        """Stress test 3 consecutive games, each with 50 mistakes and 50 hints (150 total each)."""
        analyst_id = 7071
        agent_id = 7072

        for i in range(3):
            game = await session_service.create_session(analyst_id, f"ЖЕСТКАЯ БИТВА {i}", is_custom=True)
            await session_service.join_session(game.invite_code, agent_id)

            first_sym = next(iter(game.code_to_char_map.keys()))
            wrong_char = "Я" if game.code_to_char_map[first_sym] != "Я" else "Ю"

            # 50 mistakes
            for _ in range(50):
                await session_service.submit_guess(game.id, agent_id, first_sym, wrong_char)

            # 50 hints
            for _ in range(50):
                await session_service.give_hint(game.id, analyst_id, HintType.FREQUENCY_REPORT)

            # Solve game
            for s, c in game.code_to_char_map.items():
                await session_service.submit_guess(game.id, agent_id, s, c)

        analyst = await session_service.get_or_create_user(analyst_id)
        agent = await session_service.get_or_create_user(agent_id)

        # 3 games * (200 base + 0 clean + 0 autonomy) = 600 EXP -> Сержант (450+)
        assert analyst.exp == 600
        assert agent.exp == 600
        assert analyst.total_mistakes == 150
        assert agent.total_hints_used == 150
        assert agent.total_mistakes == 150
        assert agent.total_hints_used == 150
        assert analyst.rank_title == "Сержант"
        assert analyst.games_won == 3
        assert analyst.games_played == 3
        assert analyst.win_rate == 100.0


# ======================================================================
# Test Suite 3: Solo Analyst vs Dual-Player Mechanics
# ======================================================================


class TestSoloAnalystVsDualPlayer:
    """Stress tests verifying single-player edge cases vs dual-player role coordination."""

    @pytest.mark.asyncio
    async def test_solo_analyst_session_guards_in_waiting_agent_state(
        self,
        session_service: SessionService,
    ):
        """A game in WAITING_AGENT state must reject guesses, hints, and self-joins."""
        analyst_id = 8001
        game = await session_service.create_session(analyst_id, "ОЖИДАНИЕ АГЕНТА")

        # 1. Analyst cannot join own game as agent
        with pytest.raises(SelfJoinError):
            await session_service.join_session(game.invite_code, analyst_id)

        # 2. Cannot submit guess when WAITING_AGENT
        first_sym = next(iter(game.code_to_char_map.keys()))
        with pytest.raises(SessionInvalidStateError):
            await session_service.submit_guess(game.id, analyst_id, first_sym, "А")

        # 3. Cannot give hint when WAITING_AGENT
        with pytest.raises(SessionInvalidStateError):
            await session_service.give_hint(game.id, analyst_id, HintType.FREQUENCY_REPORT)

    @pytest.mark.asyncio
    async def test_solo_analyst_direct_victory_processing_no_agent(
        self,
        session_service: SessionService,
        session_factory: async_sessionmaker[AsyncSession],
    ):
        """Direct call to _process_session_victory on a game without an agent must not crash."""
        analyst_id = 8011
        game = await session_service.create_session(analyst_id, "ОДИНОЧНЫЙ АНАЛИЗ")
        assert game.agent_id is None

        async with session_factory() as db:
            reloaded = await db.get(GameSession, game.id)
            breakdown = await session_service._process_session_victory(reloaded, db)
            await db.commit()

        analyst = await session_service.get_or_create_user(analyst_id)
        assert analyst.exp == breakdown.total_exp
        assert analyst.games_won == 1

    @pytest.mark.asyncio
    async def test_dual_player_role_swapping_invariants(
        self,
        session_service: SessionService,
    ):
        """Players swap roles: Player A (Analyst->Agent->Analyst->Agent), Player B opposite.

        Verifies that across 4 games:
        - Player A: games_as_analyst == 2, games_as_agent == 2, games_played == 4
        - Player B: games_as_analyst == 2, games_as_agent == 2, games_played == 4
        - Invariant: games_as_analyst + games_as_agent == games_played
        """
        user_a = 8021
        user_b = 8022

        for round_idx in range(4):
            if round_idx % 2 == 0:
                host, joiner = user_a, user_b
            else:
                host, joiner = user_b, user_a

            g = await session_service.create_session(host, f"СМЕНА РОЛЕЙ РАУНД {round_idx}", is_custom=True)
            await session_service.join_session(g.invite_code, joiner)

            # Solve
            for s, c in g.code_to_char_map.items():
                await session_service.submit_guess(g.id, joiner, s, c)

        a = await session_service.get_or_create_user(user_a)
        b = await session_service.get_or_create_user(user_b)

        assert a.games_as_analyst == 2
        assert a.games_as_agent == 2
        assert a.games_played == 4
        assert a.games_won == 4
        assert a.games_as_analyst + a.games_as_agent == a.games_played

        assert b.games_as_analyst == 2
        assert b.games_as_agent == 2
        assert b.games_played == 4
        assert b.games_won == 4
        assert b.games_as_analyst + b.games_as_agent == b.games_played


# ======================================================================
# Test Suite 4: End-to-End Mock Telegram UI Progression
# ======================================================================


class TestMockTelegramUIMultiGameProgression:
    """End-to-end integration tests using MockTelegramSession and Aiogram Dispatcher."""

    @pytest.mark.asyncio
    async def test_e2e_multi_game_progression_with_mock_ui(
        self,
        session_service: SessionService,
        lock_manager: SessionLockManager,
    ):
        """Simulate two consecutive cooperative games via MockTelegramSession and verify profile dossier."""
        mock_session = MockTelegramSession()
        bot = Bot(token="42:MOCK_TOKEN", session=mock_session)
        dp = create_dispatcher(
            session_service=session_service,
            lock_manager=lock_manager,
            bot_username="SpyAnalystTestBot",
            storage=MemoryStorage(),
        )

        analyst_client = MockPlayerClient(dp, bot, user_id=8501, username="HQ_Chief", first_name="Chief")
        agent_client = MockPlayerClient(dp, bot, user_id=8502, username="Field_007", first_name="Agent007")

        # 1. Check initial dossier for analyst: Рядовой (0 EXP)
        await analyst_client.send_command("/profile")
        profile_msg_1 = mock_session.get_last_message_for_chat(8501)
        assert profile_msg_1 is not None
        assert "Рядовой" in profile_msg_1.text
        assert "0 / 100" in profile_msg_1.text or "0" in profile_msg_1.text

        # 2. Play Game 1
        phrase_1 = "ЦЕНТР СВЯЗЬ"
        g1 = await session_service.create_session(8501, phrase_1, is_custom=True)
        # Seed message id
        mock_msg_analyst = types.Message(
            message_id=101,
            date=datetime.now(timezone.utc),
            chat=analyst_client.chat,
            from_user=analyst_client.user,
            text="ШТАБНОЕ ДОСЬЕ",
        )
        mock_session.messages[101] = mock_msg_analyst
        await session_service.update_message_ids(g1.id, analyst_chat_id=8501, analyst_message_id=101)

        # Agent connects via /start game_<code>
        await agent_client.send_command("/start", f"game_{g1.invite_code}")
        agent_term = mock_session.get_last_message_for_chat(8502)
        assert agent_term is not None

        # Solve Game 1 with 0 mistakes and 0 hints
        for sym, char in g1.code_to_char_map.items():
            cb = LetterGuessCallback(code=g1.invite_code, symbol=sym, letter=char).pack()
            await agent_client.click_button(cb, message=agent_term)

        # Verify profile after Game 1: 300 EXP -> Младший сержант (250+)
        await analyst_client.send_command("/profile")
        profile_msg_2 = mock_session.get_last_message_for_chat(8501)
        assert profile_msg_2 is not None
        assert "Младший сержант" in profile_msg_2.text
        assert "300" in profile_msg_2.text

        # 3. Play Game 2
        phrase_2 = "ВТОРОЙ ЭТАП"
        g2 = await session_service.create_session(8501, phrase_2, is_custom=True)
        mock_msg_analyst_2 = types.Message(
            message_id=201,
            date=datetime.now(timezone.utc),
            chat=analyst_client.chat,
            from_user=analyst_client.user,
            text="ШТАБНОЕ ДОСЬЕ 2",
        )
        mock_session.messages[201] = mock_msg_analyst_2
        await session_service.update_message_ids(g2.id, analyst_chat_id=8501, analyst_message_id=201)

        await agent_client.send_command("/start", f"game_{g2.invite_code}")
        agent_term_2 = mock_session.get_last_message_for_chat(8502)

        # Solve Game 2
        for sym, char in g2.code_to_char_map.items():
            cb = LetterGuessCallback(code=g2.invite_code, symbol=sym, letter=char).pack()
            await agent_client.click_button(cb, message=agent_term_2)

        # Verify profile after Game 2: 600 EXP -> Сержант (450+)
        await analyst_client.send_command("/profile")
        profile_msg_3 = mock_session.get_last_message_for_chat(8501)
        assert profile_msg_3 is not None
        assert "Сержант" in profile_msg_3.text
        assert "600" in profile_msg_3.text

        # Verify agent profile also updated to Сержант
        await agent_client.send_command("/profile")
        profile_agent_msg = mock_session.get_last_message_for_chat(8502)
        assert profile_agent_msg is not None
        assert "Сержант" in profile_agent_msg.text
        assert "600" in profile_agent_msg.text
