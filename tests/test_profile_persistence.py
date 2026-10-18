"""Comprehensive Persistence, Schema Migration, and Victory Accrual Tests.

Covers:
1. TestUserModelAndRankProperties:
   - Persisted column defaults (exp, games_as_analyst, games_as_agent, total_hints_used, total_mistakes).
   - Domain properties (rank_title, current_rank_badge, next_rank_exp, rank_progress_pct, rank_progress_bar).
   - Rank progression at 0 EXP, intermediate EXP (600 EXP -> Сержант, 60%), and Marshal (35000+ EXP).
   - Defensive handling of None or negative values.
2. TestIdempotentSQLiteMigration:
   - Migration adds missing columns to pre-existing SQLite database files on disk without data loss.
   - Preserves existing rows and applies default 0.
   - Idempotency when running init_db multiple times.
3. TestRoleCounterTrackingOnJoin:
   - join_session increments analyst.games_as_analyst and agent.games_as_agent alongside games_played.
   - Parity invariant: games_as_analyst + games_as_agent == games_played across role swaps.
4. TestVictoryExpAccrualViaGuess:
   - Catalog difficulty scaling (EASY: 100, MEDIUM: 200, HARD: 300).
   - Custom quote default to MEDIUM base EXP (200).
   - Clean Work bonus (+50 if mistakes == 0, else 0).
   - Autonomy bonus (+50 if hints == 0, else 0).
   - Equal dual-player awards for Analyst and Agent.
   - Cumulative total_mistakes and total_hints_used tracking.
5. TestVictoryExpAccrualViaHint:
   - Session solved via Analyst letter reveal hint awards EXP and stats atomically.
   - Correctly negates autonomy bonus (hints_used >= 1).
6. TestCumulativeStatsAcrossMultipleGames:
   - Multi-game progression accumulating EXP and triggering rank advancement.
7. TestEdgeCasesAndAtomicity:
   - Cancelled or unstarted games award 0 EXP.
   - Rejection of post-victory actions preventing duplicate EXP awards.
   - Solo analyst victory edge case handled gracefully.
"""
import asyncio
from datetime import datetime, timezone
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

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
from database.session import (
    close_db,
    create_session_factory,
    create_sqlite_async_engine,
    init_db,
)
from services.lock_manager import SessionLockManager
from services.session_service import (
    SessionFinishedError,
    SessionInvalidStateError,
    SessionNotFoundError,
    SessionService,
)


