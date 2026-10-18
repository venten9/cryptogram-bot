"""Adversarial persistence, transaction integrity, and SQLite WAL stress test suite for M2.

Stress-tests:
1. SQLite WAL mode and connection tuning pragmas on file-backed disk engines.
2. Cross-process persistence & true process restart survival (via subprocess).
3. 64-bit Telegram user ID boundaries across process boundaries (up to 2^63 - 1).
4. Transaction rollback integrity on unhandled exceptions, FK violations, and unique constraint collisions.
5. Atomic rollback ensuring zero partial state leaks.
6. Large text dispatches (10,000+ characters) and multi-KB HintLog radio depeches in SQLite.
7. Concurrent reader snapshot isolation during uncommitted writer transactions in WAL mode.
"""
import asyncio
import os
import subprocess
import sys
import time
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

from cryptogram.engine import (
    RUSSIAN_ALPHABET,
    calculate_frequencies,
    check_victory,
    generate_cipher,
    normalize_text,
    render_mask,
    validate_text,
)
from cryptogram.models import GuessStatus
from database.models import Base, GameSession, HintLog, HintType, SessionStatus, User
from database.session import (
    close_db,
    create_session_factory,
    create_sqlite_async_engine,
    get_db_session,
    init_db,
)
from services.lock_manager import SessionLockManager
from services.session_service import (
    CreatorCannotJoinAsAgent,
    SelfJoinError,
    SessionNotFoundError,
    SessionService,
    UnauthorizedActionError,
)


# =====================================================================
# Fixtures
# =====================================================================

@pytest_asyncio.fixture(scope="function")
async def file_db_path(tmp_path):
    """Path to an isolated file-backed SQLite database."""
    return tmp_path / "adversarial_test.db"


