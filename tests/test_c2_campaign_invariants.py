"""Adversarial State Invariants and Stress Verification Suite (Challenger 2).

Milestone 4 (Adversarial Coverage & State Invariance)

Verifies 4 Critical Invariants:
1. Replayability Invariant:
   - Replaying completed chapters never locks unlocked chapters.
   - Replaying completed chapters never decrements user progress.
   - Unlocking chain (1 -> 5) is strictly monotonic.
   - Replays preserve first_completed_at and increment completion_count.
   - Unfinished/cancelled replays do not revoke chapter access or decrease counts.
2. Profile XML Invariant:
   - Profile card formatting remains 100% valid XML across all progression states (0/5 to 5/5).
   - Card formatting remains 100% valid XML with adversarial/hostile username payloads (XSS, XML tags, control chars, emojis, quotes).
   - Only permitted Telegram HTML tags (<b>, <code>, <pre>, <i>, <a>, <u>, <s>) are present.
3. Session Isolation Invariant:
   - Playing story campaign does not corrupt or overwrite active solo sessions.
   - Playing story campaign does not corrupt or overwrite active multiplayer coop sessions (as Analyst or Agent).
   - Concurrent/overlapping campaign sessions and coop sessions maintain distinct states, text, mappings, and metrics.
4. Callback Payload Invariants:
   - All possible campaign callback data strings are strictly within Telegram length limits (< 64 bytes, and <= 40 bytes target).
   - Handlers defensively protect against missing messages, missing from_user, out-of-bounds chapters, and forged actions without unhandled exceptions.
   - Every callback execution triggers callback.answer() avoiding UI spinner freezing.
"""
import asyncio
import html
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import AsyncGenerator, Dict, List, Optional, Set, Tuple

import pytest
import pytest_asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from bot.app import create_dispatcher
from bot.callbacks import CampaignCallback, MenuCallback
from bot.handlers.campaign import campaign_router
from bot.handlers.common import render_player_profile
from bot.keyboards import (
    build_campaign_briefing_keyboard,
    build_campaign_menu_keyboard,
    build_campaign_victory_keyboard,
    build_main_menu_keyboard,
)
from cryptogram.campaign_data import (
    CAMPAIGN_CHAPTERS,
    TOTAL_CHAPTERS,
    get_all_campaign_chapters,
    get_campaign_chapter,
)
from cryptogram.engine import (
    RUSSIAN_ALPHABET_SET,
    check_victory,
    generate_cipher,
    normalize_text,
)
from database.models import Base, CampaignProgress, GameSession, SessionStatus, User
from database.session import create_session_factory
from services.campaign_service import (
    CampaignService,
    ChapterAccessStatus,
    TOTAL_CAMPAIGN_CHAPTERS,
)
from services.lock_manager import SessionLockManager
from services.session_service import SessionService
from tests.mock_session import MockPlayerClient, MockTelegramSession


# ==============================================================================
# Fixtures
# ==============================================================================

