"""Empirical Challenger Adversarial Test Suite for Milestone 2: Victory Processing & EXP Accrual.

Empirically challenges:
1. Concurrency and mutex locking on victory transitions:
   - 20-way concurrent winning guesses race on the final symbol.
   - Victory race between Agent guess and Analyst hint on the exact same final symbol.
   - Multi-session concurrent victories without lock contention.
   - Mutex lock lifecycle and active lock pruning to zero post-victory.
   - Strict rejection of post-victory actions (no double-accrual or duplicate EXP).
2. Victory via `submit_guess` vs `give_hint`:
   - Victory via `submit_guess` preserves autonomy bonus (0 hints).
   - Victory via `give_hint` forfeits autonomy bonus (hints_used >= 1) and persists HintLog.
3. EXP awards matrix:
   - EASY catalog quotes (base 100).
   - MEDIUM catalog quotes (base 200).
   - HARD catalog quotes (base 300).
   - Custom quotes (is_custom_quote=True, catalog_id=None -> base 200).
   - Unknown or missing catalog items defaulting to MEDIUM base EXP.
4. Clean work and autonomy bonus permutations:
   - (0 mistakes, 0 hints) -> +50 clean, +50 autonomy.
   - (N mistakes, 0 hints) -> 0 clean, +50 autonomy.
   - (0 mistakes, N hints) -> +50 clean, 0 autonomy (tested with letter reveal, frequency report, and depeche).
   - (N mistakes, N hints) -> 0 clean, 0 autonomy.
   - Verification of cumulative user total_mistakes and total_hints_used.
5. Single-player vs two-player session victory handling:
   - Two-player session dual-award symmetry.
   - Solo analyst session victory handling without agent crash.
6. Atomicity and transaction rollback safety:
   - Rollback on database commit failure during victory processing (zero partial state leaks).
   - Clean lock release and recovery after failed victory attempt.
"""
import asyncio
import secrets
from typing import AsyncGenerator
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from cryptogram.catalog import get_catalog_item
from cryptogram.models import GuessStatus
from cryptogram.ranks import (
    AUTONOMY_BONUS_EXP,
    CLEAN_WORK_BONUS_EXP,
    DIFFICULTY_BASE_EXP,
    calculate_mission_exp,
)
from database.models import GameSession, HintLog, HintType, SessionStatus, User
from database.session import (
    create_session_factory,
    create_sqlite_async_engine,
    init_db,
)
from services.lock_manager import SessionLockManager
from services.session_service import (
    InvalidHintError,
    SessionFinishedError,
    SessionInvalidStateError,
    SessionNotFoundError,
    SessionService,
    UnauthorizedActionError,
)


# =====================================================================
# Fixtures
# =====================================================================

