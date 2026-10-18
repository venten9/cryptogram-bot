"""Adversarial stress testing and empirical concurrency verification for Milestone M2.

Stress-tests:
1. Massive N-way simultaneous join races (N=10, 20, 50).
2. Simultaneous join by Creator vs Agents.
3. Rapid parallel guess spam by Agent (all unique letters at once).
4. Parallel duplicate guess spam (rapid multi-tap).
5. Cross-role concurrency: Concurrent guess and hint on distinct symbols.
6. Cross-role collision race: Concurrent guess and hint on the EXACT SAME symbol.
7. Victory race: Agent guess vs Analyst hint both trying to solve the game simultaneously.
8. Mixed chaos barrage: 50 concurrent operations (guesses, hints, SOS, invalid actions).
9. Lock key symmetry: Interleaved operations using session.id vs session.invite_code.
10. Lock manager resource lifecycle: Zero memory leaks, active count prunes to 0.
11. Multi-session concurrency: Distinct game rooms do not block each other.
"""
import asyncio
import secrets
from typing import List, Set

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from cryptogram.models import GuessStatus
from database.models import GameSession, HintLog, HintType, SessionStatus, User
from database.session import create_session_factory, create_sqlite_async_engine, init_db
from services.lock_manager import SessionLockManager
from services.session_service import (
    CreatorCannotJoinAsAgent,
    InvalidHintError,
    SelfJoinError,
    SessionAlreadyFull,
    SessionAlreadyStartedError,
    SessionFinishedError,
    SessionInvalidStateError,
    SessionNotFoundError,
    SessionService,
    UnauthorizedActionError,
)


# ==========================================
# Test Fixtures (Isolated File-Backed and Memory)
# ==========================================

@pytest_asyncio.fixture(scope="function")
async def file_engine(tmp_path) -> AsyncEngine:
    """File-backed SQLite engine in WAL mode to stress real I/O locking and concurrency."""
    db_file = tmp_path / f"adversarial_concurrency_{secrets.token_hex(4)}.db"
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


# ==========================================
# 1. Join Concurrency & Race Tests
# ==========================================