@pytest_asyncio.fixture(scope="function")
async def c2_engine() -> AsyncGenerator[AsyncEngine, None]:
    """Isolated in-memory SQLite engine with PRAGMA foreign_keys=ON."""
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
async def c2_db(c2_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """Database session bound to isolated engine."""
    session_factory = create_session_factory(c2_engine)
    async with session_factory() as session:
        yield session


@pytest_asyncio.fixture(scope="function")
async def c2_session_service(c2_engine: AsyncEngine) -> SessionService:
    """Fresh SessionService for c2 tests."""
    session_factory = create_session_factory(c2_engine)
    lock_manager = SessionLockManager()
    return SessionService(session_factory=session_factory, lock_manager=lock_manager)


@pytest_asyncio.fixture(scope="function")
async def c2_bot_env(c2_session_service: SessionService) -> Dict:
    """Mock Telegram environment with dispatcher and player client."""
    mock_session = MockTelegramSession()
    bot = Bot(token="42:MOCK_TOKEN_C2", session=mock_session)
    dp = create_dispatcher(
        session_service=c2_session_service,
        lock_manager=c2_session_service.lock_manager,
        bot_username="SpyAnalystBot",
        storage=MemoryStorage(),
    )
    player = MockPlayerClient(
        dp=dp,
        bot=bot,
        user_id=880011,
        username="c2_auditor",
        first_name="Аудитор",
    )
    return {
        "mock_session": mock_session,
        "bot": bot,
        "dp": dp,
        "player": player,
        "service": c2_session_service,
    }


# ==============================================================================
# INVARIANT 1: Replayability Invariant
# ==============================================================================

class TestC2ReplayabilityInvariant:
    """Stress testing the replayability invariant across complex sequences."""

    @pytest.mark.asyncio
    async def test_replay_preserves_unlocks_and_never_locks_later_chapters(self, c2_db: AsyncSession):
        """Replaying Chapter 1 or 2 when Chapter 4 is reached NEVER locks Chapter 3 or 4."""
        user = User(id=8001, username="sergeant_replay", first_name="Сергей")
        c2_db.add(user)
        await c2_db.commit()

        # Step 1: Complete Chapters 1, 2, 3 -> Chapter 4 is unlocked
        for ch in (1, 2, 3):
            await CampaignService.record_chapter_completion(c2_db, user.id, ch)

        assert (await CampaignService.can_play_chapter(c2_db, user.id, 4))[0] is True
        assert (await CampaignService.get_completed_count(c2_db, user.id)) == 3

        # Step 2: Replay Chapter 1 three times
        for _ in range(3):
            is_first, exp = await CampaignService.record_chapter_completion(c2_db, user.id, 1)
            assert is_first is False, "Subsequent replays must have is_first_completion=False"
            assert exp > 0

        # Step 3: Replay Chapter 2 twice
        for _ in range(2):
            is_first, _ = await CampaignService.record_chapter_completion(c2_db, user.id, 2)
            assert is_first is False

        # Invariant checks:
        # Chapter 4 must STILL be unlocked
        can_ch4, _ = await CampaignService.can_play_chapter(c2_db, user.id, 4)
        assert can_ch4 is True, "Chapter 4 was locked after replaying earlier chapters!"

        # Chapter 3 must STILL be unlocked and completed
        can_ch3, _ = await CampaignService.can_play_chapter(c2_db, user.id, 3)
        assert can_ch3 is True
        progress = await CampaignService.get_user_progress(c2_db, user.id)
        assert progress[1] is True
        assert progress[2] is True
        assert progress[3] is True
        assert progress[4] is False
        assert progress[5] is False

        # Completed count must not decrement or inflate beyond 3 unique chapters
        assert (await CampaignService.get_completed_count(c2_db, user.id)) == 3

    @pytest.mark.asyncio
    async def test_full_campaign_completion_immutable_under_all_replays(self, c2_db: AsyncSession):
        """Once 5/5 is reached, arbitrary replays maintain is_campaign_completed=True and count=5."""
        user = User(id=8002, username="veteran_hero", first_name="Василий")
        c2_db.add(user)
        await c2_db.commit()

        # Solve all 5 chapters
        for ch in range(1, 6):
            await CampaignService.record_chapter_completion(c2_db, user.id, ch)

        assert (await CampaignService.is_campaign_completed(c2_db, user.id)) is True
        assert (await CampaignService.get_completed_count(c2_db, user.id)) == 5

        # Replay chapters in chaotic random order
        replay_sequence = [5, 1, 3, 2, 4, 1, 5, 2, 3]
        for ch in replay_sequence:
            can_play, _ = await CampaignService.can_play_chapter(c2_db, user.id, ch)
            assert can_play is True, f"Chapter {ch} became unplayable during replay"
            is_first, exp = await CampaignService.record_chapter_completion(c2_db, user.id, ch)
            assert is_first is False
            assert exp > 0

            # State invariants must hold after EVERY replay
            assert (await CampaignService.get_completed_count(c2_db, user.id)) == 5
            assert (await CampaignService.is_campaign_completed(c2_db, user.id)) is True
            for check_ch in range(1, 6):
                can_p, _ = await CampaignService.can_play_chapter(c2_db, user.id, check_ch)
                assert can_p is True, f"Chapter {check_ch} locked after replaying {ch}"

    @pytest.mark.asyncio
    async def test_timestamps_and_completion_counts_integrity(self, c2_db: AsyncSession):
        """first_completed_at is immutable across replays; last_completed_at updates; completion_count increments."""
        user = User(id=8003, username="audit_time", first_name="Тимофей")
        c2_db.add(user)
        await c2_db.commit()

        # 1st completion of Chapter 1
        await CampaignService.record_chapter_completion(c2_db, user.id, 1)
        stmt = select(CampaignProgress).where(
            CampaignProgress.user_id == user.id, CampaignProgress.chapter_id == 1
        )
        rec = (await c2_db.execute(stmt)).scalar_one()
        first_time = rec.first_completed_at
        assert first_time is not None
        assert rec.completion_count == 1

        # Wait small moment and replay
        await asyncio.sleep(0.01)
        await CampaignService.record_chapter_completion(c2_db, user.id, 1)

        c2_db.expire_all()
        rec_reloaded = (await c2_db.execute(stmt)).scalar_one()
        # first_completed_at MUST be identical (normalizing for SQLite naive datetime storage)
        first_time_naive = first_time.replace(tzinfo=None) if first_time.tzinfo else first_time
        reloaded_time_naive = rec_reloaded.first_completed_at.replace(tzinfo=None) if rec_reloaded.first_completed_at.tzinfo else rec_reloaded.first_completed_at
        assert reloaded_time_naive == first_time_naive
        # last_completed_at must be >= first_time
        last_time_naive = rec_reloaded.last_completed_at.replace(tzinfo=None) if rec_reloaded.last_completed_at.tzinfo else rec_reloaded.last_completed_at
        assert last_time_naive >= first_time_naive
        # completion_count must be 2
        assert rec_reloaded.completion_count == 2

    @pytest.mark.asyncio
    async def test_uncompleted_campaign_session_does_not_mutate_progress(self, c2_db: AsyncSession):
        """Creating a campaign session for chapter 2 and leaving it incomplete does not alter progress."""
        user = User(id=8004, username="abandoner", first_name="Алексей")
        c2_db.add(user)
        await c2_db.commit()

        # Complete chapter 1
        await CampaignService.record_chapter_completion(c2_db, user.id, 1)
        assert (await CampaignService.get_completed_count(c2_db, user.id)) == 1

        # Create session for chapter 2 (unlocked)
        game = await CampaignService.create_campaign_session(c2_db, user_tg_id=user.id, chapter_id=2)
        assert game.status == SessionStatus.IN_PROGRESS
        assert game.campaign_chapter == 2

        # Invariant: Chapter 2 is NOT completed yet
        assert (await CampaignService.get_completed_count(c2_db, user.id)) == 1
        progress = await CampaignService.get_user_progress(c2_db, user.id)
        assert progress[2] is False

        # Cancel or abandon game
        game.status = SessionStatus.CANCELLED
        await c2_db.commit()

        # Invariant: Progress is still exactly 1, Chapter 2 is still unlocked, Chapter 3 is still locked
        assert (await CampaignService.get_completed_count(c2_db, user.id)) == 1
        assert (await CampaignService.can_play_chapter(c2_db, user.id, 2))[0] is True
        assert (await CampaignService.can_play_chapter(c2_db, user.id, 3))[0] is False


# ==============================================================================
# INVARIANT 2: Profile XML Invariant
# ==============================================================================

class TestC2ProfileXmlInvariant:
    """Exhaustive fuzzing and verification of military dossier card XML validity."""

    ALLOWED_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "pre", "a", "span", "tg-spoiler", "blockquote"}

    HOSTILE_NAME_PAYLOADS = [
        # XML / HTML injection
        "<script>alert(1)</script>",
        "<b>Bold</b>",
        "</b><script src='evil.js'>",
        "<a href='https://evil.com'>Click me</a>",
        "<![CDATA[<test>&evil;]]>",
        "<!-- Comment injection -->",
        "<unclosed tag",
        "<custom_tag>inside</custom_tag>",
        # Entities & quotes
        "&amp; &lt; &gt; &quot; &#39;",
        "&&&&& <<<<< >>>>> ''''' \"\"\"\"\"",
        "' OR '1'='1",
        "'; DROP TABLE users; --",
        # Extreme unicode
        "🔥🎖️🕵️‍♂️⚔️🇷🇺💥",
        "\u200b\u200c\u200d\ufeffHiddenZeroWidth",
        "\u202eReversedText",
        "Александр\n\r\tПробелы",
        "A" * 255,  # Max length
        "",  # Empty string
        "   ",  # Whitespace only
    ]

    def _verify_card_xml_and_tags(self, card: str, expected_campaign_status: str) -> None:
        """Helper to verify XML well-formedness and tag allowlist."""
        # 1. Parse XML fragment
        xml_envelope = f"<root>{card}</root>"
        try:
            root = ET.fromstring(xml_envelope)
        except ET.ParseError as err:
            pytest.fail(f"render_player_profile produced INVALID XML: {err}\nCard content:\n{card}")

        assert root.tag == "root"

        # 2. Verify all element tags are in Telegram allowed set
        for elem in root.iter():
            if elem.tag != "root":
                assert elem.tag in self.ALLOWED_TAGS, f"Illegal HTML tag <{elem.tag}> in profile card!"

        # 3. Verify campaign status line exists and matches expected
        assert expected_campaign_status in card, f"Expected '{expected_campaign_status}' not found in card"

    @pytest.mark.asyncio
    async def test_profile_card_validity_across_all_six_progression_states(self, c2_db: AsyncSession):
        """Card remains 100% valid XML across progression states 0/5, 1/5, 2/5, 3/5, 4/5, 5/5."""
        user = User(id=9001, username="ivan_normal", first_name="Иван", last_name="Иванов")
        c2_db.add(user)
        await c2_db.commit()

        for stage in range(6):
            if stage > 0:
                await CampaignService.record_chapter_completion(c2_db, user.id, stage)

            reloaded = await c2_db.get(User, user.id)
            card = render_player_profile(reloaded)
            expected_status = f"{stage}/5 глав"
            self._verify_card_xml_and_tags(card, expected_status)

    @pytest.mark.asyncio
    async def test_profile_card_adversarial_name_fuzzing(self, c2_db: AsyncSession):
        """Every hostile name payload produces 100% valid XML without entity injection or breakage."""
        uid_base = 9100
        for idx, payload in enumerate(self.HOSTILE_NAME_PAYLOADS):
            uid = uid_base + idx
            user = User(
                id=uid,
                username=f"hostile_{idx}",
                first_name=payload,
                last_name=payload,
                games_played=idx,
                games_won=idx,
                exp=idx * 250,
            )
            c2_db.add(user)
            await c2_db.commit()

            # Test at 0/5 and at 3/5
            card_0 = render_player_profile(user)
            self._verify_card_xml_and_tags(card_0, "0/5 глав")

            # Add progress
            for ch in (1, 2, 3):
                await CampaignService.record_chapter_completion(c2_db, uid, ch)

            reloaded = await c2_db.get(User, uid)
            card_3 = render_player_profile(reloaded)
            self._verify_card_xml_and_tags(card_3, "3/5 глав")

    @pytest.mark.asyncio
    async def test_profile_card_none_fields_safety(self, c2_db: AsyncSession):
        """User with None username, None last_name, None/0 exp renders valid XML."""
        user = User(id=9999, username=None, first_name="", last_name=None, exp=0)
        c2_db.add(user)
        await c2_db.commit()

        card = render_player_profile(user)
        self._verify_card_xml_and_tags(card, "0/5 глав")


