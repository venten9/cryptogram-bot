"""Tier 3 Automated Async Persistence & Session Service Test Suite.

Comprehensive async pytest suite covering:
1. TestDatabaseInitialization: schema creation, WAL pragmas, FK enforcement, idempotency.
2. TestUserManagement: creation, upsert, unique constraint, 64-bit Telegram IDs, win rate.
3. TestSessionLifecycle: complete playthrough, guess evaluation, hints, help SOS, victory.
4. TestInvalidJoinsAndRejections: own game join, already active, bad code, role authorization.
5. TestConcurrencyAndGather: atomic join race, concurrent guesses, guess+hint mutex, duplicate guesses.
6. TestPersistenceIntegrity: JSON mapping recovery, selectin relations, cascade delete, file persistence across restart.
"""
import asyncio
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from cryptogram.models import GuessStatus
from database.models import Base, GameSession, HintLog, HintType, SessionStatus, User
from database.session import (
    close_db,
    create_session_factory,
    create_sqlite_async_engine,
    init_db,
)
from services.lock_manager import SessionLockManager
from services.session_service import (
    CreatorCannotJoinAsAgent,
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
# Async Fixtures
# ==========================================

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
async def session_factory(async_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Provides an async sessionmaker bound to the test engine."""
    return create_session_factory(async_engine)


@pytest_asyncio.fixture(scope="function")
async def db_session(session_factory: async_sessionmaker[AsyncSession]) -> AsyncGenerator[AsyncSession, None]:
    """Provides an isolated AsyncSession per test."""
    async with session_factory() as session:
        yield session
        await session.rollback()


@pytest_asyncio.fixture(scope="function")
async def temp_file_engine(tmp_path) -> AsyncGenerator[AsyncEngine, None]:
    """File-backed SQLite async engine for testing WAL mode and persistence across restarts."""
    db_file = tmp_path / "test_wal_persistence.db"
    engine = create_sqlite_async_engine(
        f"sqlite+aiosqlite:///{db_file}",
        echo=False,
    )
    yield engine
    await engine.dispose()


@pytest.fixture(scope="function")
def lock_manager() -> SessionLockManager:
    """Provides an isolated in-memory lock manager for concurrency tests."""
    return SessionLockManager()


@pytest_asyncio.fixture(scope="function")
async def session_service(
    session_factory: async_sessionmaker[AsyncSession],
    lock_manager: SessionLockManager,
) -> SessionService:
    """Provides SessionService pre-configured with test DB sessionmaker and lock manager."""
    return SessionService(session_factory=session_factory, lock_manager=lock_manager)


@pytest_asyncio.fixture(scope="function")
async def analyst_user(session_service: SessionService) -> User:
    """Pre-seeded Headquarters Analyst user."""
    return await session_service.get_or_create_user(
        telegram_id=1001,
        username="hq_analyst",
        first_name="Штабной Аналитик",
    )


@pytest_asyncio.fixture(scope="function")
async def agent_user(session_service: SessionService) -> User:
    """Pre-seeded Field Agent user."""
    return await session_service.get_or_create_user(
        telegram_id=1002,
        username="field_agent",
        first_name="Полевой Агент",
    )


@pytest_asyncio.fixture(scope="function")
async def active_game_session(
    session_service: SessionService,
    analyst_user: User,
    agent_user: User,
) -> GameSession:
    """Active game session in IN_PROGRESS state with both players joined."""
    game = await session_service.create_session(
        creator_tg_id=analyst_user.id,
        text="СЕКРЕТНЫЙ ШТАБ",
    )
    joined = await session_service.join_session(
        invite_code=game.invite_code,
        agent_tg_id=agent_user.id,
    )
    return joined


# ==========================================
# 1. TestDatabaseInitialization
# ==========================================

@pytest.mark.database
class TestDatabaseInitialization:
    async def test_init_db_creates_all_tables(self, async_engine: AsyncEngine):
        async with async_engine.connect() as conn:
            result = await conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('users', 'game_sessions', 'hint_logs')")
            )
            tables = [row[0] for row in result.fetchall()]
            assert "users" in tables
            assert "game_sessions" in tables
            assert "hint_logs" in tables

    async def test_init_db_idempotency(self, async_engine: AsyncEngine):
        await init_db(async_engine)
        await init_db(async_engine)

    async def test_sqlite_wal_pragma_on_file_engine(self, temp_file_engine: AsyncEngine):
        await init_db(temp_file_engine)
        async with temp_file_engine.connect() as conn:
            res = await conn.execute(text("PRAGMA journal_mode;"))
            mode = res.scalar()
            assert mode.lower() == "wal"

    async def test_foreign_key_constraints_enforced(self, session_factory):
        async with session_factory() as session:
            orphan_session = GameSession(
                invite_code="bad_orph",
                analyst_id=999999999,  # Non-existent User
                original_text="ТЕСТ",
                normalized_text="ТЕСТ",
                cipher_mapping={"Т": 1, "Е": 2, "С": 3},
                guessed_mapping={},
            )
            session.add(orphan_session)
            with pytest.raises(IntegrityError):
                await session.commit()


# ==========================================
# 2. TestUserManagement
# ==========================================

@pytest.mark.database
class TestUserManagement:
    async def test_get_or_create_user_lifecycle(self, session_service: SessionService):
        u1 = await session_service.get_or_create_user(
            telegram_id=5001,
            username="analyst_test",
            first_name="Аналитик",
        )
        assert u1.id > 0
        assert u1.telegram_id == 5001
        assert u1.games_played == 0
        assert u1.games_won == 0

        # Idempotent fetch with profile update
        u1_again = await session_service.get_or_create_user(
            telegram_id=5001,
            username="analyst_updated",
            first_name="Аналитик Обновленный",
        )
        assert u1_again.id == u1.id
        assert u1_again.username == "analyst_updated"
        assert u1_again.first_name == "Аналитик Обновленный"

    async def test_64bit_telegram_id(self, session_service: SessionService):
        big_tg_id = 9_999_888_777
        user = await session_service.get_or_create_user(telegram_id=big_tg_id)
        assert user.telegram_id == big_tg_id
        assert user.id == big_tg_id

    async def test_user_win_rate_and_display_name(self, session_service: SessionService, session_factory):
        user = await session_service.get_or_create_user(
            telegram_id=7001,
            username="ivan_agent",
            first_name="Иван",
            last_name="Петров",
        )
        assert user.display_name == "Иван Петров"
        assert user.win_rate == 0.0

        async with session_factory() as session:
            db_u = await session.scalar(select(User).where(User.id == 7001))
            db_u.games_played = 10
            db_u.games_won = 7
            await session.commit()
            await session.refresh(db_u)
            assert db_u.win_rate == 70.0

    async def test_telegram_id_unique_constraint(self, session_service: SessionService, session_factory):
        await session_service.get_or_create_user(telegram_id=8001, username="first")
        async with session_factory() as session:
            duplicate = User(id=8001, username="second")
            session.add(duplicate)
            with pytest.raises(IntegrityError):
                await session.commit()


# ==========================================
# 3. TestSessionLifecycle
# ==========================================

@pytest.mark.database
class TestSessionLifecycle:
    async def test_full_playthrough(
        self,
        session_service: SessionService,
        analyst_user: User,
        agent_user: User,
    ):
        # 1. Create Session
        game = await session_service.create_session(
            creator_tg_id=analyst_user.id,
            text="ШТАБ ЦЕНТР",
        )
        assert game.status == SessionStatus.WAITING_AGENT
        assert len(game.invite_code) == 8
        assert game.analyst_id == analyst_user.id
        assert game.agent_id is None
        assert game.total_unique_letters == len(game.cipher_mapping)

        # 2. Join Session
        joined = await session_service.join_session(
            invite_code=game.invite_code,
            agent_tg_id=agent_user.id,
        )
        assert joined.status == SessionStatus.IN_PROGRESS
        assert joined.agent_id == agent_user.id

        # Mapping inspect
        code_to_char = joined.code_to_char_map
        char_to_code = joined.cipher_mapping

        # 3. Wrong Guess
        first_code = 1
        expected_first_char = code_to_char[first_code]
        wrong_letter = "Ю" if expected_first_char != "Ю" else "Я"

        res_wrong = await session_service.submit_guess(
            session_id_or_code=joined.id,
            user_tg_id=agent_user.id,
            symbol_code=first_code,
            letter=wrong_letter,
        )
        assert res_wrong.status == GuessStatus.WRONG
        assert res_wrong["status"] == "WRONG"
        assert res_wrong["solved"] is False
        assert "Неверно" in res_wrong.message

        # 4. Correct Guess
        res_ok = await session_service.submit_guess(
            session_id_or_code=joined.id,
            user_tg_id=agent_user.id,
            symbol_code=first_code,
            letter=expected_first_char,
        )
        assert res_ok.status == GuessStatus.CORRECT
        assert res_ok["status"] == "CORRECT"
        assert res_ok["solved"] is False
        assert "Верно" in res_ok.message

        # 5. Duplicate Guess on already revealed symbol
        res_dup = await session_service.submit_guess(
            session_id_or_code=joined.id,
            user_tg_id=agent_user.id,
            symbol_code=first_code,
            letter=expected_first_char,
        )
        assert res_dup.status == GuessStatus.ALREADY_REVEALED
        assert res_dup["status"] == "ALREADY_REVEALED"

        # 6. Hint from Analyst
        second_code = 2
        res_hint = await session_service.give_hint(
            session_id_or_code=joined.id,
            user_tg_id=analyst_user.id,
            symbol_code=second_code,
        )
        assert res_hint["status"] == "HINT_APPLIED"
        assert res_hint.symbol_code == second_code
        assert res_hint.letter == code_to_char[second_code]

        # 7. Solve remainder of cryptogram
        for code in range(1, len(char_to_code) + 1):
            reloaded_game = await session_service.get_session_by_id(joined.id)
            if str(code) not in reloaded_game.guessed_mapping:
                res = await session_service.submit_guess(
                    session_id_or_code=joined.id,
                    user_tg_id=agent_user.id,
                    symbol_code=code,
                    letter=code_to_char[code],
                )

        assert res["solved"] is True
        assert res.status == GuessStatus.VICTORY

        # 8. Check final DB state and player stats
        final_game = await session_service.get_session_by_id(joined.id)
        assert final_game.status == SessionStatus.SOLVED
        assert final_game.is_solved is True
        assert final_game.progress_percentage == 100.0
        assert final_game.solved_at is not None

        analyst_refreshed = await session_service.get_or_create_user(analyst_user.id)
        agent_refreshed = await session_service.get_or_create_user(agent_user.id)
        assert analyst_refreshed.games_won >= 1
        assert agent_refreshed.games_won >= 1

    async def test_request_help_logs_sos(
        self,
        session_service: SessionService,
        active_game_session: GameSession,
        agent_user: User,
    ):
        hint = await session_service.request_help(
            session_id_or_code=active_game_session.id,
            user_tg_id=agent_user.id,
            message="Штаб, застрял на символе [3]!",
        )
        assert hint.hint_type == HintType.AGENT_SOS
        assert hint.sender_id == agent_user.id
        assert hint.session_id == active_game_session.id
        assert "Штаб, застрял" in hint.content

    async def test_frequency_report_hint(
        self,
        session_service: SessionService,
        active_game_session: GameSession,
        analyst_user: User,
    ):
        hint = await session_service.give_hint(
            session_id_or_code=active_game_session.id,
            user_tg_id=analyst_user.id,
            hint_type=HintType.FREQUENCY_REPORT,
        )
        assert hint.hint_type == HintType.FREQUENCY_REPORT
        assert "Частотный анализ" in hint.content

    async def test_symbol_keypad_and_render_mask(
        self,
        session_service: SessionService,
        active_game_session: GameSession,
    ):
        mask = await session_service.render_session_mask(active_game_session)
        assert "<pre>" in mask
        assert "[1]" in mask
        sym_map = active_game_session.symbol_to_revealed_char
        assert isinstance(sym_map, dict)


# ==========================================
# 4. TestInvalidJoinsAndRejections
# ==========================================

@pytest.mark.database
class TestInvalidJoinsAndRejections:
    async def test_creator_cannot_join_own_game(
        self,
        session_service: SessionService,
        analyst_user: User,
    ):
        game = await session_service.create_session(analyst_user.id, "ТЕСТ")
        with pytest.raises((SelfJoinError, ValueError), match="CREATOR_CANNOT_JOIN"):
            await session_service.join_session(game.invite_code, analyst_user.id)

    async def test_join_nonexistent_game(
        self,
        session_service: SessionService,
        agent_user: User,
    ):
        with pytest.raises((SessionNotFoundError, ValueError), match="SESSION_NOT_FOUND"):
            await session_service.join_session("badcode1", agent_user.id)

    async def test_join_already_full_game(
        self,
        session_service: SessionService,
        analyst_user: User,
        agent_user: User,
    ):
        game = await session_service.create_session(analyst_user.id, "ТЕСТ")
        await session_service.join_session(game.invite_code, agent_user.id)

        u3 = await session_service.get_or_create_user(1003, "intruder")
        with pytest.raises((SessionAlreadyStartedError, ValueError), match="SESSION_ALREADY_TAKEN"):
            await session_service.join_session(game.invite_code, u3.id)

    async def test_join_solved_game_rejected(
        self,
        session_service: SessionService,
        active_game_session: GameSession,
    ):
        # Force solve
        active_game_session.status = SessionStatus.SOLVED
        async with session_service.session_factory() as session:
            await session.merge(active_game_session)
            await session.commit()

        u4 = await session_service.get_or_create_user(1004, "late_joiner")
        with pytest.raises((SessionFinishedError, ValueError), match="SESSION_INACTIVE"):
            await session_service.join_session(active_game_session.invite_code, u4.id)

    async def test_unauthorized_user_cannot_guess(
        self,
        session_service: SessionService,
        active_game_session: GameSession,
    ):
        u3 = await session_service.get_or_create_user(1003, "intruder")
        with pytest.raises(UnauthorizedActionError, match="UNAUTHORIZED_ACTION"):
            await session_service.submit_guess(
                session_id_or_code=active_game_session.id,
                user_tg_id=u3.id,
                symbol_code=1,
                letter="А",
            )

    async def test_analyst_cannot_submit_guess(
        self,
        session_service: SessionService,
        active_game_session: GameSession,
        analyst_user: User,
    ):
        with pytest.raises(UnauthorizedActionError, match="UNAUTHORIZED_ACTION"):
            await session_service.submit_guess(
                session_id_or_code=active_game_session.id,
                user_tg_id=analyst_user.id,
                symbol_code=1,
                letter="А",
            )

    async def test_agent_cannot_give_hint(
        self,
        session_service: SessionService,
        active_game_session: GameSession,
        agent_user: User,
    ):
        with pytest.raises(UnauthorizedActionError, match="UNAUTHORIZED_ACTION"):
            await session_service.give_hint(
                session_id_or_code=active_game_session.id,
                user_tg_id=agent_user.id,
                symbol_code=1,
            )

    async def test_guess_on_unstarted_session_rejected(
        self,
        session_service: SessionService,
        analyst_user: User,
        agent_user: User,
    ):
        unstarted = await session_service.create_session(analyst_user.id, "ТЕСТ ПРОВЕРКА")
        with pytest.raises(SessionInvalidStateError, match="INVALID_SESSION_STATE"):
            await session_service.submit_guess(
                session_id_or_code=unstarted.id,
                user_tg_id=agent_user.id,
                symbol_code=1,
                letter="Т",
            )


# ==========================================
# 5. TestConcurrencyAndGather
# ==========================================

@pytest.mark.database
class TestConcurrencyAndGather:
    async def test_concurrent_join_atomic(
        self,
        session_service: SessionService,
        analyst_user: User,
    ):
        game = await session_service.create_session(analyst_user.id, "ОПЕРАЦИЯ")
        u2 = await session_service.get_or_create_user(2002, "agent_fast")
        u3 = await session_service.get_or_create_user(2003, "agent_slow")

        results = await asyncio.gather(
            session_service.join_session(game.invite_code, u2.id),
            session_service.join_session(game.invite_code, u3.id),
            return_exceptions=True,
        )

        successes = [r for r in results if not isinstance(r, Exception)]
        failures = [r for r in results if isinstance(r, Exception)]
        assert len(successes) == 1, "Exactly one agent must successfully join"
        assert len(failures) == 1, "The competing agent must be rejected"
        assert isinstance(failures[0], (SessionAlreadyStartedError, ValueError))

    async def test_concurrent_guesses_no_lost_updates(
        self,
        session_service: SessionService,
        active_game_session: GameSession,
        agent_user: User,
    ):
        c2c = active_game_session.code_to_char_map
        results = await asyncio.gather(
            session_service.submit_guess(active_game_session.id, agent_user.id, 1, c2c[1]),
            session_service.submit_guess(active_game_session.id, agent_user.id, 2, c2c[2]),
            session_service.submit_guess(active_game_session.id, agent_user.id, 3, c2c[3]),
        )
        assert all(r.status == GuessStatus.CORRECT for r in results)

        game = await session_service.get_session_by_id(active_game_session.id)
        assert len(game.guessed_mapping) == 3

    async def test_concurrent_guess_and_hint(
        self,
        session_service: SessionService,
        active_game_session: GameSession,
        analyst_user: User,
        agent_user: User,
    ):
        c2c = active_game_session.code_to_char_map
        results = await asyncio.gather(
            session_service.submit_guess(active_game_session.id, agent_user.id, 1, c2c[1]),
            session_service.give_hint(active_game_session.id, analyst_user.id, symbol_code=2),
        )
        game = await session_service.get_session_by_id(active_game_session.id)
        assert "1" in game.guessed_mapping
        assert "2" in game.guessed_mapping

    async def test_concurrent_duplicate_guesses(
        self,
        session_service: SessionService,
        active_game_session: GameSession,
        agent_user: User,
    ):
        c2c = active_game_session.code_to_char_map
        results = await asyncio.gather(
            session_service.submit_guess(active_game_session.id, agent_user.id, 1, c2c[1]),
            session_service.submit_guess(active_game_session.id, agent_user.id, 1, c2c[1]),
        )
        statuses = {r.status for r in results}
        assert GuessStatus.CORRECT in statuses
        assert GuessStatus.ALREADY_REVEALED in statuses


# ==========================================
# 6. TestPersistenceIntegrity
# ==========================================

@pytest.mark.database
class TestPersistenceIntegrity:
    async def test_session_recovery_and_cascade(
        self,
        session_service: SessionService,
        session_factory,
        analyst_user: User,
        agent_user: User,
    ):
        game = await session_service.create_session(analyst_user.id, "ПАРОЛЬ ОМЕГА")
        joined = await session_service.join_session(game.invite_code, agent_user.id)
        await session_service.give_hint(joined.id, analyst_user.id, 1)

        # Re-fetch from a fresh independent session (clean identity map)
        async with session_factory() as db:
            reloaded = await db.scalar(select(GameSession).where(GameSession.id == joined.id))
            assert reloaded is not None
            assert reloaded.original_text == "ПАРОЛЬ ОМЕГА"
            assert len(reloaded.cipher_mapping) > 0
            assert "1" in reloaded.guessed_mapping
            assert len(reloaded.hints) == 1
            assert reloaded.analyst.username == analyst_user.username

            # Cascade delete test: deleting GameSession must delete attached HintLogs
            await db.delete(reloaded)
            await db.commit()

            hints_count = await db.scalar(
                select(func.count()).select_from(HintLog).where(HintLog.session_id == joined.id)
            )
            assert hints_count == 0, "HintLogs must be removed upon GameSession deletion"

    async def test_file_persistence_across_engine_reconnect(self, tmp_path):
        db_file = tmp_path / "persist_test.db"
        engine1 = create_sqlite_async_engine(f"sqlite+aiosqlite:///{db_file}")
        await init_db(engine1)
        sm1 = create_session_factory(engine1)
        svc1 = SessionService(session_factory=sm1)

        game = await svc1.create_session(creator_tg_id=9876, text="ХОЛОДНЫЙ ФРОНТ")
        code = game.invite_code
        await close_db(engine1)

        # Open completely new engine pointing to same disk file
        engine2 = create_sqlite_async_engine(f"sqlite+aiosqlite:///{db_file}")
        sm2 = create_session_factory(engine2)
        svc2 = SessionService(session_factory=sm2)

        loaded = await svc2.get_session_by_code(code)
        assert loaded is not None
        assert loaded.original_text == "ХОЛОДНЫЙ ФРОНТ"
        assert len(loaded.cipher_mapping) > 0
        await close_db(engine2)