@pytest_asyncio.fixture(scope="function")
async def file_engine(file_db_path) -> AsyncGenerator[AsyncEngine, None]:
    """File-backed async engine with WAL mode and pragmas configured."""
    engine = create_sqlite_async_engine(f"sqlite+aiosqlite:///{file_db_path}")
    await init_db(engine)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def session_factory(file_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Session factory bound to file-backed engine."""
    return create_session_factory(file_engine)


@pytest_asyncio.fixture(scope="function")
async def session_service(session_factory) -> SessionService:
    """Configured SessionService with isolated in-memory lock manager."""
    return SessionService(session_factory=session_factory, lock_manager=SessionLockManager())


# =====================================================================
# 1. SQLite WAL Mode and Pragmas Verification
# =====================================================================

@pytest.mark.database
class TestSqliteWalAndPragmas:
    """Verify that SQLite file engines strictly configure and maintain WAL mode and pragmas."""

    async def test_wal_mode_and_pragmas_on_disk(self, file_engine: AsyncEngine):
        """Verify PRAGMA journal_mode=WAL, synchronous=NORMAL, foreign_keys=ON, busy_timeout=5000."""
        async with file_engine.connect() as conn:
            jm = await conn.execute(text("PRAGMA journal_mode;"))
            assert jm.scalar().lower() == "wal", "SQLite engine must be in WAL mode on disk."

            sync = await conn.execute(text("PRAGMA synchronous;"))
            # 1 == NORMAL in SQLite
            assert sync.scalar() == 1, "SQLite synchronous pragma must be NORMAL (1)."

            fk = await conn.execute(text("PRAGMA foreign_keys;"))
            assert fk.scalar() == 1, "Foreign key constraints must be actively enforced."

            bt = await conn.execute(text("PRAGMA busy_timeout;"))
            assert bt.scalar() >= 5000, "busy_timeout must be at least 5000ms."

            cs = await conn.execute(text("PRAGMA cache_size;"))
            assert cs.scalar() == -64000, "cache_size must be -64000 (64MB)."

    async def test_wal_checkpoint_and_integrity_check(self, file_engine: AsyncEngine, session_service: SessionService):
        """Verify that writing data under WAL mode and checkpointing maintains 100% database integrity."""
        # Write some data
        user = await session_service.get_or_create_user(
            telegram_id=888123,
            username="wal_analyst",
            first_name="Аналитик ВАЛ",
        )
        game = await session_service.create_session(
            creator_tg_id=user.id,
            text="СЕКРЕТНЫЙ РАПОРТ ШТАБА",
        )
        assert game.invite_code is not None

        # Execute WAL checkpoint and integrity check
        async with file_engine.connect() as conn:
            checkpoint_res = await conn.execute(text("PRAGMA wal_checkpoint(PASSIVE);"))
            row = checkpoint_res.fetchone()
            assert row is not None
            # row[0] is checkpoint status: 0 indicates success

            integrity_res = await conn.execute(text("PRAGMA integrity_check;"))
            assert integrity_res.scalar().lower() == "ok", "Database file integrity check must be OK."

    async def test_concurrent_reader_unblocked_by_writer(self, file_db_path):
        """In WAL mode, an uncommitted writer must not block concurrent readers (snapshot isolation)."""
        engine_writer = create_sqlite_async_engine(f"sqlite+aiosqlite:///{file_db_path}")
        await init_db(engine_writer)
        engine_reader = create_sqlite_async_engine(f"sqlite+aiosqlite:///{file_db_path}")

        sm_writer = create_session_factory(engine_writer)
        sm_reader = create_session_factory(engine_reader)

        # Baseline: insert one user
        async with sm_writer() as sess:
            u0 = User(id=100, username="baseline_user", first_name="Базовый")
            sess.add(u0)
            await sess.commit()

        # Step 1: Open writer session and add a new row WITHOUT committing
        async with sm_writer() as write_sess:
            u_temp = User(id=200, username="uncommitted_user", first_name="Временный")
            write_sess.add(u_temp)
            await write_sess.flush()  # flushed to DB buffer but transaction NOT committed

            # Step 2: Concurrently query from reader session
            # Under WAL, this read MUST NOT hang and MUST NOT raise database locked error
            async with sm_reader() as read_sess:
                # Reader should see baseline user
                res_u0 = await read_sess.scalar(select(User).where(User.id == 100))
                assert res_u0 is not None
                assert res_u0.username == "baseline_user"

                # Reader MUST NOT see uncommitted user (Transaction Isolation)
                res_u_temp = await read_sess.scalar(select(User).where(User.id == 200))
                assert res_u_temp is None, "Snapshot isolation: uncommitted writes must not be visible to readers."

            # Step 3: Writer rolls back
            await write_sess.rollback()

        # Step 4: Verify reader still sees clean state
        async with sm_reader() as read_sess:
            res_after_rollback = await read_sess.scalar(select(User).where(User.id == 200))
            assert res_after_rollback is None, "Rolled back write must leave no trace in database."

        await engine_writer.dispose()
        await engine_reader.dispose()


# =====================================================================
# 2. Cross-Process Persistence & True Process Restart
# =====================================================================

@pytest.mark.database
class TestCrossProcessPersistence:
    """Verify true process restart persistence using independent Python subprocesses."""

    def test_cross_process_lifecycle_and_restart(self, tmp_path):
        """Simulate separate OS processes writing to and reading from the file-backed SQLite database."""
        db_file = tmp_path / "proc_restart.db"
        project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

        # Script 1: Initial process creates DB, adds 64-bit users, creates session, agent joins, and exits
        worker_writer_code = f"""
import asyncio
import sys
sys.path.insert(0, {repr(project_root)})

from database.session import create_sqlite_async_engine, create_session_factory, init_db, close_db
from services.session_service import SessionService
from database.models import User, GameSession, HintType

async def run():
    engine = create_sqlite_async_engine("sqlite+aiosqlite:///" + {repr(str(db_file).replace('\\\\', '/'))})
    await init_db(engine)
    sm = create_session_factory(engine)
    svc = SessionService(session_factory=sm)

    # 64-bit Telegram user IDs
    analyst_id = 9123456789012345
    agent_id = 9123456789012346

    analyst = await svc.get_or_create_user(analyst_id, username="hq_proc1", first_name="Штаб-1")
    agent = await svc.get_or_create_user(agent_id, username="field_proc1", first_name="Агент-1")

    game = await svc.create_session(
        creator_tg_id=analyst_id,
        text="ПОЕЗД ОТПРАВЛЯЕТСЯ В ПОЛНОЧЬ ПОСЛЕ ДОЖДЯ",
        is_custom=True,
    )

    joined = await svc.join_session(game.invite_code, agent_id)

    # Agent makes 1 guess for symbol 1
    code_map = joined.code_to_char_map
    guess_res = await svc.submit_guess(joined.id, agent_id, 1, code_map[1])
    assert guess_res.status.value in ("CORRECT", "VICTORY")

    # Analyst gives 1 hint for symbol 2
    hint_res = await svc.give_hint(joined.id, analyst_id, symbol_code=2)
    assert hint_res.status == "HINT_APPLIED"

    # Agent requests SOS help
    await svc.request_help(joined.id, agent_id, symbol_code=3, message="Штаб, прием, требуется содействие!")

    await close_db(engine)
    print(f"SUCCESS:{{game.invite_code}}:{{joined.id}}")

asyncio.run(run())
"""
        writer_script = tmp_path / "writer_proc.py"
        writer_script.write_text(worker_writer_code, encoding="utf-8")

        # Execute writer process
        res1 = subprocess.run(
            [sys.executable, str(writer_script)],
            capture_output=True,
            text=True,
            cwd=project_root,
        )
        assert res1.returncode == 0, f"Writer process failed:\nSTDOUT: {res1.stdout}\nSTDERR: {res1.stderr}"
        assert "SUCCESS:" in res1.stdout
        invite_code, session_id_str = res1.stdout.strip().split("SUCCESS:")[1].split(":")
        session_id = int(session_id_str)

        # Script 2: Separate second process opens the database, inspects state, finishes game, and exits
        worker_reader_code = f"""
import asyncio
import sys
sys.path.insert(0, {repr(project_root)})

from database.session import create_sqlite_async_engine, create_session_factory, close_db
from services.session_service import SessionService
from database.models import User, GameSession, SessionStatus, HintType

async def run():
    engine = create_sqlite_async_engine("sqlite+aiosqlite:///" + {repr(str(db_file).replace('\\\\', '/'))})
    sm = create_session_factory(engine)
    svc = SessionService(session_factory=sm)

    analyst_id = 9123456789012345
    agent_id = 9123456789012346

    # Verify users persisted with exact 64-bit IDs and games_played count
    analyst = await svc.get_or_create_user(analyst_id)
    agent = await svc.get_or_create_user(agent_id)
    assert analyst.games_played == 1
    assert agent.games_played == 1

    # Fetch game session
    game = await svc.get_session_by_code({repr(invite_code)})
    assert game is not None
    assert game.id == {session_id}
    assert game.status == SessionStatus.IN_PROGRESS
    assert game.original_text == "ПОЕЗД ОТПРАВЛЯЕТСЯ В ПОЛНОЧЬ ПОСЛЕ ДОЖДЯ"
    assert "1" in game.guessed_mapping
    assert "2" in game.guessed_mapping

    # Verify hint logs
    assert len(game.hints) == 2
    types = {{h.hint_type for h in game.hints}}
    assert HintType.REVEAL_LETTER in types
    assert HintType.AGENT_SOS in types

    # Finish the game by guessing all remaining symbols
    code_map = game.code_to_char_map
    for sym_code, char in sorted(code_map.items()):
        if str(sym_code) not in game.guessed_mapping:
            res = await svc.submit_guess(game.id, agent_id, sym_code, char)
            game = res.game

    assert game.status == SessionStatus.SOLVED
    assert game.solved_at is not None

    # Reload users and verify games_won updated
    analyst_after = await svc.get_or_create_user(analyst_id)
    agent_after = await svc.get_or_create_user(agent_id)
    assert analyst_after.games_won == 1
    assert agent_after.games_won == 1
    assert analyst_after.win_rate == 100.0

    await close_db(engine)
    print("FINISH_SUCCESS")

asyncio.run(run())
"""
        reader_script = tmp_path / "reader_proc.py"
        reader_script.write_text(worker_reader_code, encoding="utf-8")

        res2 = subprocess.run(
            [sys.executable, str(reader_script)],
            capture_output=True,
            text=True,
            cwd=project_root,
        )
        assert res2.returncode == 0, f"Reader process failed:\nSTDOUT: {res2.stdout}\nSTDERR: {res2.stderr}"
        assert "FINISH_SUCCESS" in res2.stdout

    async def test_extreme_64bit_boundary_telegram_ids(
        self,
        file_db_path,
        file_engine: AsyncEngine,
        session_service: SessionService,
    ):
        """Verify boundary 64-bit Telegram IDs (e.g. 2^31-1, 2^31, 2^53-1, 2^63-1) persist without corruption."""
        boundary_ids = [
            2147483647,             # 2^31 - 1 (Max 32-bit signed)
            2147483648,             # 2^31 (Exceeds 32-bit signed)
            9007199254740991,       # 2^53 - 1 (Max safe JS integer)
            9223372036854775807,   # 2^63 - 1 (Max 64-bit signed integer)
        ]

        for i, tid in enumerate(boundary_ids):
            user = await session_service.get_or_create_user(
                telegram_id=tid,
                username=f"boundary_user_{i}",
                first_name=f"User {tid}",
            )
            assert user.id == tid
            assert user.telegram_id == tid

        # Dispose engine and re-query on fresh engine
        await file_engine.dispose()
        fresh_engine = create_sqlite_async_engine(f"sqlite+aiosqlite:///{file_db_path}")
        fresh_sm = create_session_factory(fresh_engine)
        fresh_svc = SessionService(session_factory=fresh_sm)

        for i, tid in enumerate(boundary_ids):
            loaded = await fresh_svc.get_or_create_user(telegram_id=tid)
            assert loaded.id == tid, f"Telegram ID {tid} must match exactly without truncation."
            assert loaded.username == f"boundary_user_{i}"

        await fresh_engine.dispose()

    def test_multiprocess_concurrent_contention(self, tmp_path):
        """Verify that concurrent OS processes reading and writing to the SQLite WAL database do not crash."""
        db_file = tmp_path / "concurrent_proc.db"
        project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

        # Setup database
        init_script = tmp_path / "init_proc.py"
        init_script.write_text(f"""
import asyncio, sys
sys.path.insert(0, {repr(project_root)})
from database.session import create_sqlite_async_engine, init_db, close_db

async def run():
    engine = create_sqlite_async_engine("sqlite+aiosqlite:///" + {repr(str(db_file).replace('\\\\', '/'))})
    await init_db(engine)
    await close_db(engine)

asyncio.run(run())
""", encoding="utf-8")
        subprocess.run([sys.executable, str(init_script)], check=True, cwd=project_root)

        # Worker script that creates a game session and submits a guess
        worker_code = f"""
import asyncio, sys, random
sys.path.insert(0, {repr(project_root)})
from database.session import create_sqlite_async_engine, create_session_factory, close_db
from services.session_service import SessionService

async def run(worker_id):
    engine = create_sqlite_async_engine("sqlite+aiosqlite:///" + {repr(str(db_file).replace('\\\\', '/'))})
    sm = create_session_factory(engine)
    svc = SessionService(session_factory=sm)

    analyst_id = 700000 + worker_id
    agent_id = 800000 + worker_id

    for cycle in range(3):
        game = await svc.create_session(creator_tg_id=analyst_id, text=f"РАПОРТ ПОСТА НОМЕР {{worker_id}} ЦИКЛ {{cycle}}")
        joined = await svc.join_session(game.invite_code, agent_id)
        c2c = joined.code_to_char_map
        await svc.submit_guess(joined.id, agent_id, 1, c2c[1])
        await asyncio.sleep(0.01)

    await close_db(engine)
    print(f"WORKER_{{worker_id}}_DONE")

if __name__ == "__main__":
    w_id = int(sys.argv[1])
    asyncio.run(run(w_id))
"""
        worker_script = tmp_path / "worker.py"
        worker_script.write_text(worker_code, encoding="utf-8")

        # Spawn 3 concurrent processes
        procs = [
            subprocess.Popen([sys.executable, str(worker_script), str(i)], cwd=project_root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for i in range(3)
        ]

        for p in procs:
            stdout, stderr = p.communicate(timeout=30)
            assert p.returncode == 0, f"Concurrent worker failed:\nSTDOUT: {stdout}\nSTDERR: {stderr}"



# =====================================================================
# 3. Transaction Rollback Integrity
# =====================================================================

@pytest.mark.database
class TestTransactionRollbackIntegrity:
    """Adversarially verify that exceptions inside transactions execute 100% clean rollbacks."""

    async def test_rollback_on_explicit_exception_in_get_db_session(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ):
        """Verify that an exception raised inside get_db_session rolls back all dirty and uncommitted entities."""
        with pytest.raises(RuntimeError, match="Uncaught exception during business transaction"):
            async with get_db_session(session_factory) as session:
                user = User(
                    id=777001,
                    username="doomed_user",
                    first_name="Обреченный",
                )
                session.add(user)
                # Flush to assign state in session
                await session.flush()
                # Simulate mid-transaction business logic crash
                raise RuntimeError("Uncaught exception during business transaction")

        # Open completely fresh independent session: verify user was NEVER committed
        async with session_factory() as verify_session:
            persisted = await verify_session.scalar(select(User).where(User.id == 777001))
            assert persisted is None, "Rolled-back user MUST NOT exist in database."

    async def test_rollback_on_foreign_key_violation(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ):
        """Verify that a foreign key violation aborts cleanly without corrupting the session or engine."""
        with pytest.raises(IntegrityError):
            async with get_db_session(session_factory) as session:
                # GameSession referencing non-existent analyst_id
                bad_game = GameSession(
                    invite_code="badfk001",
                    analyst_id=999999999999,  # Non-existent FK
                    original_text="ТЕКСТ",
                    normalized_text="ТЕКСТ",
                    cipher_mapping={"Т": 1, "Е": 2, "К": 3, "С": 4},
                )
                session.add(bad_game)
                await session.commit()

        # Verify that subsequent operations on the session factory work smoothly
        async with session_factory() as session:
            count = await session.scalar(select(func.count()).select_from(GameSession).where(GameSession.invite_code == "badfk001"))
            assert count == 0, "No invalid foreign key row should exist."

    async def test_rollback_on_unique_constraint_violation(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ):
        """Verify unique constraint violations roll back cleanly and do not corrupt existing rows."""
        # Insert initial valid user
        async with get_db_session(session_factory) as session:
            u1 = User(id=555001, username="first_unique", first_name="Первый")
            session.add(u1)

        # Attempt inserting conflicting primary key
        with pytest.raises(IntegrityError):
            async with get_db_session(session_factory) as session:
                u2 = User(id=555001, username="second_conflicting", first_name="Второй")
                session.add(u2)

        # Verify initial valid user is intact and unharmed
        async with session_factory() as session:
            u_check = await session.scalar(select(User).where(User.id == 555001))
            assert u_check is not None
            assert u_check.username == "first_unique", "Existing user must retain original attributes."

    async def test_atomic_multi_statement_rollback(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ):
        """Verify that multi-item transactions roll back atomically (all or nothing)."""
        with pytest.raises(IntegrityError):
            async with get_db_session(session_factory) as session:
                u1 = User(id=601, username="u601", first_name="User 601")
                u2 = User(id=602, username="u602", first_name="User 602")
                u3_conflict = User(id=601, username="u601_dup", first_name="Duplicate")
                session.add_all([u1, u2, u3_conflict])

        # Verify NEITHER u1 NOR u2 was committed
        async with session_factory() as session:
            r1 = await session.scalar(select(User).where(User.id == 601))
            r2 = await session.scalar(select(User).where(User.id == 602))
            assert r1 is None, "Atomic rollback: entity 1 must not be committed."
            assert r2 is None, "Atomic rollback: entity 2 must not be committed."

    async def test_service_level_clean_state_on_self_join_error(
        self,
        session_service: SessionService,
        session_factory: async_sessionmaker[AsyncSession],
    ):
        """Verify that domain validation errors in SessionService leave database state 100% clean."""
        creator_id = 888999
        game = await session_service.create_session(
            creator_tg_id=creator_id,
            text="ОПЕРАЦИЯ БУРЯ В ПУСТЫНЕ",
        )
        assert game.status == SessionStatus.WAITING_AGENT
        assert game.agent_id is None

        # Creator attempts to join as agent -> SelfJoinError
        with pytest.raises(SelfJoinError):
            await session_service.join_session(game.invite_code, creator_id)

        # Inspect DB state
        async with session_factory() as session:
            reloaded = await session.scalar(select(GameSession).where(GameSession.id == game.id))
            assert reloaded.status == SessionStatus.WAITING_AGENT
            assert reloaded.agent_id is None
            assert reloaded.analyst.games_played == 0, "games_played must not be incremented on failed join."

    async def test_rollback_mid_guess_leaves_db_clean(
        self,
        session_service: SessionService,
        session_factory: async_sessionmaker[AsyncSession],
        monkeypatch,
    ):
        """Simulate an unexpected error during guess processing (e.g. before commit) and verify clean rollback."""
        game = await session_service.create_session(creator_tg_id=7100, text="ШИФРОВКА АЛЕКСА")
        joined = await session_service.join_session(game.invite_code, 7200)

        # Monkeypatch check_victory to simulate a crash during guess processing
        def crashing_check_victory(*args, **kwargs):
            raise RuntimeError("Database engine simulated crash during victory evaluation")

        monkeypatch.setattr("services.session_service.check_victory", crashing_check_victory)

        c2c = joined.code_to_char_map
        with pytest.raises(RuntimeError, match="simulated crash"):
            await session_service.submit_guess(joined.id, 7200, 1, c2c[1])

        # Reload game from a fresh session: verify guess was NOT recorded in database
        async with session_factory() as session:
            reloaded = await session.scalar(select(GameSession).where(GameSession.id == joined.id))
            assert "1" not in reloaded.guessed_mapping, "Rolled back guess must NOT be persisted in database."
            assert reloaded.revealed_count == 0
            assert reloaded.status == SessionStatus.IN_PROGRESS



# =====================================================================
# 4. Large Payload and Multiline Dispatches
# =====================================================================

@pytest.mark.database
class TestLargePayloadAndDispatches:
    """Stress-test large payloads, multiline military dispatches, and large HintLogs in SQLite."""

    async def test_large_dispatch_10k_chars_persistence(
        self,
        file_db_path,
        file_engine: AsyncEngine,
    ):
        """Persist, reload, render, and solve a massive 10,000+ character dispatch with all 33 letters."""
        # Generate 10,000+ characters Russian text
        pangram = (
            "В чащах юга жил-был цитрус? Да, но фальшивый экземпляр! "
            "Съешь же ещё этих мягких французских булок, да выпей горячего чаю. "
            "Широкая электрификация южных губерний даст мощный толчок подъёму сельского хозяйства. "
        )
        repetitions = (10000 // len(pangram)) + 2
        large_text = (pangram * repetitions)[:12000]
        assert len(large_text) >= 10000

        # Create session on engine 1
        sm1 = create_session_factory(file_engine)
        svc1 = SessionService(session_factory=sm1)

        t0 = time.perf_counter()
        game = await svc1.create_session(
            creator_tg_id=4001,
            text=large_text,
            is_custom=True,
        )
        t_create = time.perf_counter() - t0
        assert t_create < 1.0, f"Session creation for 10k chars took too long: {t_create:.3f}s"
        assert len(game.original_text) >= 10000
        assert len(game.cipher_mapping) >= 30
        invite_code = game.invite_code
        game_id = game.id

        # Dispose engine 1 completely
        await file_engine.dispose()

        # Open engine 2 pointing to same file
        engine2 = create_sqlite_async_engine(f"sqlite+aiosqlite:///{file_db_path}")
        sm2 = create_session_factory(engine2)
        svc2 = SessionService(session_factory=sm2)

        # Load session from disk
        loaded = await svc2.get_session_by_code(invite_code)
        assert loaded is not None
        assert loaded.id == game_id
        assert len(loaded.original_text) == len(large_text)
        assert len(loaded.cipher_mapping) == len(game.cipher_mapping)

        # Test mask rendering performance on 10k character payload
        t_mask_0 = time.perf_counter()
        mask = await svc2.render_session_mask(loaded, wrap_pre=True)
        t_mask = time.perf_counter() - t_mask_0
        assert t_mask < 0.2, f"Mask rendering for 10k chars took too long: {t_mask:.3f}s"
        assert "<pre>" in mask and "</pre>" in mask

        # Join as agent and guess one symbol
        await svc2.join_session(invite_code, 4002)
        c2c = loaded.code_to_char_map
        guess_res = await svc2.submit_guess(game_id, 4002, 1, c2c[1])
        assert guess_res.status in (GuessStatus.CORRECT, GuessStatus.VICTORY)

        # Reload and check guessed mapping persisted
        async with sm2() as session:
            reloaded = await session.scalar(select(GameSession).where(GameSession.id == game_id))
            assert "1" in reloaded.guessed_mapping
            assert reloaded.guessed_mapping["1"] == c2c[1]

        await engine2.dispose()

    async def test_multiline_dispatch_with_formatting_and_quotes(
        self,
        session_service: SessionService,
        session_factory: async_sessionmaker[AsyncSession],
    ):
        """Test realistic historical military dispatch with newlines, quotes, brackets, and numbers."""
        dispatch = (
            "=== ВНЕОЧЕРЕДНАЯ РАДИОГРАММА № 482/Б ===\n"
            "КОМУ: ЦЕНТР\n"
            "ОТ: ЮСТАС\n"
            "ДАТА: 17.10.1943 02:45 UTC\n\n"
            "«ВНИМАНИЕ! ЭШЕЛОН С ОБОРУДОВАНИЕМ [ОБЪЕКТ-77] ПРОСЛЕДОВАЛ СТАНЦИЮ \"ТАЙФУН\".\n"
            "КООРДИНАТЫ: 55°45'21\"N 37°37'04\"E.\n"
            "СВЯЗЬ ЧЕРЕЗ РЕЗЕРВНЫЙ КАНАЛ #9. ПРИЕМ.»"
        )

        game = await session_service.create_session(creator_tg_id=5001, text=dispatch)
        joined = await session_service.join_session(game.invite_code, 5002)

        # Solve all symbols
        c2c = joined.code_to_char_map
        for sym_code, char in sorted(c2c.items()):
            res = await session_service.submit_guess(joined.id, 5002, sym_code, char)

        assert res.is_victory is True
        assert res.status == GuessStatus.VICTORY

        # Verify solved state in database
        async with session_factory() as session:
            solved_game = await session.scalar(select(GameSession).where(GameSession.id == joined.id))
            assert solved_game.status == SessionStatus.SOLVED
            assert solved_game.solved_at is not None
            assert solved_game.progress_percentage == 100.0

    async def test_large_hint_logs_and_radio_depeches(
        self,
        session_service: SessionService,
        session_factory: async_sessionmaker[AsyncSession],
    ):
        """Test persisting multiple large (>5KB) tactical radio logs in HintLog without truncation."""
        game = await session_service.create_session(creator_tg_id=6001, text="ШТАБ СЕВЕРНОГО ФЛОТА")
        await session_service.join_session(game.invite_code, 6002)

        # Add 5 large radio depeches
        large_body = "РАПОРТ: " + ("РАДИОГРАММА СВЯЗИ ОПЕРАТИВНОГО ШТАБА. " * 150)
        assert len(large_body) > 5000

        async with session_factory() as session:
            for idx in range(5):
                hint = HintLog(
                    session_id=game.id,
                    sender_id=6001,
                    hint_type=HintType.RADIO_DEPECHE,
                    content=f"Депеша #{idx + 1}: {large_body}",
                    payload=f'{{"transmission_id": {idx + 1}, "length": {len(large_body)}}}',
                )
                session.add(hint)
            await session.commit()

        # Reload and verify all 5 logs survived with full payload
        async with session_factory() as session:
            reloaded_game = await session.scalar(select(GameSession).where(GameSession.id == game.id))
            assert len(reloaded_game.hints) == 5
            for idx, h in enumerate(reloaded_game.hints):
                assert h.hint_type == HintType.RADIO_DEPECHE
                assert len(h.content) > 5000
                assert f"Депеша #{idx + 1}" in h.content
                assert "transmission_id" in h.payload

    async def test_sql_injection_payload_in_text_and_usernames(
        self,
        session_service: SessionService,
        session_factory: async_sessionmaker[AsyncSession],
    ):
        """Verify that SQL injection strings in text and usernames are safely escaped by SQLAlchemy."""
        malicious_username = "admin'; DROP TABLE users; --"
        malicious_text = "СЕКРЕТНЫЙ ПЛАН '; DROP TABLE game_sessions; -- В РАБОТЕ"

        user = await session_service.get_or_create_user(
            telegram_id=999666,
            username=malicious_username,
            first_name="' OR '1'='1",
        )
        game = await session_service.create_session(
            creator_tg_id=user.id,
            text=malicious_text,
            is_custom=True,
        )

        async with session_factory() as session:
            # Tables must exist and contain the records
            u_check = await session.scalar(select(User).where(User.id == 999666))
            assert u_check is not None
            assert u_check.username == malicious_username

            g_check = await session.scalar(select(GameSession).where(GameSession.id == game.id))
            assert g_check is not None
            assert g_check.original_text == malicious_text

            # Check that table count queries work (tables were not dropped)
            u_count = await session.scalar(select(func.count()).select_from(User))
            g_count = await session.scalar(select(func.count()).select_from(GameSession))
            assert u_count >= 1
            assert g_count >= 1

    async def test_unicode_control_and_zero_width_characters(
        self,
        session_service: SessionService,
        session_factory: async_sessionmaker[AsyncSession],
    ):
        """Verify persistence of non-breaking spaces, zero-width spaces, and emojis within Cyrillic dispatches."""
        special_text = "РАПОРТ\u200B:\u00A0ОПЕРАЦИЯ «СЕВЕР» 🛰️ ЗАВЕРШЕНА!"
        game = await session_service.create_session(creator_tg_id=888333, text=special_text)

        async with session_factory() as session:
            reloaded = await session.scalar(select(GameSession).where(GameSession.id == game.id))
            assert reloaded is not None
            assert "\u200B" in reloaded.original_text
            assert "\u00A0" in reloaded.original_text
            assert "🛰️" in reloaded.original_text