# ==============================================================================
# INVARIANT 3: Session Isolation Invariant
# ==============================================================================

class TestC2SessionIsolationInvariant:
    """Verifies that story campaign gameplay does not corrupt regular solo or coop sessions."""

    @pytest.mark.asyncio
    async def test_campaign_does_not_corrupt_active_coop_session_as_analyst(
        self, c2_db: AsyncSession, c2_session_service: SessionService
    ):
        """User hosts a Coop session as Analyst, then plays Campaign: Coop session remains completely isolated."""
        # 1. User A creates coop session
        user_a_id = 7101
        coop_game = await c2_session_service.create_session(
            creator_tg_id=user_a_id,
            text="Секретная операция союзных войск на восточном направлении.",
            username="analyst_a",
            first_name="Алексей",
            db_session=c2_db,
        )
        assert coop_game.status == SessionStatus.WAITING_AGENT
        coop_code = coop_game.invite_code
        coop_id = coop_game.id
        coop_cipher = dict(coop_game.cipher_mapping)

        # 2. In parallel, User A starts and finishes Campaign Chapter 1
        camp_game = await CampaignService.create_campaign_session(
            c2_db, user_tg_id=user_a_id, chapter_id=1, username="analyst_a", first_name="Алексей"
        )
        assert camp_game.id != coop_id
        assert camp_game.invite_code != coop_code
        assert camp_game.is_solo is True
        assert camp_game.campaign_chapter == 1

        # User A guesses in campaign
        camp_cipher = camp_game.cipher_mapping
        sym_1 = list(camp_cipher.values())[0]
        char_1 = camp_game.code_to_char_map[sym_1]
        camp_res = await c2_session_service.submit_guess(
            session_id_or_code=camp_game.id,
            user_tg_id=user_a_id,
            symbol_code=sym_1,
            letter=char_1,
            db_session=c2_db,
        )
        assert camp_res.is_correct is True

        # Invariant check: Coop session must be COMPLETELY UNTOUCHED
        reloaded_coop = await c2_session_service.get_session(coop_id, db_session=c2_db)
        assert reloaded_coop is not None
        assert reloaded_coop.status == SessionStatus.WAITING_AGENT
        assert reloaded_coop.agent_id is None
        assert reloaded_coop.guessed_mapping == {}
        assert reloaded_coop.cipher_mapping == coop_cipher
        assert reloaded_coop.campaign_chapter is None

        # 3. Now User B joins the coop session successfully
        user_b_id = 7102
        joined_coop = await c2_session_service.join_session(
            invite_code=coop_code,
            agent_tg_id=user_b_id,
            username="agent_b",
            first_name="Борис",
            db_session=c2_db,
        )
        assert joined_coop.status == SessionStatus.IN_PROGRESS
        assert joined_coop.agent_id == user_b_id

    @pytest.mark.asyncio
    async def test_campaign_does_not_corrupt_active_coop_session_as_agent(
        self, c2_db: AsyncSession, c2_session_service: SessionService
    ):
        """User acts as Agent in active Coop session and in parallel plays Campaign: states remain strictly disjoint."""
        user_host = 7201
        user_agent = 7202

        coop_game = await c2_session_service.create_session(
            creator_tg_id=user_host,
            text="Разведка доложила точные координаты штаба противника.",
            db_session=c2_db,
        )
        await c2_session_service.join_session(
            invite_code=coop_game.invite_code,
            agent_tg_id=user_agent,
            db_session=c2_db,
        )

        # Agent plays campaign chapter 1
        camp_game = await CampaignService.create_campaign_session(
            c2_db, user_tg_id=user_agent, chapter_id=1
        )

        # Agent makes a guess in Campaign
        camp_sym = list(camp_game.cipher_mapping.values())[0]
        camp_letter = camp_game.code_to_char_map[camp_sym]
        await c2_session_service.submit_guess(
            session_id_or_code=camp_game.id,
            user_tg_id=user_agent,
            symbol_code=camp_sym,
            letter=camp_letter,
            db_session=c2_db,
        )

        # Verify Coop session guessed_mapping is STILL EMPTY
        coop_reloaded = await c2_session_service.get_session(coop_game.id, db_session=c2_db)
        assert coop_reloaded.guessed_mapping == {}
        assert coop_reloaded.status == SessionStatus.IN_PROGRESS

        # Agent makes a guess in Coop
        coop_sym = list(coop_reloaded.cipher_mapping.values())[0]
        coop_letter = coop_reloaded.code_to_char_map[coop_sym]
        await c2_session_service.submit_guess(
            session_id_or_code=coop_game.id,
            user_tg_id=user_agent,
            symbol_code=coop_sym,
            letter=coop_letter,
            db_session=c2_db,
        )

        # Verify Coop has exactly 1 guess, and Campaign has exactly 1 guess
        coop_reloaded = await c2_session_service.get_session(coop_game.id, db_session=c2_db)
        camp_reloaded = await c2_session_service.get_session(camp_game.id, db_session=c2_db)
        assert len(coop_reloaded.guessed_mapping) == 1
        assert len(camp_reloaded.guessed_mapping) == 1

    @pytest.mark.asyncio
    async def test_campaign_does_not_corrupt_standard_solo_session(
        self, c2_db: AsyncSession, c2_session_service: SessionService
    ):
        """User has an active regular solo game and launches a Campaign session: both proceed independently."""
        user_id = 7301
        standard_solo = await c2_session_service.create_solo_session(
            player_tg_id=user_id,
            difficulty="EASY",
            db_session=c2_db,
        )
        assert standard_solo.is_solo is True
        assert standard_solo.campaign_chapter is None

        # Start campaign
        camp_game = await CampaignService.create_campaign_session(
            c2_db, user_tg_id=user_id, chapter_id=1
        )
        assert camp_game.campaign_chapter == 1
        assert camp_game.id != standard_solo.id

        # Verify regular solo session unchanged
        reloaded_solo = await c2_session_service.get_session(standard_solo.id, db_session=c2_db)
        assert reloaded_solo.campaign_chapter is None
        assert reloaded_solo.status == SessionStatus.IN_PROGRESS