# ======================================================================
# Test Fixtures
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
async def session_factory(async_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Provides an async sessionmaker bound to test engine."""
    return create_session_factory(async_engine)


@pytest_asyncio.fixture(scope="function")
async def lock_manager() -> SessionLockManager:
    """Isolated session lock manager."""
    return SessionLockManager()


@pytest_asyncio.fixture(scope="function")
async def session_service(
    session_factory: async_sessionmaker[AsyncSession],
    lock_manager: SessionLockManager,
) -> SessionService:
    """SessionService configured with test sessionmaker and lock manager."""
    return SessionService(session_factory=session_factory, lock_manager=lock_manager)


@pytest_asyncio.fixture(scope="function")
async def analyst_user(session_service: SessionService) -> User:
    """Pre-seeded Headquarters Analyst user."""
    return await session_service.get_or_create_user(
        telegram_id=9001,
        username="hq_ivan",
        first_name="Иван",
        last_name="Аналитик",
    )


@pytest_asyncio.fixture(scope="function")
async def agent_user(session_service: SessionService) -> User:
    """Pre-seeded Field Agent user."""
    return await session_service.get_or_create_user(
        telegram_id=9002,
        username="agent_alex",
        first_name="Алексей",
        last_name="Агент",
    )


# ======================================================================
# 1. TestUserModelAndRankProperties
# ======================================================================

@pytest.mark.asyncio
class TestUserModelAndRankProperties:
    async def test_user_model_default_column_values(
        self,
        session_service: SessionService,
        session_factory: async_sessionmaker[AsyncSession],
    ):
        """Newly created User must have all 5 progression columns defaulted to 0."""
        user = await session_service.get_or_create_user(
            telegram_id=11001,
            username="recruit",
            first_name="Новобранец",
        )
        assert user.exp == 0
        assert user.games_as_analyst == 0
        assert user.games_as_agent == 0
        assert user.total_hints_used == 0
        assert user.total_mistakes == 0

        # Reload from fresh DB session to verify SQL schema defaults
        async with session_factory() as session:
            reloaded = await session.scalar(select(User).where(User.id == 11001))
            assert reloaded is not None
            assert reloaded.exp == 0
            assert reloaded.games_as_analyst == 0
            assert reloaded.games_as_agent == 0
            assert reloaded.total_hints_used == 0
            assert reloaded.total_mistakes == 0

    async def test_user_rank_properties_at_zero_exp(self, session_service: SessionService):
        """At 0 EXP, user must be Private (Рядовой) with 0% progress to next rank."""
        user = await session_service.get_or_create_user(telegram_id=11002)
        user.exp = 0

        assert user.rank_title == "Рядовой"
        assert user.current_rank_badge == "🪖"
        assert user.next_rank_exp == 100
        assert user.rank_progress_pct == 0.0
        assert user.rank_progress_bar == "[░░░░░░░░░░] 0%"

    async def test_user_rank_properties_intermediate_spec_case(self, session_service: SessionService):
        """At 600 EXP, user must be Sergeant (Сержант) with 60% progress towards Senior Sergeant."""
        user = await session_service.get_or_create_user(telegram_id=11003)
        user.exp = 600

        assert user.rank_title == "Сержант"
        assert user.current_rank_badge == "🥈"
        assert user.next_rank_exp == 700
        assert user.rank_progress_pct == 60.0
        assert user.rank_progress_bar == "[██████░░░░] 60%"

    async def test_user_rank_properties_marshal_and_above(self, session_service: SessionService):
        """At 35000 and 50000 EXP, user must be Marshal (Маршал) with next_rank_exp=None and 100%."""
        user = await session_service.get_or_create_user(telegram_id=11004)
        user.exp = 35000

        assert user.rank_title == "Маршал"
        assert user.current_rank_badge == "👑⭐"
        assert user.next_rank_exp is None
        assert user.rank_progress_pct == 100.0
        assert user.rank_progress_bar == "[██████████] 100%"

        # Well beyond Marshal cap
        user.exp = 100000
        assert user.rank_title == "Маршал"
        assert user.current_rank_badge == "👑⭐"
        assert user.next_rank_exp is None
        assert user.rank_progress_pct == 100.0
        assert user.rank_progress_bar == "[██████████] 100%"

    async def test_user_rank_properties_none_or_negative_defensive(self):
        """Properties should safely handle None or negative exp without raising exceptions."""
        user = User(id=99999)
        user.exp = None  # type: ignore

        assert user.rank_title == "Рядовой"
        assert user.current_rank_badge == "🪖"
        assert user.next_rank_exp == 100
        assert user.rank_progress_pct == 0.0
        assert user.rank_progress_bar == "[░░░░░░░░░░] 0%"

        user.exp = -50
        assert user.rank_title == "Рядовой"
        assert user.rank_progress_pct == 0.0


# ======================================================================
# 2. TestIdempotentSQLiteMigration
# ======================================================================

@pytest.mark.asyncio
class TestIdempotentSQLiteMigration:
    async def test_migration_adds_missing_columns_to_existing_database(self, tmp_path):
        """Simulates an older SQLite DB lacking the 5 new columns; init_db must migrate safely."""
        db_file = tmp_path / "legacy_v1.db"
        legacy_engine = create_sqlite_async_engine(f"sqlite+aiosqlite:///{db_file}", echo=False)

        # 1. Create legacy table structure with raw SQL (only old columns)
        async with legacy_engine.begin() as conn:
            await conn.exec_driver_sql(
                """
                CREATE TABLE users (
                    id BIGINT PRIMARY KEY,
                    username VARCHAR(64),
                    first_name VARCHAR(255) NOT NULL,
                    last_name VARCHAR(255),
                    games_played INTEGER NOT NULL DEFAULT 0,
                    games_won INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
                );
                """
            )
            # Insert a legacy user with historical statistics
            await conn.exec_driver_sql(
                """
                INSERT INTO users (id, username, first_name, last_name, games_played, games_won)
                VALUES (123456, 'old_soldier', 'Константин', 'Рокоссовский', 15, 12);
                """
            )

        # 2. Run init_db on the legacy database
        await init_db(legacy_engine)

        # 3. Verify all 5 new columns were added
        async with legacy_engine.connect() as conn:
            result = await conn.exec_driver_sql("PRAGMA table_info(users);")
            col_info = {row[1]: row for row in result.fetchall()}
            assert "exp" in col_info
            assert "games_as_analyst" in col_info
            assert "games_as_agent" in col_info
            assert "total_hints_used" in col_info
            assert "total_mistakes" in col_info

        # 4. Verify existing record data was preserved and new columns defaulted to 0
        factory = create_session_factory(legacy_engine)
        async with factory() as session:
            migrated_user = await session.scalar(select(User).where(User.id == 123456))
            assert migrated_user is not None
            assert migrated_user.username == "old_soldier"
            assert migrated_user.first_name == "Константин"
            assert migrated_user.games_played == 15
            assert migrated_user.games_won == 12
            assert migrated_user.exp == 0
            assert migrated_user.games_as_analyst == 0
            assert migrated_user.games_as_agent == 0
            assert migrated_user.total_hints_used == 0
            assert migrated_user.total_mistakes == 0
            assert migrated_user.rank_title == "Рядовой"

        # 5. Verify idempotency: calling init_db again causes no error
        await init_db(legacy_engine)
        await close_db(legacy_engine)


# ======================================================================
# 3. TestRoleCounterTrackingOnJoin
# ======================================================================

@pytest.mark.asyncio
class TestRoleCounterTrackingOnJoin:
    async def test_join_session_increments_role_counters(
        self,
        session_service: SessionService,
        analyst_user: User,
        agent_user: User,
    ):
        """When an agent joins, analyst.games_as_analyst and agent.games_as_agent must increment."""
        # Baseline check
        assert analyst_user.games_played == 0
        assert analyst_user.games_as_analyst == 0
        assert analyst_user.games_as_agent == 0
        assert agent_user.games_played == 0
        assert agent_user.games_as_analyst == 0
        assert agent_user.games_as_agent == 0

        # Game 1: analyst_user hosts, agent_user joins
        g1 = await session_service.create_session(creator_tg_id=analyst_user.id, text="РАДИОГРАММА")
        await session_service.join_session(invite_code=g1.invite_code, agent_tg_id=agent_user.id)

        u1 = await session_service.get_or_create_user(analyst_user.id)
        u2 = await session_service.get_or_create_user(agent_user.id)

        assert u1.games_played == 1
        assert u1.games_as_analyst == 1
        assert u1.games_as_agent == 0

        assert u2.games_played == 1
        assert u2.games_as_analyst == 0
        assert u2.games_as_agent == 1

        # Game 2: Reverse roles! agent_user hosts, analyst_user joins
        g2 = await session_service.create_session(creator_tg_id=agent_user.id, text="ДЕПЕША ЦЕНТРА")
        await session_service.join_session(invite_code=g2.invite_code, agent_tg_id=analyst_user.id)

        u1_rev = await session_service.get_or_create_user(analyst_user.id)
        u2_rev = await session_service.get_or_create_user(agent_user.id)

        assert u1_rev.games_played == 2
        assert u1_rev.games_as_analyst == 1
        assert u1_rev.games_as_agent == 1
        assert u1_rev.games_as_analyst + u1_rev.games_as_agent == u1_rev.games_played

        assert u2_rev.games_played == 2
        assert u2_rev.games_as_analyst == 1
        assert u2_rev.games_as_agent == 1
        assert u2_rev.games_as_analyst + u2_rev.games_as_agent == u2_rev.games_played


# ======================================================================
# 4. TestVictoryExpAccrualViaGuess
# ======================================================================

@pytest.mark.asyncio
class TestVictoryExpAccrualViaGuess:
    async def test_perfect_game_easy_catalog_quote(
        self,
        session_service: SessionService,
        analyst_user: User,
        agent_user: User,
    ):
        """Victory on EASY catalog quote with 0 mistakes and 0 hints awards 100+50+50 = 200 EXP."""
        # gagarin_poekhali is an EASY catalog quote
        game = await session_service.create_session(
            creator_tg_id=analyst_user.id,
            text="ПОЕХАЛИ",
            catalog_id="gagarin_poekhali",
        )
        joined = await session_service.join_session(
            invite_code=game.invite_code,
            agent_tg_id=agent_user.id,
        )

        code_to_char = joined.code_to_char_map
        for code in range(1, len(code_to_char) + 1):
            res = await session_service.submit_guess(
                session_id_or_code=joined.id,
                user_tg_id=agent_user.id,
                symbol_code=code,
                letter=code_to_char[code],
            )

        assert res.status == GuessStatus.VICTORY
        assert res.is_victory is True

        # Verify participants received exact EXP awards
        u_analyst = await session_service.get_or_create_user(analyst_user.id)
        u_agent = await session_service.get_or_create_user(agent_user.id)

        assert u_analyst.games_won == 1
        assert u_analyst.exp == 200  # 100 base + 50 clean + 50 autonomy
        assert u_analyst.total_hints_used == 0
        assert u_analyst.total_mistakes == 0

        assert u_agent.games_won == 1
        assert u_agent.exp == 200
        assert u_agent.total_hints_used == 0
        assert u_agent.total_mistakes == 0

    async def test_victory_with_mistakes_forfeits_clean_work_bonus(
        self,
        session_service: SessionService,
        analyst_user: User,
        agent_user: User,
    ):
        """Victory on MEDIUM catalog quote with 2 mistakes and 0 hints awards 200+0+50 = 250 EXP."""
        game = await session_service.create_session(
            creator_tg_id=analyst_user.id,
            text="ШТОРМ",
            catalog_id="sorge_tokyo_1941",  # MEDIUM
        )
        joined = await session_service.join_session(
            invite_code=game.invite_code,
            agent_tg_id=agent_user.id,
        )

        code_to_char = joined.code_to_char_map
        first_code = 1
        correct_first = code_to_char[first_code]
        wrong_char = "Я" if correct_first != "Я" else "Ю"

        # Make 2 mistakes
        await session_service.submit_guess(joined.id, agent_user.id, first_code, wrong_char)
        await session_service.submit_guess(joined.id, agent_user.id, first_code, wrong_char)

        # Solve remainder
        for code in range(1, len(code_to_char) + 1):
            await session_service.submit_guess(joined.id, agent_user.id, code, code_to_char[code])

        u_analyst = await session_service.get_or_create_user(analyst_user.id)
        u_agent = await session_service.get_or_create_user(agent_user.id)

        assert u_analyst.exp == 250  # 200 base + 0 clean + 50 autonomy
        assert u_analyst.total_mistakes == 2
        assert u_analyst.total_hints_used == 0

        assert u_agent.exp == 250
        assert u_agent.total_mistakes == 2
        assert u_agent.total_hints_used == 0

    async def test_victory_with_hints_and_mistakes_on_hard_quote(
        self,
        session_service: SessionService,
        analyst_user: User,
        agent_user: User,
    ):
        """Victory on HARD quote with 1 hint and 1 mistake awards 300+0+0 = 300 EXP."""
        game = await session_service.create_session(
            creator_tg_id=analyst_user.id,
            text="ШИФРОВКА",
            catalog_id="kuznetsov_order_one",  # HARD
        )
        joined = await session_service.join_session(
            invite_code=game.invite_code,
            agent_tg_id=agent_user.id,
        )

        code_to_char = joined.code_to_char_map

        # 1 mistake
        wrong_letter = "Ж" if code_to_char[1] != "Ж" else "З"
        await session_service.submit_guess(joined.id, agent_user.id, 1, wrong_letter)

        # 1 hint from Analyst
        await session_service.give_hint(
            session_id_or_code=joined.id,
            user_tg_id=analyst_user.id,
            hint_type=HintType.FREQUENCY_REPORT,
        )

        # Solve all symbols
        for code in range(1, len(code_to_char) + 1):
            await session_service.submit_guess(joined.id, agent_user.id, code, code_to_char[code])

        u_analyst = await session_service.get_or_create_user(analyst_user.id)
        u_agent = await session_service.get_or_create_user(agent_user.id)

        assert u_analyst.exp == 300  # 300 base + 0 clean + 0 autonomy
        assert u_analyst.total_mistakes == 1
        assert u_analyst.total_hints_used == 1

        assert u_agent.exp == 300
        assert u_agent.total_mistakes == 1
        assert u_agent.total_hints_used == 1

    async def test_custom_quote_defaults_to_medium_base_exp(
        self,
        session_service: SessionService,
        analyst_user: User,
        agent_user: User,
    ):
        """Custom quotes default to MEDIUM base EXP (200 EXP). With 0/0 bonuses -> 300 EXP."""
        game = await session_service.create_session(
            creator_tg_id=analyst_user.id,
            text="СВОЙ ТЕКСТ",
            is_custom=True,
        )
        joined = await session_service.join_session(
            invite_code=game.invite_code,
            agent_tg_id=agent_user.id,
        )

        code_to_char = joined.code_to_char_map
        for code in range(1, len(code_to_char) + 1):
            await session_service.submit_guess(joined.id, agent_user.id, code, code_to_char[code])

        u_analyst = await session_service.get_or_create_user(analyst_user.id)
        u_agent = await session_service.get_or_create_user(agent_user.id)

        assert u_analyst.exp == 300  # 200 base + 50 clean + 50 autonomy
        assert u_agent.exp == 300


# ======================================================================
# 5. TestVictoryExpAccrualViaHint
# ======================================================================

@pytest.mark.asyncio
class TestVictoryExpAccrualViaHint:
    async def test_victory_triggered_by_analyst_reveal_hint(
        self,
        session_service: SessionService,
        analyst_user: User,
        agent_user: User,
    ):
        """When Analyst reveals the final symbol uncovering the text, session reaches SOLVED with EXP."""
        game = await session_service.create_session(
            creator_tg_id=analyst_user.id,
            text="СЕКРЕТ",
            catalog_id="gagarin_poekhali",  # EASY -> base 100
        )
        joined = await session_service.join_session(
            invite_code=game.invite_code,
            agent_tg_id=agent_user.id,
        )

        code_to_char = joined.code_to_char_map
        total_symbols = len(code_to_char)

        # Agent solves all symbols EXCEPT the very last one
        for code in range(1, total_symbols):
            await session_service.submit_guess(joined.id, agent_user.id, code, code_to_char[code])

        reloaded_mid = await session_service.get_session_by_id(joined.id)
        assert reloaded_mid.status == SessionStatus.IN_PROGRESS

        # Analyst discloses the final symbol via REVEAL_LETTER hint!
        last_code = total_symbols
        hint_res = await session_service.give_hint(
            session_id_or_code=joined.id,
            user_tg_id=analyst_user.id,
            hint_type=HintType.REVEAL_LETTER,
            symbol_code=last_code,
        )
        assert hint_res.status == "HINT_APPLIED"

        # Game must now be SOLVED!
        final_game = await session_service.get_session_by_id(joined.id)
        assert final_game.status == SessionStatus.SOLVED
        assert final_game.is_solved is True
        assert final_game.solved_at is not None
        assert final_game.hints_used == 1

        # Check EXP: Base EASY (100) + Clean Work (50) + Autonomy (0 because 1 hint used) = 150 EXP
        u_analyst = await session_service.get_or_create_user(analyst_user.id)
        u_agent = await session_service.get_or_create_user(agent_user.id)

        assert u_analyst.games_won == 1
        assert u_analyst.exp == 150
        assert u_analyst.total_hints_used == 1
        assert u_analyst.total_mistakes == 0

        assert u_agent.games_won == 1
        assert u_agent.exp == 150
        assert u_agent.total_hints_used == 1
        assert u_agent.total_mistakes == 0


# ======================================================================
# 6. TestCumulativeStatsAcrossMultipleGames
# ======================================================================

@pytest.mark.asyncio
class TestCumulativeStatsAcrossMultipleGames:
    async def test_stats_persist_and_accumulate_across_sequential_games(
        self,
        session_service: SessionService,
        analyst_user: User,
        agent_user: User,
    ):
        """EXP and stats monotonically accumulate across multiple games, promoting player rank."""
        # Initial: Рядовой (0 EXP)
        assert analyst_user.exp == 0
        assert analyst_user.rank_title == "Рядовой"

        # Match 1: EASY, 0 hints, 0 mistakes -> +200 EXP
        g1 = await session_service.create_session(analyst_user.id, text="ДЕПЕША", catalog_id="gagarin_poekhali")
        j1 = await session_service.join_session(g1.invite_code, agent_user.id)
        for code, char in j1.code_to_char_map.items():
            await session_service.submit_guess(j1.id, agent_user.id, code, char)

        u1 = await session_service.get_or_create_user(analyst_user.id)
        assert u1.exp == 200
        # 200 EXP is between 100 (Ефрейтор) and 250 (Младший сержант) -> Ефрейтор!
        assert u1.rank_title == "Ефрейтор"
        assert u1.current_rank_badge == "🎗️"

        # Match 2: MEDIUM, 1 hint, 0 mistakes -> 200 base + 50 clean + 0 autonomy = 250 EXP
        # Total EXP becomes 200 + 250 = 450 EXP -> exact threshold for Сержант!
        g2 = await session_service.create_session(analyst_user.id, text="СВЯЗЬ", catalog_id="sorge_tokyo_1941")
        j2 = await session_service.join_session(g2.invite_code, agent_user.id)
        # Consume 1 hint
        await session_service.give_hint(j2.id, analyst_user.id, HintType.FREQUENCY_REPORT)
        for code, char in j2.code_to_char_map.items():
            await session_service.submit_guess(j2.id, agent_user.id, code, char)

        u2 = await session_service.get_or_create_user(analyst_user.id)
        assert u2.exp == 450
        assert u2.games_played == 2
        assert u2.games_won == 2
        assert u2.games_as_analyst == 2
        assert u2.total_hints_used == 1
        assert u2.total_mistakes == 0
        assert u2.rank_title == "Сержант"
        assert u2.current_rank_badge == "🥈"
        assert u2.next_rank_exp == 700


# ======================================================================
# 7. TestEdgeCasesAndAtomicity
# ======================================================================

@pytest.mark.asyncio
class TestEdgeCasesAndAtomicity:
    async def test_cancelled_session_awards_no_exp(
        self,
        session_service: SessionService,
        analyst_user: User,
        agent_user: User,
    ):
        """Cancelling a session does not award EXP or modify total_hints_used/total_mistakes."""
        game = await session_service.create_session(analyst_user.id, text="ОТМЕНА")
        await session_service.cancel_session(game.id, analyst_user.id)

        u_analyst = await session_service.get_or_create_user(analyst_user.id)
        assert u_analyst.exp == 0
        assert u_analyst.games_won == 0
        assert u_analyst.total_mistakes == 0

    async def test_post_victory_guess_rejected_no_duplicate_exp(
        self,
        session_service: SessionService,
        analyst_user: User,
        agent_user: User,
    ):
        """Submitting a guess on an already SOLVED game is rejected and cannot award duplicate EXP."""
        game = await session_service.create_session(analyst_user.id, text="КОНЕЦ", catalog_id="gagarin_poekhali")
        joined = await session_service.join_session(game.invite_code, agent_user.id)

        # Solve game
        for code, char in joined.code_to_char_map.items():
            await session_service.submit_guess(joined.id, agent_user.id, code, char)

        u_initial = await session_service.get_or_create_user(analyst_user.id)
        initial_exp = u_initial.exp

        # Attempt post-victory guess
        with pytest.raises(SessionFinishedError):
            await session_service.submit_guess(joined.id, agent_user.id, 1, "А")

        u_after = await session_service.get_or_create_user(analyst_user.id)
        assert u_after.exp == initial_exp
        assert u_after.games_won == 1

    async def test_process_victory_with_solo_analyst_graceful(
        self,
        session_service: SessionService,
        analyst_user: User,
        session_factory: async_sessionmaker[AsyncSession],
    ):
        """Direct call to _process_session_victory without an agent gracefully awards EXP to analyst."""
        game = await session_service.create_session(analyst_user.id, text="ОДИН")
        async with session_factory() as session:
            db_game = await session.scalar(select(GameSession).where(GameSession.id == game.id))
            assert db_game.agent_id is None

            exp_result = await session_service._process_session_victory(db_game, session)
            assert exp_result.total_exp > 0
            await session.commit()

        u = await session_service.get_or_create_user(analyst_user.id)
        assert u.exp == exp_result.total_exp
        assert u.games_won == 1