@pytest.mark.database
class TestAdversarialJoinRaces:
    """Adversarial stress tests for concurrent player join operations."""

    async def test_massive_join_race_20_agents(
        self,
        session_service: SessionService,
    ):
        """20 distinct simulated agents simultaneously attempt to join the same game room."""
        analyst = await session_service.get_or_create_user(telegram_id=10001, username="hq_lead")
        game = await session_service.create_session(
            creator_tg_id=analyst.id,
            text="СЕКРЕТНАЯ ОПЕРАЦИЯ ЦЕНТРА",
        )

        agent_ids = [20000 + i for i in range(20)]
        for aid in agent_ids:
            await session_service.get_or_create_user(telegram_id=aid, username=f"agent_{aid}")

        # Fire all 20 join attempts concurrently
        tasks = [
            session_service.join_session(invite_code=game.invite_code, agent_tg_id=aid)
            for aid in agent_ids
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        successes = [r for r in results if not isinstance(r, Exception)]
        failures = [r for r in results if isinstance(r, Exception)]

        assert len(successes) == 1, f"Strictly 1 join must succeed, got {len(successes)}"
        assert len(failures) == 19, f"Strictly 19 joins must be rejected, got {len(failures)}"

        for exc in failures:
            assert isinstance(exc, (SessionAlreadyStartedError, SessionAlreadyFull)), (
                f"Expected SessionAlreadyStartedError, got {type(exc)}: {exc}"
            )

        # Verify DB state
        reloaded = await session_service.get_session_by_id(game.id)
        assert reloaded.status == SessionStatus.IN_PROGRESS
        assert reloaded.agent_id in agent_ids

        # Verify games_played stats
        analyst_db = await session_service.get_or_create_user(analyst.id)
        assert analyst_db.games_played == 1, "Analyst games_played must increment exactly once"

        winner_db = await session_service.get_or_create_user(reloaded.agent_id)
        assert winner_db.games_played == 1, "Winning agent games_played must increment exactly once"

        for aid in agent_ids:
            if aid != reloaded.agent_id:
                loser = await session_service.get_or_create_user(aid)
                assert loser.games_played == 0, f"Rejected agent {aid} must have games_played == 0"

    async def test_massive_join_race_50_agents(
        self,
        session_service: SessionService,
    ):
        """High-scale test: 50 distinct simulated agents simultaneously race to join the same room."""
        analyst = await session_service.get_or_create_user(telegram_id=25000, username="hq_overlord")
        game = await session_service.create_session(
            creator_tg_id=analyst.id,
            text="ШИФРОТЕЛЕГРАММА ВЫСШЕЙ СЕКРЕТНОСТИ",
        )

        agent_ids = [26000 + i for i in range(50)]
        for aid in agent_ids:
            await session_service.get_or_create_user(telegram_id=aid, username=f"agent_{aid}")

        tasks = [
            session_service.join_session(invite_code=game.invite_code, agent_tg_id=aid)
            for aid in agent_ids
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        successes = [r for r in results if not isinstance(r, Exception)]
        failures = [r for r in results if isinstance(r, Exception)]

        assert len(successes) == 1, f"Strictly 1 join must succeed in 50-way race, got {len(successes)}"
        assert len(failures) == 49, f"Strictly 49 joins must fail in 50-way race, got {len(failures)}"

        for exc in failures:
            assert isinstance(exc, (SessionAlreadyStartedError, SessionAlreadyFull))

        reloaded = await session_service.get_session_by_id(game.id)
        assert reloaded.status == SessionStatus.IN_PROGRESS
        assert reloaded.agent_id in agent_ids


    async def test_simultaneous_creator_and_agents_join_race(
        self,
        session_service: SessionService,
    ):
        """Analyst (creator) and 5 agents race to join at the exact same instant."""
        analyst = await session_service.get_or_create_user(telegram_id=30001, username="creator_analyst")
        game = await session_service.create_session(creator_tg_id=analyst.id, text="ДЕПЕША ШТАБА")

        agent_ids = [40000 + i for i in range(5)]
        for aid in agent_ids:
            await session_service.get_or_create_user(telegram_id=aid)

        # Analyst races against all 5 agents
        all_candidates = [analyst.id] + agent_ids
        tasks = [
            session_service.join_session(invite_code=game.invite_code, agent_tg_id=cid)
            for cid in all_candidates
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Analyst must NEVER succeed
        analyst_result = results[0]
        assert isinstance(analyst_result, (SelfJoinError, CreatorCannotJoinAsAgent)), (
            f"Analyst self-join must raise SelfJoinError, got {analyst_result}"
        )

        # Among the 5 agents, exactly 1 must succeed
        agent_results = results[1:]
        successes = [r for r in agent_results if not isinstance(r, Exception)]
        failures = [r for r in agent_results if isinstance(r, Exception)]

        assert len(successes) == 1, "Exactly one agent must succeed"
        assert len(failures) == 4, "Remaining 4 agents must fail"

    async def test_join_race_with_invite_code_casing_and_spaces(
        self,
        session_service: SessionService,
    ):
        """Simultaneous joins using various case and whitespace formatting."""
        analyst = await session_service.get_or_create_user(telegram_id=50001, username="casing_creator")
        game = await session_service.create_session(creator_tg_id=analyst.id, text="ШИФРОВКА ПО БОЕВОЙ ТРЕВОГЕ")
        code = game.invite_code

        u1 = await session_service.get_or_create_user(50002)
        u2 = await session_service.get_or_create_user(50003)
        u3 = await session_service.get_or_create_user(50004)
        u4 = await session_service.get_or_create_user(50005)

        variations = [
            code.lower(),
            code.upper(),
            f"  {code.upper()}  ",
            f"\n{code.lower()}\t",
        ]
        users = [u1, u2, u3, u4]

        tasks = [
            session_service.join_session(invite_code=var, agent_tg_id=u.id)
            for var, u in zip(variations, users)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        successes = [r for r in results if not isinstance(r, Exception)]
        assert len(successes) == 1, "Normalization must ensure lock serialization across casings"


# ==========================================
# 2. Parallel Guess Spam & Mutation Tests
# ==========================================

@pytest.mark.database
class TestAdversarialGuessSpam:
    """Adversarial stress tests for concurrent guess submissions."""

    async def test_full_alphabet_parallel_guess_spam(
        self,
        session_service: SessionService,
    ):
        """Agent fires guesses for ALL unique letters simultaneously.

        Verifies:
        - Zero lost updates
        - Final state has all letters revealed
        - Exactly one guess triggers victory transition
        - Statistics (games_won) incremented exactly once
        """
        analyst = await session_service.get_or_create_user(telegram_id=60001)
        agent = await session_service.get_or_create_user(telegram_id=60002)

        # Russian pangram containing 10 unique letters
        text = "СЪЕШЬ ЖЕ ЕЩЕ ЭТИХ МЯГКИХ"
        game = await session_service.create_session(creator_tg_id=analyst.id, text=text)
        await session_service.join_session(game.invite_code, agent.id)

        # Inverted mapping: code -> letter
        code_to_char = game.code_to_char_map
        all_codes = list(code_to_char.keys())

        # Submit all guesses concurrently
        tasks = [
            session_service.submit_guess(
                session_id_or_code=game.id,
                user_tg_id=agent.id,
                symbol_code=code,
                letter=code_to_char[code],
            )
            for code in all_codes
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Because once solved, subsequent coroutines in queue might receive SessionFinishedError
        # or victory/correct results depending on order:
        victories = [
            r for r in results
            if not isinstance(r, Exception) and r.status == GuessStatus.VICTORY
        ]
        corrects = [
            r for r in results
            if not isinstance(r, Exception) and r.status == GuessStatus.CORRECT
        ]
        finished_exceptions = [
            r for r in results
            if isinstance(r, SessionFinishedError)
        ]

        # Total valid outcomes must account for all symbols
        total_handled = len(victories) + len(corrects) + len(finished_exceptions)
        assert total_handled == len(all_codes)
        assert len(victories) == 1, f"Exactly one guess must trigger victory, got {len(victories)}"

        # Verify DB final state
        final_game = await session_service.get_session_by_id(game.id)
        assert final_game.status == SessionStatus.SOLVED
        assert final_game.is_solved is True

        # Verify no double increment of stats
        analyst_db = await session_service.get_or_create_user(analyst.id)
        agent_db = await session_service.get_or_create_user(agent.id)
        assert analyst_db.games_won == 1, "Analyst games_won must be 1"
        assert agent_db.games_won == 1, "Agent games_won must be 1"

    async def test_rapid_duplicate_guess_multi_tap(
        self,
        session_service: SessionService,
    ):
        """Simulate rapid double/multi-tapping by agent: 15 concurrent identical guesses."""
        analyst = await session_service.get_or_create_user(telegram_id=70001)
        agent = await session_service.get_or_create_user(telegram_id=70002)

        game = await session_service.create_session(creator_tg_id=analyst.id, text="РАДИОГРАММА")
        await session_service.join_session(game.invite_code, agent.id)

        target_code = 1
        target_char = game.code_to_char_map[target_code]

        tasks = [
            session_service.submit_guess(
                session_id_or_code=game.id,
                user_tg_id=agent.id,
                symbol_code=target_code,
                letter=target_char,
            )
            for _ in range(15)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        assert all(not isinstance(r, Exception) for r in results)

        corrects = [r for r in results if r.status == GuessStatus.CORRECT]
        already_revealed = [r for r in results if r.status == GuessStatus.ALREADY_REVEALED]

        assert len(corrects) == 1, f"Strictly 1 guess must be CORRECT, got {len(corrects)}"
        assert len(already_revealed) == 14, f"Strictly 14 guesses must be ALREADY_REVEALED, got {len(already_revealed)}"

        # Verify DB state
        reloaded = await session_service.get_session_by_id(game.id)
        assert len(reloaded.guessed_mapping) == 1
        assert reloaded.guessed_mapping[str(target_code)] == target_char

    async def test_concurrent_mixed_correct_wrong_invalid(
        self,
        session_service: SessionService,
    ):
        """Simultaneous barrage of correct, wrong, and out-of-range guesses."""
        analyst = await session_service.get_or_create_user(telegram_id=80001)
        agent = await session_service.get_or_create_user(telegram_id=80002)

        game = await session_service.create_session(creator_tg_id=analyst.id, text="ШТАБНОЙ ОФИЦЕР")
        await session_service.join_session(game.invite_code, agent.id)

        code_to_char = game.code_to_char_map

        # 3 correct guesses on codes 1, 2, 3
        correct_tasks = [
            session_service.submit_guess(game.id, agent.id, c, code_to_char[c])
            for c in [1, 2, 3]
        ]
        # 4 wrong guesses on code 4
        wrong_tasks = [
            session_service.submit_guess(game.id, agent.id, 4, "Ю")
            for _ in range(4)
        ]
        # 3 invalid code guesses (code 999 does not exist)
        invalid_tasks = [
            session_service.submit_guess(game.id, agent.id, 999, "А")
            for _ in range(3)
        ]

        all_tasks = correct_tasks + wrong_tasks + invalid_tasks
        results = await asyncio.gather(*all_tasks, return_exceptions=True)

        assert all(not isinstance(r, Exception) for r in results)

        reloaded = await session_service.get_session_by_id(game.id)
        # All 3 correct codes must be registered
        assert len(reloaded.guessed_mapping) == 3
        for c in [1, 2, 3]:
            assert str(c) in reloaded.guessed_mapping
            assert reloaded.guessed_mapping[str(c)] == code_to_char[c]

        # Mistakes count should reflect all 4 wrong guesses
        assert reloaded.mistakes_count == 4


# ==========================================
# 3. Cross-Role Concurrency & Collision Races
# ==========================================

@pytest.mark.database
class TestAdversarialCrossRoleRaces:
    """Adversarial stress tests for concurrent actions between Analyst and Agent."""

    async def test_collision_race_same_symbol_guess_and_hint(
        self,
        session_service: SessionService,
    ):
        """Agent guesses symbol [1] while Analyst simultaneously reveals symbol [1]!

        Race resolution:
        One action must serialize first. The second action must gracefully recognize
        that the symbol is already revealed (ALREADY_REVEALED or InvalidHintError).
        Zero data corruption.
        """
        analyst = await session_service.get_or_create_user(telegram_id=90001)
        agent = await session_service.get_or_create_user(telegram_id=90002)

        game = await session_service.create_session(creator_tg_id=analyst.id, text="ОПЕРАТИВНЫЙ ПЛАН")
        await session_service.join_session(game.invite_code, agent.id)

        target_code = 1
        target_char = game.code_to_char_map[target_code]

        # Agent submits guess for code 1, Analyst reveals code 1 simultaneously
        results = await asyncio.gather(
            session_service.submit_guess(game.id, agent.id, target_code, target_char),
            session_service.give_hint(game.id, analyst.id, symbol_code=target_code),
            return_exceptions=True,
        )

        guess_res, hint_res = results[0], results[1]

        # Case A: Guess won race, Hint followed
        if not isinstance(guess_res, Exception) and guess_res.status == GuessStatus.CORRECT:
            assert isinstance(hint_res, InvalidHintError), (
                f"Hint on already guessed symbol must raise InvalidHintError, got {hint_res}"
            )
        # Case B: Hint won race, Guess followed
        elif not isinstance(hint_res, Exception) and hint_res.status == "HINT_APPLIED":
            assert not isinstance(guess_res, Exception)
            assert guess_res.status == GuessStatus.ALREADY_REVEALED
        else:
            pytest.fail(f"Unexpected race outcome: guess={guess_res}, hint={hint_res}")

        # In both cases, symbol 1 is revealed exactly once
        reloaded = await session_service.get_session_by_id(game.id)
        assert len(reloaded.guessed_mapping) == 1
        assert reloaded.guessed_mapping[str(target_code)] == target_char

    async def test_victory_race_agent_guess_vs_analyst_hint(
        self,
        session_service: SessionService,
    ):
        """Text has 2 unrevealed symbols remaining.
        Agent guesses one, Analyst reveals the other simultaneously, completing the game.
        """
        analyst = await session_service.get_or_create_user(telegram_id=100001)
        agent = await session_service.get_or_create_user(telegram_id=100002)

        game = await session_service.create_session(creator_tg_id=analyst.id, text="ДАВАЙ ПОБЕДУ")
        await session_service.join_session(game.invite_code, agent.id)

        code_to_char = game.code_to_char_map
        all_codes = list(code_to_char.keys())

        # Pre-solve all except the last 2 symbols
        for code in all_codes[:-2]:
            await session_service.submit_guess(game.id, agent.id, code, code_to_char[code])

        code_a = all_codes[-2]
        code_b = all_codes[-1]

        # Race: Agent guesses code_a, Analyst reveals code_b simultaneously
        results = await asyncio.gather(
            session_service.submit_guess(game.id, agent.id, code_a, code_to_char[code_a]),
            session_service.give_hint(game.id, analyst.id, symbol_code=code_b),
            return_exceptions=True,
        )

        assert all(not isinstance(r, Exception) for r in results), f"Errors in race: {results}"

        reloaded = await session_service.get_session_by_id(game.id)
        assert reloaded.status == SessionStatus.SOLVED
        assert reloaded.is_solved is True

        # Exactly 1 game won for each player
        analyst_db = await session_service.get_or_create_user(analyst.id)
        agent_db = await session_service.get_or_create_user(agent.id)
        assert analyst_db.games_won == 1
        assert agent_db.games_won == 1

    async def test_barrage_mixed_50_concurrent_operations(
        self,
        session_service: SessionService,
    ):
        """50 concurrent operations across Analyst and Agent on a single game session:
        - 10 distinct correct guesses
        - 10 duplicate guesses
        - 10 wrong guesses
        - 5 frequency hints
        - 15 SOS requests from agent
        """
        analyst = await session_service.get_or_create_user(telegram_id=110001)
        agent = await session_service.get_or_create_user(telegram_id=110002)

        text = "БЫСТРЫЙ КОРИЧНЕВЫЙ ЛИС ПРЫГАЕТ ЧЕРЕЗ ЛЕНИВОГО ПСА"
        game = await session_service.create_session(creator_tg_id=analyst.id, text=text)
        await session_service.join_session(game.invite_code, agent.id)

        code_to_char = game.code_to_char_map
        unique_codes = list(code_to_char.keys())

        tasks = []

        # 10 correct guesses
        for c in unique_codes[:10]:
            tasks.append(session_service.submit_guess(game.id, agent.id, c, code_to_char[c]))

        # 10 duplicate guesses (on code 1)
        for _ in range(10):
            tasks.append(session_service.submit_guess(game.id, agent.id, unique_codes[0], code_to_char[unique_codes[0]]))

        # 10 wrong guesses
        for _ in range(10):
            tasks.append(session_service.submit_guess(game.id, agent.id, unique_codes[11], "Я" if code_to_char[unique_codes[11]] != "Я" else "Ю"))

        # 5 frequency report hints
        for _ in range(5):
            tasks.append(session_service.give_hint(game.id, analyst.id, hint_type=HintType.FREQUENCY_REPORT))

        # 15 SOS requests
        for i in range(15):
            tasks.append(session_service.request_help(game.id, agent.id, message=f"SOS {i}"))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        exceptions = [r for r in results if isinstance(r, Exception)]
        assert len(exceptions) == 0, f"Expected zero unhandled exceptions, got {exceptions}"

        # Verify final state integrity
        reloaded = await session_service.get_session_by_id(game.id)
        assert len(reloaded.guessed_mapping) >= 10
        assert reloaded.mistakes_count == 10
        assert reloaded.hints_used == 5

        # Check hint logs count (5 frequency hints + 15 SOS requests = 20 logs)
        assert len(reloaded.hints) == 20


# ==========================================
# 4. Lock Safety & Resource Lifecycle Tests
# ==========================================

@pytest.mark.database
class TestAdversarialLockSafety:
    """Stress tests for SessionLockManager invariants."""

    async def test_session_id_vs_invite_code_lock_symmetry(
        self,
        session_service: SessionService,
    ):
        """Verify concurrent operations using session.id vs session.invite_code both serialize cleanly.

        VULNERABILITY DEMONSTRATION:
        Because lock key is computed as f"session:{str(session_id_or_code).strip().lower()}",
        caller using session.id (e.g. 1) and caller using session.invite_code (e.g. "e681a796")
        acquire DIFFERENT locks! This allows concurrent un-synchronized execution against the same
        row in SQLite, resulting in lost updates!
        """
        analyst = await session_service.get_or_create_user(telegram_id=120001)
        agent = await session_service.get_or_create_user(telegram_id=120002)

        game = await session_service.create_session(creator_tg_id=analyst.id, text="ПРОВЕРКА СИММЕТРИИ БЛОКИРОВОК")
        await session_service.join_session(game.invite_code, agent.id)

        c2c = game.code_to_char_map

        # Caller 1 uses integer ID: game.id -> lock_key = "session:1"
        # Caller 2 uses string invite_code: game.invite_code -> lock_key = "session:<hex>"
        # Caller 3 uses string ID: str(game.id) -> lock_key = "session:1"
        results = await asyncio.gather(
            session_service.submit_guess(game.id, agent.id, 1, c2c[1]),
            session_service.submit_guess(game.invite_code, agent.id, 2, c2c[2]),
            session_service.submit_guess(str(game.id), agent.id, 3, c2c[3]),
            return_exceptions=True,
        )

        assert all(not isinstance(r, Exception) for r in results), f"Errors: {results}"

        reloaded = await session_service.get_session_by_id(game.id)
        assert len(reloaded.guessed_mapping) == 3, (
            f"Expected 3 revealed symbols without lost updates, got {reloaded.guessed_mapping}"
        )

    async def test_homogeneous_invite_code_concurrency(
        self,
        session_service: SessionService,
    ):
        """Verify that when ALL callers consistently use game.invite_code, locking functions properly."""
        analyst = await session_service.get_or_create_user(telegram_id=125001)
        agent = await session_service.get_or_create_user(telegram_id=125002)

        game = await session_service.create_session(creator_tg_id=analyst.id, text="ОДНОРОДНЫЙ КОД")
        await session_service.join_session(game.invite_code, agent.id)

        c2c = game.code_to_char_map
        code = game.invite_code

        # All 3 callers use invite_code
        results = await asyncio.gather(
            session_service.submit_guess(code, agent.id, 1, c2c[1]),
            session_service.submit_guess(code, agent.id, 2, c2c[2]),
            session_service.submit_guess(code, agent.id, 3, c2c[3]),
            return_exceptions=True,
        )

        assert all(not isinstance(r, Exception) for r in results), f"Errors: {results}"

        reloaded = await session_service.get_session_by_id(game.id)
        assert len(reloaded.guessed_mapping) == 3

    async def test_homogeneous_session_id_concurrency(
        self,
        session_service: SessionService,
    ):
        """Verify that when ALL callers consistently use game.id, locking functions properly."""
        analyst = await session_service.get_or_create_user(telegram_id=126001)
        agent = await session_service.get_or_create_user(telegram_id=126002)

        game = await session_service.create_session(creator_tg_id=analyst.id, text="ОДНОРОДНЫЙ АЙДИ")
        await session_service.join_session(game.invite_code, agent.id)

        c2c = game.code_to_char_map
        gid = game.id

        # All 3 callers use game.id
        results = await asyncio.gather(
            session_service.submit_guess(gid, agent.id, 1, c2c[1]),
            session_service.submit_guess(gid, agent.id, 2, c2c[2]),
            session_service.submit_guess(gid, agent.id, 3, c2c[3]),
            return_exceptions=True,
        )

        assert all(not isinstance(r, Exception) for r in results), f"Errors: {results}"

        reloaded = await session_service.get_session_by_id(game.id)
        assert len(reloaded.guessed_mapping) == 3

    async def test_lock_manager_prunes_active_locks_to_zero(
        self,
        session_service: SessionService,
        lock_manager: SessionLockManager,
    ):
        """Verify reference counting completely prunes all locks after operations complete (no memory leaks)."""
        analyst = await session_service.get_or_create_user(telegram_id=130001)
        agent = await session_service.get_or_create_user(telegram_id=130002)

        game = await session_service.create_session(creator_tg_id=analyst.id, text="ТЕСТ УТЕЧКИ ПАМЯТИ")
        await session_service.join_session(game.invite_code, agent.id)

        # Execute 30 concurrent guesses
        tasks = [
            session_service.submit_guess(game.id, agent.id, 1, "А")
            for _ in range(30)
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

        # After all coroutines have finished, active locks count MUST be 0
        assert lock_manager.get_active_count() == 0, (
            f"Expected 0 active locks after completion, found {lock_manager.get_active_count()}"
        )

    async def test_lock_manager_timeout_and_exception_resilience(
        self,
        lock_manager: SessionLockManager,
    ):
        """Verify that timeout and exceptions do not corrupt lock state or leak ref counts."""
        session_key = "test_resilience_100"

        # 1. Acquire lock and hold it
        hold_event = asyncio.Event()
        acquired_event = asyncio.Event()

        async def holder():
            async with lock_manager.lock(session_key):
                acquired_event.set()
                await hold_event.wait()

        holder_task = asyncio.create_task(holder())
        await acquired_event.wait()

        assert lock_manager.is_locked(session_key) is True
        assert lock_manager.get_active_count() == 1

        # 2. Competitor attempts to acquire with short timeout -> should raise TimeoutError
        with pytest.raises(TimeoutError):
            async with lock_manager.lock(session_key, timeout=0.05):
                pytest.fail("Should not acquire held lock")

        # After timeout, lock is still held by holder, active count is still 1
        assert lock_manager.is_locked(session_key) is True
        assert lock_manager.get_active_count() == 1

        # 3. Release holder
        hold_event.set()
        await holder_task

        # Now lock should be completely pruned
        assert lock_manager.is_locked(session_key) is False
        assert lock_manager.get_active_count() == 0

        # 4. Exception inside critical section must decrement ref_count and release lock
        with pytest.raises(RuntimeError, match="Crash inside critical section"):
            async with lock_manager.lock(session_key):
                assert lock_manager.is_locked(session_key) is True
                raise RuntimeError("Crash inside critical section")

        assert lock_manager.is_locked(session_key) is False
        assert lock_manager.get_active_count() == 0

    async def test_multi_session_independent_locks(
        self,
        session_service: SessionService,
    ):
        """Operations on different sessions must not block each other."""
        analyst1 = await session_service.get_or_create_user(telegram_id=140001)
        agent1 = await session_service.get_or_create_user(telegram_id=140002)
        game1 = await session_service.create_session(creator_tg_id=analyst1.id, text="ПЕРВАЯ ИГРА")
        await session_service.join_session(game1.invite_code, agent1.id)

        analyst2 = await session_service.get_or_create_user(telegram_id=140003)
        agent2 = await session_service.get_or_create_user(telegram_id=140004)
        game2 = await session_service.create_session(creator_tg_id=analyst2.id, text="ВТОРАЯ ИГРА")
        await session_service.join_session(game2.invite_code, agent2.id)

        c1 = game1.code_to_char_map
        c2 = game2.code_to_char_map

        # Concurrently perform actions across both games
        results = await asyncio.gather(
            session_service.submit_guess(game1.id, agent1.id, 1, c1[1]),
            session_service.submit_guess(game2.id, agent2.id, 1, c2[1]),
            session_service.give_hint(game1.id, analyst1.id, hint_type=HintType.FREQUENCY_REPORT),
            session_service.give_hint(game2.id, analyst2.id, hint_type=HintType.FREQUENCY_REPORT),
            return_exceptions=True,
        )

        assert all(not isinstance(r, Exception) for r in results), f"Errors: {results}"

        g1 = await session_service.get_session_by_id(game1.id)
        g2 = await session_service.get_session_by_id(game2.id)
        assert len(g1.guessed_mapping) == 1
        assert len(g2.guessed_mapping) == 1
        assert g1.hints_used == 1
        assert g2.hints_used == 1