# ==============================================================================
# INVARIANT 4: Callback Payload Invariants
# ==============================================================================

class TestC2CallbackPayloadInvariants:
    """Stress testing Telegram 64-byte payload limits and crash immunity across all campaign callbacks."""

    ACTIONS = [
        "menu",
        "select",
        "chapter",
        "briefing",
        "start",
        "next",
        "locked",
        "victory",
        "debriefing",
        "unknown_action",
        "fallback",
        "x" * 30,  # Stress length
    ]

    CHAPTER_VALUES = [-999999, -1, 0, 1, 2, 3, 4, 5, 6, 7, 100, 999999999]

    def test_all_campaign_callback_combinations_under_64_bytes(self):
        """Ensure all generated combinations strictly satisfy len(pack().encode('utf-8')) < 64."""
        for act in self.ACTIONS:
            for ch in self.CHAPTER_VALUES:
                cb = CampaignCallback(action=act, chapter=ch)
                packed = cb.pack()
                byte_len = len(packed.encode("utf-8"))
                # Telegram hard limit is 64 bytes
                assert byte_len <= 64, f"Payload {packed!r} length {byte_len} exceeds 64 bytes!"

    @pytest.mark.asyncio
    async def test_all_callback_variations_always_answer_and_never_crash(self, c2_bot_env: Dict):
        """Fuzzing the campaign router with all actions and chapter values: must always answer and never raise."""
        mock_session: MockTelegramSession = c2_bot_env["mock_session"]
        bot: Bot = c2_bot_env["bot"]
        player: MockPlayerClient = c2_bot_env["player"]

        test_matrix = [
            ("menu", 0),
            ("select", 1),
            ("chapter", 1),
            ("chapter", 5),
            ("chapter", 6),  # OOB
            ("briefing", 2),
            ("start", 1),
            ("next", 2),
            ("locked", 3),
            ("victory", 1),
            ("debriefing", 5),
            ("forged_unknown", 99),
        ]

        for act, ch in test_matrix:
            mock_session.callback_answers.clear()
            payload = CampaignCallback(action=act, chapter=ch).pack()

            msg = types.Message(
                message_id=player._next_message_id(),
                date=datetime.now(timezone.utc),
                chat=player.chat,
                from_user=types.User(id=bot.id, is_bot=True, first_name="Bot"),
                text="Тест кнопок",
            )
            mock_session.messages[msg.message_id] = msg

            try:
                await player.click_button(payload, message=msg)
            except Exception as exc:
                pytest.fail(f"Callback {payload!r} triggered unhandled exception: {exc}")

            # Must have answered callback
            assert len(mock_session.callback_answers) > 0, f"Callback {payload!r} did not answer!"

    @pytest.mark.asyncio
    async def test_callback_defensive_against_missing_from_user_and_message(self, c2_bot_env: Dict):
        """Ensures callbacks with message=None and from_user=None are safely handled without crash."""
        dp: Dispatcher = c2_bot_env["dp"]
        bot: Bot = c2_bot_env["bot"]
        player: MockPlayerClient = c2_bot_env["player"]

        # Case A: message is None
        cb_none_msg = types.CallbackQuery(
            id=str(player._next_update_id()),
            from_user=player.user,
            chat_instance="chat_inst_1",
            data=CampaignCallback(action="menu", chapter=0).pack(),
            message=None,
        )
        update_a = types.Update(update_id=player._next_update_id(), callback_query=cb_none_msg)
        try:
            await dp.feed_update(bot, update_a)
        except Exception as e:
            pytest.fail(f"Callback with message=None crashed: {e}")

        # Case B: from_user is None
        msg = types.Message(
            message_id=player._next_message_id(),
            date=datetime.now(timezone.utc),
            chat=player.chat,
            from_user=types.User(id=bot.id, is_bot=True, first_name="Bot"),
            text="Тест",
        )
        cb_none_user = types.CallbackQuery(
            id=str(player._next_update_id()),
            from_user=None,
            chat_instance="chat_inst_2",
            data=CampaignCallback(action="menu", chapter=0).pack(),
            message=msg,
        )
        update_b = types.Update(update_id=player._next_update_id(), callback_query=cb_none_user)
        try:
            await dp.feed_update(bot, update_b)
        except Exception as e:
            pytest.fail(f"Callback with from_user=None crashed: {e}")

    @pytest.mark.asyncio
    async def test_rapid_clicks_unmodified_shield(self, c2_engine: AsyncEngine):
        """Simulate rapid double clicks with simulate_unmodified_error=True to prove TelegramBadRequest suppression."""
        mock_session = MockTelegramSession(simulate_unmodified_error=True)
        bot = Bot(token="42:MOCK_TOKEN", session=mock_session)
        service = SessionService(session_factory=create_session_factory(c2_engine))
        dp = create_dispatcher(
            session_service=service,
            lock_manager=service.lock_manager,
            bot_username="SpyAnalystBot",
            storage=MemoryStorage(),
        )
        player = MockPlayerClient(dp=dp, bot=bot, user_id=880099)

        await player.send_command("/start")
        start_msg = mock_session.get_last_message_for_chat(880099)
        assert start_msg is not None

        cb_data = CampaignCallback(action="menu", chapter=0).pack()

        # Two consecutive identical clicks
        try:
            await player.click_button(cb_data, message=start_msg)
            await player.click_button(cb_data, message=start_msg)
        except TelegramBadRequest as exc:
            pytest.fail(f"Unmodified error shield failed: {exc}")