@pytest_asyncio.fixture(scope="function")
async def file_engine(tmp_path) -> AsyncGenerator[AsyncEngine, None]:
    """File-backed SQLite engine in WAL mode for realistic disk concurrency."""
    db_file = tmp_path / f"adversarial_victory_{secrets.token_hex(4)}.db"
    engine = create_sqlite_async_engine(f"sqlite+aiosqlite:///{db_file}", echo=False)
    await init_db(engine)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def session_factory(file_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(file_engine)


@pytest.fixture(scope="function")
def lock_manager() -> SessionLockManager:
    return SessionLockManager()


@pytest_asyncio.fixture(scope="function")
async def session_service(
    session_factory: async_sessionmaker[AsyncSession],
    lock_manager: SessionLockManager,
) -> SessionService:
    return SessionService(session_factory=session_factory, lock_manager=lock_manager)


# =====================================================================
# Helpers
# =====================================================================

async def solve_all_except_last_n(
    session_service: SessionService,
    game: GameSession,
    agent_id: int,
    n_remaining: int = 1,
):
    """Solve symbols until exactly n_remaining unique symbols are left unrevealed."""
    c2c = game.code_to_char_map
    codes = list(c2c.keys())
    for code in codes[:-n_remaining]:
        await session_service.submit_guess(game.id, agent_id, code, c2c[code])


# =====================================================================
# 1. Concurrency and Mutex Locking on Victory Transitions
# =====================================================================

@pytest.mark.database
class TestAdversarialVictoryConcurrency:
    """Empirical concurrency stress tests on victory transitions."""

    async def test_20_way_concurrent_winning_guesses_race(
        self,
        session_service: SessionService,
        lock_manager: SessionLockManager,
    ):
        """20 coroutines concurrently submit the winning guess for the final symbol.

        Invariants:
        - Exactly 1 coroutine transitions status to VICTORY.
        - Exactly 19 coroutines are rejected with SessionFinishedError (or ALREADY_REVEALED).
        - EXP and games_won are awarded EXACTLY ONCE to both players.
        - LockManager active count returns to 0 (no leaked locks).
        """
        analyst = await session_service.get_or_create_user(telegram_id=201001, username="hq_lead")
        agent = await session_service.get_or_create_user(telegram_id=201002, username="field_agent")

        game = await session_service.create_session(
            creator_tg_id=analyst.id,
            text="ШИФРОВАННЫЙ КЛЮЧ",
            catalog_id="gagarin_poekhali",
        )
        await session_service.join_session(game.invite_code, agent.id)

        c2c = game.code_to_char_map
        codes = list(c2c.keys())

        # Solve all except the last symbol
        await solve_all_except_last_n(session_service, game, agent.id, n_remaining=1)

        last_code = codes[-1]
        last_char = c2c[last_code]

        # 20 concurrent coroutines attempting the winning guess
        tasks = [
            session_service.submit_guess(game.id, agent.id, last_code, last_char)
            for _ in range(20)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        victories = [r for r in results if not isinstance(r, Exception) and r.status == GuessStatus.VICTORY]
        finished_errors = [r for r in results if isinstance(r, SessionFinishedError)]
        other_exceptions = [r for r in results if isinstance(r, Exception) and not isinstance(r, SessionFinishedError)]

        assert len(victories) == 1, f"Strictly 1 coroutine must win the race, got {len(victories)}"
        assert len(finished_errors) == 19, f"Strictly 19 coroutines must receive SessionFinishedError, got {len(finished_errors)}"
        assert len(other_exceptions) == 0, f"Unexpected exceptions: {other_exceptions}"

        # Verify DB user metrics: exactly 1 game won, exactly 1 EXP accrual
        analyst_db = await session_service.get_or_create_user(analyst.id)
        agent_db = await session_service.get_or_create_user(agent.id)

        # EASY catalog: base 100 + clean 50 + autonomy 50 = 200 EXP
        assert analyst_db.games_won == 1
        assert agent_db.games_won == 1
        assert analyst_db.exp == 200
        assert agent_db.exp == 200

        # Verify lock manager prunes all locks
        assert lock_manager.get_active_count() == 0, (
            f"Expected 0 active locks, found {lock_manager.get_active_count()}"
        )

    async def test_race_between_agent_winning_guess_and_analyst_winning_hint(
        self,
        session_service: SessionService,
        lock_manager: SessionLockManager,
    ):
        """Simultaneous race on the final remaining symbol:
        Agent submits winning guess WHILE Analyst submits letter reveal hint for that same symbol!

        Invariants:
        - Only one operation applies victory.
        - The competitor operation is rejected cleanly (SessionFinishedError, SessionInvalidStateError, or InvalidHintError).
        - EXP awarded exactly once, games_won incremented exactly once.
        - Zero partial corruption.
        """
        analyst = await session_service.get_or_create_user(telegram_id=202001)
        agent = await session_service.get_or_create_user(telegram_id=202002)

        game = await session_service.create_session(
            creator_tg_id=analyst.id,
            text="СОВЕРШЕННО СЕКРЕТНО",
            catalog_id="sorge_tokyo_1941",  # MEDIUM
        )
        await session_service.join_session(game.invite_code, agent.id)

        c2c = game.code_to_char_map
        codes = list(c2c.keys())

        # Pre-solve all except the final code
        await solve_all_except_last_n(session_service, game, agent.id, n_remaining=1)

        final_code = codes[-1]
        final_char = c2c[final_code]

        # Launch guess and hint concurrently for the same final code
        results = await asyncio.gather(
            session_service.submit_guess(game.id, agent.id, final_code, final_char),
            session_service.give_hint(game.id, analyst.id, symbol_code=final_code),
            return_exceptions=True,
        )

        guess_res, hint_res = results[0], results[1]

        # One must succeed in solving the session, the other must fail cleanly
        if not isinstance(guess_res, Exception) and guess_res.status == GuessStatus.VICTORY:
            # Guess won race: hint should fail because game is already SOLVED or symbol already revealed
            assert isinstance(hint_res, (SessionInvalidStateError, InvalidHintError)), (
                f"Expected hint to be rejected after guess victory, got {hint_res}"
            )
        elif not isinstance(hint_res, Exception) and hint_res.status == "HINT_APPLIED":
            # Hint won race: guess should fail because game is already SOLVED
            assert isinstance(guess_res, SessionFinishedError), (
                f"Expected guess to be rejected after hint victory, got {guess_res}"
            )
        else:
            pytest.fail(f"Unexpected race outcome: guess={guess_res}, hint={hint_res}")

        # In both outcomes, victory was processed exactly once
        analyst_db = await session_service.get_or_create_user(analyst.id)
        agent_db = await session_service.get_or_create_user(agent.id)
        assert analyst_db.games_won == 1
        assert agent_db.games_won == 1

        # Check EXP: MEDIUM base is 200
        # If guess won: 200 + 50 (clean) + 50 (autonomy) = 300
        # If hint won: 200 + 50 (clean) + 0 (hint used) = 250
        assert analyst_db.exp in (250, 300)
        assert agent_db.exp == analyst_db.exp

        assert lock_manager.get_active_count() == 0

    async def test_multi_session_concurrent_victories(
        self,
        session_service: SessionService,
        lock_manager: SessionLockManager,
    ):
        """5 independent game rooms achieve victory concurrently.
        Verifies that locks across distinct sessions do not block or interfere with each other.
        """
        games = []
        for i in range(5):
            analyst = await session_service.get_or_create_user(telegram_id=300000 + i * 2 + 1)
            agent = await session_service.get_or_create_user(telegram_id=300000 + i * 2 + 2)
            game = await session_service.create_session(
                creator_tg_id=analyst.id,
                text=f"СЕССИЯ НОМЕР {i + 1}",
                catalog_id="gagarin_poekhali",
            )
            await session_service.join_session(game.invite_code, agent.id)
            await solve_all_except_last_n(session_service, game, agent.id, n_remaining=1)
            games.append((game, analyst, agent))

        # Solve all 5 simultaneously
        tasks = []
        for game, _, agent in games:
            c2c = game.code_to_char_map
            final_code = list(c2c.keys())[-1]
            tasks.append(
                session_service.submit_guess(game.id, agent.id, final_code, c2c[final_code])
            )

        results = await asyncio.gather(*tasks, return_exceptions=True)

        assert all(not isinstance(r, Exception) for r in results), f"Errors in concurrent victories: {results}"
        assert all(r.status == GuessStatus.VICTORY for r in results)

        # Verify all sessions solved and all locks released
        for game, analyst, agent in games:
            reloaded = await session_service.get_session_by_id(game.id)
            assert reloaded.status == SessionStatus.SOLVED
            an = await session_service.get_or_create_user(analyst.id)
            ag = await session_service.get_or_create_user(agent.id)
            assert an.games_won == 1
            assert ag.games_won == 1
            assert an.exp == 200
            assert ag.exp == 200

        assert lock_manager.get_active_count() == 0

    async def test_strict_rejection_of_post_victory_actions(
        self,
        session_service: SessionService,
    ):
        """Verify that once a session is SOLVED, no further actions can alter stats or award EXP."""
        analyst = await session_service.get_or_create_user(telegram_id=400001)
        agent = await session_service.get_or_create_user(telegram_id=400002)

        game = await session_service.create_session(
            creator_tg_id=analyst.id,
            text="КОНЕЦ СЕАНСА",
        )
        await session_service.join_session(game.invite_code, agent.id)

        # Solve the game completely
        c2c = game.code_to_char_map
        for code, char in c2c.items():
            await session_service.submit_guess(game.id, agent.id, code, char)

        reloaded = await session_service.get_session_by_id(game.id)
        assert reloaded.status == SessionStatus.SOLVED

        analyst_before = await session_service.get_or_create_user(analyst.id)
        agent_before = await session_service.get_or_create_user(agent.id)

        # 1. Post-victory guess must raise SessionFinishedError
        with pytest.raises(SessionFinishedError):
            await session_service.submit_guess(game.id, agent.id, 1, "А")

        # 2. Post-victory hint must raise SessionInvalidStateError
        with pytest.raises(SessionInvalidStateError):
            await session_service.give_hint(game.id, analyst.id, hint_type=HintType.FREQUENCY_REPORT)

        # 3. Post-victory join must raise SessionFinishedError
        with pytest.raises(SessionFinishedError):
            await session_service.join_session(game.invite_code, 999999)

        # Verify stats are strictly frozen
        analyst_after = await session_service.get_or_create_user(analyst.id)
        agent_after = await session_service.get_or_create_user(agent.id)

        assert analyst_after.exp == analyst_before.exp
        assert agent_after.exp == agent_before.exp
        assert analyst_after.games_won == analyst_before.games_won
        assert agent_after.games_won == agent_before.games_won


# =====================================================================
# 2. Victory via submit_guess vs give_hint
# =====================================================================

@pytest.mark.database
class TestVictoryGuessVsHint:
    """Empirical verification of victory mechanics triggered by guess vs hint."""

    async def test_victory_via_submit_guess_clean_autonomy(
        self,
        session_service: SessionService,
    ):
        """Victory triggered by Agent's submit_guess with 0 mistakes and 0 hints.

        Must award base EXP + clean work bonus (+50) + autonomy bonus (+50).
        """
        analyst = await session_service.get_or_create_user(telegram_id=500001)
        agent = await session_service.get_or_create_user(telegram_id=500002)

        game = await session_service.create_session(
            creator_tg_id=analyst.id,
            text="СУВОРОВ НАУКА",
            catalog_id="suvorov_nauka",  # EASY
        )
        await session_service.join_session(game.invite_code, agent.id)

        c2c = game.code_to_char_map
        for code, char in c2c.items():
            res = await session_service.submit_guess(game.id, agent.id, code, char)

        assert res.status == GuessStatus.VICTORY
        assert res.is_victory is True

        an = await session_service.get_or_create_user(analyst.id)
        ag = await session_service.get_or_create_user(agent.id)

        # EASY: 100 + 50 + 50 = 200
        assert an.exp == 200
        assert ag.exp == 200
        assert an.total_mistakes == 0
        assert ag.total_mistakes == 0
        assert an.total_hints_used == 0
        assert ag.total_hints_used == 0

    async def test_victory_via_give_hint_reveals_final_letter(
        self,
        session_service: SessionService,
    ):
        """Victory triggered by Analyst's give_hint letter reveal.

        Must forfeit autonomy bonus (hints_used = 1), keep clean work bonus (mistakes = 0).
        HintLog must be recorded and committed.
        """
        analyst = await session_service.get_or_create_user(telegram_id=500003)
        agent = await session_service.get_or_create_user(telegram_id=500004)

        game = await session_service.create_session(
            creator_tg_id=analyst.id,
            text="НЕВСКИЙ ПРИЗЫВ",
            catalog_id="nevsky_prizyv",  # EASY
        )
        await session_service.join_session(game.invite_code, agent.id)

        c2c = game.code_to_char_map
        codes = list(c2c.keys())

        # Solve all except final symbol via guess
        await solve_all_except_last_n(session_service, game, agent.id, n_remaining=1)

        final_code = codes[-1]
        final_char = c2c[final_code]

        # Final symbol revealed by Analyst hint!
        hint_res = await session_service.give_hint(game.id, analyst.id, symbol_code=final_code)
        assert hint_res.status == "HINT_APPLIED"

        reloaded = await session_service.get_session_by_id(game.id)
        assert reloaded.status == SessionStatus.SOLVED
        assert reloaded.hints_used == 1
        assert reloaded.mistakes_count == 0

        an = await session_service.get_or_create_user(analyst.id)
        ag = await session_service.get_or_create_user(agent.id)

        # EASY: 100 + 50 (clean) + 0 (autonomy forfeited due to hint) = 150 EXP
        assert an.exp == 150
        assert ag.exp == 150
        assert an.total_hints_used == 1
        assert ag.total_hints_used == 1
        assert an.total_mistakes == 0
        assert ag.total_mistakes == 0

        # Check HintLog record
        assert len(reloaded.hints) == 1
        log = reloaded.hints[0]
        assert log.hint_type == HintType.REVEAL_LETTER
        assert log.symbol_code == final_code
        assert log.letter == final_char


# =====================================================================
# 3. EXP Awards Matrix across Difficulties and Custom Quotes
# =====================================================================

@pytest.mark.database
class TestExpAwardsMatrix:
    """Empirical testing of EXP calculations across catalog difficulties and custom texts."""

    @pytest.mark.parametrize(
        "catalog_id, is_custom, mistakes, hints, expected_base, expected_clean, expected_autonomy, expected_total",
        [
            ("gagarin_poekhali", False, 0, 0, 100, 50, 50, 200),     # EASY clean & autonomous
            ("gagarin_poekhali", False, 1, 0, 100, 0, 50, 150),      # EASY mistake forfeited clean
            ("sorge_tokyo_1941", False, 0, 0, 200, 50, 50, 300),     # MEDIUM clean & autonomous
            ("sorge_tokyo_1941", False, 0, 1, 200, 50, 0, 250),      # MEDIUM hint forfeited autonomy
            ("stavka_1914_corps", False, 0, 0, 300, 50, 50, 400),    # HARD clean & autonomous
            ("stavka_1914_corps", False, 2, 3, 300, 0, 0, 300),      # HARD both bonuses forfeited
            (None, True, 0, 0, 200, 50, 50, 300),                     # Custom quote defaults to MEDIUM
            (None, True, 3, 1, 200, 0, 0, 200),                      # Custom quote no bonuses
        ],
    )
    async def test_exp_awards_end_to_end(
        self,
        session_service: SessionService,
        catalog_id,
        is_custom,
        mistakes,
        hints,
        expected_base,
        expected_clean,
        expected_autonomy,
        expected_total,
    ):
        """End-to-end verification through SessionService creating and solving sessions."""
        uid_base = secrets.randbelow(100000) + 600000
        analyst = await session_service.get_or_create_user(telegram_id=uid_base + 1)
        agent = await session_service.get_or_create_user(telegram_id=uid_base + 2)

        text = "ПРОВЕРОЧНАЯ ДЕПЕША ЦЕНТРА"
        if catalog_id:
            cat_item = get_catalog_item(catalog_id)
            if cat_item:
                text = cat_item.text

        game = await session_service.create_session(
            creator_tg_id=analyst.id,
            text=text,
            is_custom=is_custom,
            catalog_id=catalog_id,
        )
        await session_service.join_session(game.invite_code, agent.id)

        c2c = game.code_to_char_map
        codes = list(c2c.keys())

        # Inject simulated mistakes
        for _ in range(mistakes):
            await session_service.submit_guess(game.id, agent.id, codes[0], "Я" if c2c[codes[0]] != "Я" else "Ю")

        # Inject simulated hints (frequency reports)
        for _ in range(hints):
            await session_service.give_hint(game.id, analyst.id, hint_type=HintType.FREQUENCY_REPORT)

        # Solve all symbols
        for code in codes:
            await session_service.submit_guess(game.id, agent.id, code, c2c[code])

        reloaded = await session_service.get_session_by_id(game.id)
        assert reloaded.status == SessionStatus.SOLVED

        an = await session_service.get_or_create_user(analyst.id)
        ag = await session_service.get_or_create_user(agent.id)

        assert an.exp == expected_total
        assert ag.exp == expected_total
        assert an.total_mistakes == mistakes
        assert ag.total_mistakes == mistakes
        assert an.total_hints_used == hints
        assert ag.total_hints_used == hints

    async def test_unknown_catalog_id_defaults_to_medium(
        self,
        session_service: SessionService,
    ):
        """If a session has a non-existent catalog_id, it must gracefully default to MEDIUM (200 base EXP)."""
        analyst = await session_service.get_or_create_user(telegram_id=650001)
        agent = await session_service.get_or_create_user(telegram_id=650002)

        game = await session_service.create_session(
            creator_tg_id=analyst.id,
            text="НЕИЗВЕСТНАЯ ДЕПЕША ИЗ КАТАЛОГА",
            catalog_id="unknown_mythical_dispatch_999",
        )
        await session_service.join_session(game.invite_code, agent.id)

        c2c = game.code_to_char_map
        for code, char in c2c.items():
            await session_service.submit_guess(game.id, agent.id, code, char)

        an = await session_service.get_or_create_user(analyst.id)
        ag = await session_service.get_or_create_user(agent.id)

        # MEDIUM base: 200 + 50 clean + 50 autonomy = 300 EXP
        assert an.exp == 300
        assert ag.exp == 300


# =====================================================================
# 4. Bonuses for Clean Work and Autonomy
# =====================================================================

@pytest.mark.database
class TestBonusesCleanWorkAndAutonomy:
    """Empirically stress-test clean work and autonomy bonus conditions."""

    async def test_frequency_report_hint_forfeits_autonomy_bonus(
        self,
        session_service: SessionService,
    ):
        """Verify that non-reveal hints (FREQUENCY_REPORT) increment hints_used and forfeit autonomy bonus."""
        analyst = await session_service.get_or_create_user(telegram_id=701001)
        agent = await session_service.get_or_create_user(telegram_id=701002)

        game = await session_service.create_session(creator_tg_id=analyst.id, text="ДЕПЕША О ЧАСТОТАХ")
        await session_service.join_session(game.invite_code, agent.id)

        # Analyst sends frequency report
        await session_service.give_hint(game.id, analyst.id, hint_type=HintType.FREQUENCY_REPORT)

        # Agent solves text with 0 mistakes
        c2c = game.code_to_char_map
        for code, char in c2c.items():
            await session_service.submit_guess(game.id, agent.id, code, char)

        an = await session_service.get_or_create_user(analyst.id)
        ag = await session_service.get_or_create_user(agent.id)

        # MEDIUM base: 200 + 50 (clean) + 0 (autonomy lost due to frequency hint) = 250 EXP
        assert an.exp == 250
        assert ag.exp == 250
        assert an.total_hints_used == 1
        assert an.total_mistakes == 0

    async def test_radio_depeche_hint_forfeits_autonomy_bonus(
        self,
        session_service: SessionService,
    ):
        """Verify that RADIO_DEPECHE hint also increments hints_used and forfeits autonomy bonus."""
        analyst = await session_service.get_or_create_user(telegram_id=702001)
        agent = await session_service.get_or_create_user(telegram_id=702002)

        game = await session_service.create_session(creator_tg_id=analyst.id, text="РАДИОДЕПЕША ШТАБА")
        await session_service.join_session(game.invite_code, agent.id)

        # Analyst sends radio depeche
        await session_service.give_hint(game.id, analyst.id, hint_type=HintType.RADIO_DEPECHE)

        # Agent solves text with 0 mistakes
        c2c = game.code_to_char_map
        for code, char in c2c.items():
            await session_service.submit_guess(game.id, agent.id, code, char)

        an = await session_service.get_or_create_user(analyst.id)
        ag = await session_service.get_or_create_user(agent.id)

        # MEDIUM base: 200 + 50 (clean) + 0 (autonomy lost) = 250 EXP
        assert an.exp == 250
        assert ag.exp == 250
        assert an.total_hints_used == 1


# =====================================================================
# 5. Single-Player vs Two-Player Session Victory Handling
# =====================================================================

@pytest.mark.database
class TestSingleVsTwoPlayerVictory:
    """Stress-test single-player and two-player session victory handling."""

    async def test_solo_analyst_session_victory_graceful(
        self,
        session_service: SessionService,
        session_factory: async_sessionmaker[AsyncSession],
    ):
        """If a session has only an Analyst (agent_id is None) and _process_session_victory is invoked,
        it must cleanly award stats to the Analyst without raising NoneType / AttributeError for agent.
        """
        analyst = await session_service.get_or_create_user(telegram_id=801001)

        game = await session_service.create_session(creator_tg_id=analyst.id, text="ОДИНОЧНЫЙ АНАЛИЗ")
        assert game.agent_id is None

        # Directly invoke victory processing
        async with session_factory() as db:
            reloaded_game = await db.get(GameSession, game.id)
            exp_breakdown = await session_service._process_session_victory(reloaded_game, db)
            await db.commit()

        assert exp_breakdown.total_exp == 300  # MEDIUM: 200 + 50 + 50

        an = await session_service.get_or_create_user(analyst.id)
        assert an.games_won == 1
        assert an.exp == 300

    async def test_two_player_session_symmetric_stats(
        self,
        session_service: SessionService,
    ):
        """In a cooperative two-player session, both Analyst and Agent must receive exact symmetric awards."""
        analyst = await session_service.get_or_create_user(telegram_id=802001)
        agent = await session_service.get_or_create_user(telegram_id=802002)

        game = await session_service.create_session(creator_tg_id=analyst.id, text="СИММЕТРИЯ ОПЕРАЦИИ")
        await session_service.join_session(game.invite_code, agent.id)

        c2c = game.code_to_char_map
        for code, char in c2c.items():
            await session_service.submit_guess(game.id, agent.id, code, char)

        an = await session_service.get_or_create_user(analyst.id)
        ag = await session_service.get_or_create_user(agent.id)

        assert an.exp == ag.exp
        assert an.games_won == ag.games_won
        assert an.total_mistakes == ag.total_mistakes
        assert an.total_hints_used == ag.total_hints_used


# =====================================================================
# 6. Atomicity and Rollback Safety
# =====================================================================

@pytest.mark.database
class TestVictoryAtomicityAndRollback:
    """Stress-test rollback safety during victory processing failures."""

    async def test_victory_rollback_on_db_commit_failure(
        self,
        session_service: SessionService,
        lock_manager: SessionLockManager,
    ):
        """Simulate an unexpected database error during commit in submit_guess victory.

        Invariants:
        - Transaction must roll back.
        - Game status must NOT be SOLVED in the DB.
        - Neither player must receive EXP or games_won.
        - Mutex lock must be cleanly released (not left locked).
        """
        analyst = await session_service.get_or_create_user(telegram_id=901001)
        agent = await session_service.get_or_create_user(telegram_id=901002)

        game = await session_service.create_session(creator_tg_id=analyst.id, text="ОТКАТ ТРАНЗАКЦИИ")
        await session_service.join_session(game.invite_code, agent.id)

        c2c = game.code_to_char_map
        codes = list(c2c.keys())

        # Solve all except final symbol
        await solve_all_except_last_n(session_service, game, agent.id, n_remaining=1)

        final_code = codes[-1]
        final_char = c2c[final_code]

        # Patch db.commit to raise an unhandled database error during victory
        real_get_db = session_service._get_db

        class FailingSessionWrapper:
            def __init__(self, real_session):
                self._real = real_session

            def __getattr__(self, name):
                return getattr(self._real, name)

            async def commit(self):
                raise RuntimeError("SIMULATED_DB_COMMIT_FAILURE")

            async def __aenter__(self):
                await self._real.__aenter__()
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                return await self._real.__aexit__(exc_type, exc_val, exc_tb)

        @session_service._get_db
        def mock_get_db(db_session=None):
            pass

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def failing_get_db(db_session=None):
            async with session_service.session_factory() as session:
                yield FailingSessionWrapper(session)

        with patch.object(session_service, "_get_db", failing_get_db):
            with pytest.raises(RuntimeError, match="SIMULATED_DB_COMMIT_FAILURE"):
                await session_service.submit_guess(game.id, agent.id, final_code, final_char)

        # Verify rollback: game is still IN_PROGRESS, not SOLVED
        reloaded = await session_service.get_session_by_id(game.id)
        assert reloaded.status == SessionStatus.IN_PROGRESS
        assert reloaded.is_solved is False

        # Players got NO exp or games_won
        an = await session_service.get_or_create_user(analyst.id)
        ag = await session_service.get_or_create_user(agent.id)
        assert an.games_won == 0
        assert ag.games_won == 0
        assert an.exp == 0
        assert ag.exp == 0

        # Lock was cleanly released
        assert lock_manager.is_locked(f"session:{game.id}") is False
        assert lock_manager.get_active_count() == 0

        # Subsequent legitimate retry succeeds!
        retry_res = await session_service.submit_guess(game.id, agent.id, final_code, final_char)
        assert retry_res.status == GuessStatus.VICTORY

        reloaded_after = await session_service.get_session_by_id(game.id)
        assert reloaded_after.status == SessionStatus.SOLVED

        an_after = await session_service.get_or_create_user(analyst.id)
        ag_after = await session_service.get_or_create_user(agent.id)
        assert an_after.games_won == 1
        assert ag_after.games_won == 1
        assert an_after.exp == 300
        assert ag_after.exp == 300