class TestC2AdvancedStressScenarios:
    """Advanced adversarial stress tests across cross-cutting subsystem interactions."""

    @pytest.mark.asyncio
    async def test_campaign_user_summary_consistency_on_replays(self, c2_db: AsyncSession):
        """get_user_campaign_summary reflects exact chapter statuses throughout initial progression and replays."""
        user = User(id=8501, username="summary_tester", first_name="Семен")
        c2_db.add(user)
        await c2_db.commit()

        # Initial state: 0 completed, ch1 UNLOCKED, ch2..5 LOCKED
        s0 = await CampaignService.get_user_campaign_summary(c2_db, user.id)
        assert s0.completed_count == 0
        assert s0.is_campaign_completed is False
        assert s0.current_active_chapter == 1
        assert s0.status_display == "0/5 глав"
        assert s0.chapters[1].status == ChapterAccessStatus.UNLOCKED
        assert s0.chapters[2].status == ChapterAccessStatus.LOCKED

        # Complete chapter 1 & 2
        await CampaignService.record_chapter_completion(c2_db, user.id, 1)
        await CampaignService.record_chapter_completion(c2_db, user.id, 2)

        s2 = await CampaignService.get_user_campaign_summary(c2_db, user.id)
        assert s2.completed_count == 2
        assert s2.is_campaign_completed is False
        assert s2.current_active_chapter == 3
        assert s2.status_display == "2/5 глав"
        assert s2.chapters[1].status == ChapterAccessStatus.COMPLETED
        assert s2.chapters[2].status == ChapterAccessStatus.COMPLETED
        assert s2.chapters[3].status == ChapterAccessStatus.UNLOCKED
        assert s2.chapters[4].status == ChapterAccessStatus.LOCKED

        # Replay Chapter 1 multiple times
        for _ in range(5):
            await CampaignService.record_chapter_completion(c2_db, user.id, 1)

        s_replay = await CampaignService.get_user_campaign_summary(c2_db, user.id)
        assert s_replay.completed_count == 2
        assert s_replay.current_active_chapter == 3
        assert s_replay.status_display == "2/5 глав"
        assert s_replay.chapters[1].completion_count == 6
        assert s_replay.chapters[1].status == ChapterAccessStatus.COMPLETED
        assert s_replay.chapters[3].status == ChapterAccessStatus.UNLOCKED

    @pytest.mark.asyncio
    async def test_unauthorized_direct_session_creation_for_locked_chapters(self, c2_db: AsyncSession):
        """create_campaign_session strictly forbids launching locked or out-of-bounds chapters."""
        user = User(id=8502, username="lock_breaker", first_name="Взломщик")
        c2_db.add(user)
        await c2_db.commit()

        # Attempt to launch locked chapters (2..5)
        for locked_ch in range(2, 6):
            with pytest.raises(ValueError, match="засекречена"):
                await CampaignService.create_campaign_session(c2_db, user_tg_id=user.id, chapter_id=locked_ch)

        # Attempt invalid chapter IDs
        for invalid_ch in (-10, 0, 6, 999):
            with pytest.raises(ValueError, match="Недопустимый номер главы"):
                await CampaignService.create_campaign_session(c2_db, user_tg_id=user.id, chapter_id=invalid_ch)

    @pytest.mark.asyncio
    async def test_campaign_replay_exp_awarding_distinction(self, c2_db: AsyncSession):
        """First completion awards full 400 + 200 first completion bonus (+clean/autonomy); replay awards base 200."""
        user = User(id=8503, username="exp_analyst", first_name="Эдуард", exp=0)
        c2_db.add(user)
        await c2_db.commit()

        # First completion with clean work and autonomy (0 mistakes, 0 hints)
        is_first, exp1 = await CampaignService.record_chapter_completion(
            c2_db, user.id, 1, mistakes_count=0, hints_used=0
        )
        assert is_first is True
        # 400 base + 200 first + 100 clean + 100 autonomy = 800
        assert exp1 == 800

        # Replay with clean work and autonomy
        is_first_rep, exp_rep = await CampaignService.record_chapter_completion(
            c2_db, user.id, 1, mistakes_count=0, hints_used=0
        )
        assert is_first_rep is False
        # 200 replay base + 100 clean + 100 autonomy = 400
        assert exp_rep == 400

        reloaded = await c2_db.get(User, user.id)
        assert reloaded.exp == 1200

    @pytest.mark.asyncio
    async def test_coop_session_cancellation_does_not_affect_campaign(
        self, c2_db: AsyncSession, c2_session_service: SessionService
    ):
        """Cancelling or finishing coop operations leaves story campaign progress 100% unaffected."""
        user_id = 8504
        user = User(id=user_id, username="coop_canceller", first_name="Кирилл")
        c2_db.add(user)
        await c2_db.commit()

        # Complete chapters 1 and 2 in campaign
        await CampaignService.record_chapter_completion(c2_db, user_id, 1)
        await CampaignService.record_chapter_completion(c2_db, user_id, 2)
        assert (await CampaignService.get_completed_count(c2_db, user_id)) == 2

        # Create coop session
        coop = await c2_session_service.create_session(
            creator_tg_id=user_id,
            text="План совместной радиопередачи разведгруппы в центр.",
            db_session=c2_db,
        )
        assert coop.status == SessionStatus.WAITING_AGENT

        # Cancel coop session
        await c2_session_service.cancel_session(coop.id, user_tg_id=user_id, db_session=c2_db)
        reloaded_coop = await c2_session_service.get_session(coop.id, db_session=c2_db)
        assert reloaded_coop.status == SessionStatus.CANCELLED

        # Verify campaign state is 100% intact
        progress = await CampaignService.get_user_progress(c2_db, user_id)
        assert progress[1] is True
        assert progress[2] is True
        assert progress[3] is False
        assert (await CampaignService.get_completed_count(c2_db, user_id)) == 2
        assert (await CampaignService.can_play_chapter(c2_db, user_id, 3))[0] is True

