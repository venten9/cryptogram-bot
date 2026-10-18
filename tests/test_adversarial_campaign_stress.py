"""Adversarial Stress Test Suite & Bug Hunters Harness for Story Campaign.

Milestone 4: Adversarial Verification & Stress Testing
Investigates:
1. High concurrency and race conditions:
   - Simultaneous record_chapter_completion on fresh user records.
   - Concurrent can_play_chapter requests during active unlocks.
   - Rapid sequential progression under race conditions.
2. Hostile inputs & Parameter Forgery:
   - Out-of-bounds chapter IDs, extreme values, negative IDs.
   - Forged callback queries attempting to bypass sequential gating (camp:start:2, camp:start:5, camp:briefing:4).
   - Forged victory screens (camp:victory:5, camp:debriefing:5).
   - Malformed, corrupted, and Unicode-fuzzed callback strings.
3. Actual End-to-End Campaign Gameplay Loop:
   - Empirical verification of the full game loop:
     Starting Chapter 1 -> Submitting guesses to victory -> Verifying whether
     CampaignProgress is recorded, whether Chapter 2 unlocks, whether elevated
     campaign EXP is awarded, and whether the victory screen offers next-chapter transition.
4. Callback Queries Crash Shield under hostile/corrupt inputs.
"""
import asyncio
import html
import random
import string
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import AsyncGenerator, Dict, List

import pytest
import pytest_asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from bot.app import create_dispatcher
from bot.callbacks import (
    CampaignCallback,
    HintActionCallback,
    LetterGuessCallback,
    MenuCallback,
    SymbolSelectCallback,
)
from bot.keyboards import (
    build_campaign_briefing_keyboard,
    build_campaign_menu_keyboard,
    build_campaign_victory_keyboard,
)
from cryptogram.campaign_data import (
    CAMPAIGN_CHAPTERS,
    TOTAL_CHAPTERS,
    get_all_campaign_chapters,
    get_campaign_chapter,
)
from database.models import Base, CampaignProgress, GameSession, SessionStatus, User
from database.session import create_session_factory
from services.campaign_service import CampaignService, ChapterAccessStatus
from services.lock_manager import SessionLockManager
from services.session_service import GuessStatus, SessionService
from tests.mock_session import MockPlayerClient, MockTelegramSession


@pytest_asyncio.fixture(scope="function")
async def adv_engine() -> AsyncGenerator[AsyncEngine, None]:
    """Isolated in-memory SQLite async engine."""
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
async def adv_service(adv_engine: AsyncEngine) -> SessionService:
    session_factory = create_session_factory(adv_engine)
    lock_manager = SessionLockManager()
    return SessionService(session_factory=session_factory, lock_manager=lock_manager)


@pytest_asyncio.fixture(scope="function")
async def adv_env(adv_service: SessionService) -> Dict:
    mock_session = MockTelegramSession()
    bot = Bot(token="42:MOCK_TOKEN", session=mock_session)
    dp = create_dispatcher(
        session_service=adv_service,
        lock_manager=adv_service.lock_manager,
        bot_username="SpyAnalystBot",
        storage=MemoryStorage(),
    )
    player = MockPlayerClient(
        dp=dp,
        bot=bot,
        user_id=888001,
        username="hacker_agent",
        first_name="Виктор",
    )
    return {
        "engine": adv_service.session_factory,
        "mock_session": mock_session,
        "bot": bot,
        "dp": dp,
        "player": player,
        "service": adv_service,
    }


async def click_with_msg(adv_env: Dict, cb_data: str, text: str = "Test"):
    player = adv_env["player"]
    mock_session = adv_env["mock_session"]
    bot = adv_env["bot"]
    msg = types.Message(
        message_id=player._next_message_id(),
        date=datetime.now(timezone.utc),
        chat=player.chat,
        from_user=types.User(id=bot.id, is_bot=True, first_name="Bot"),
        text=text,
    )
    mock_session.messages[msg.message_id] = msg
    await player.click_button(cb_data, message=msg)
    return msg


class TestAdversarialGatingAndForgedCallbacks:
    """Stress-test sequential gating against forged parameters and direct callback injections."""

    @pytest.mark.asyncio
    async def test_forged_direct_start_locked_chapters(self, adv_env: Dict):
        """Player sends camp:start:2 or camp:start:5 without completing chapter 1."""
        player: MockPlayerClient = adv_env["player"]
        mock_session: MockTelegramSession = adv_env["mock_session"]
        service: SessionService = adv_env["service"]

        # Ensure user exists
        async with service.session_factory() as session:
            user = User(id=888001, username="hacker_agent", first_name="Виктор")
            session.add(user)
            await session.commit()

        # Attempt to forge direct start of chapters 2, 3, 4, 5
        for ch in [2, 3, 4, 5]:
            mock_session.callback_answers.clear()
            cb_data = CampaignCallback(action="start", chapter=ch).pack()
            await click_with_msg(adv_env, cb_data, "Start")

            # Must answer and must NOT create active game session for that chapter
            assert len(mock_session.callback_answers) > 0
            has_alert = any(ans.show_alert is True for ans in mock_session.callback_answers)
            assert has_alert is True, f"Chapter {ch} start did not show alert"

            # Verify in DB that no session was created for this chapter
            async with service.session_factory() as session:
                stmt = select(GameSession).where(
                    GameSession.agent_id == 888001,
                    GameSession.campaign_chapter == ch,
                )
                res = (await session.execute(stmt)).scalars().all()
                assert len(res) == 0, f"Forged start allowed creation of session for locked chapter {ch}!"

    @pytest.mark.asyncio
    async def test_forged_briefing_locked_chapters(self, adv_env: Dict):
        """Player directly sends camp:briefing:5 without completing chapters 1-4."""
        player: MockPlayerClient = adv_env["player"]
        mock_session: MockTelegramSession = adv_env["mock_session"]
        service: SessionService = adv_env["service"]

        async with service.session_factory() as session:
            user = User(id=888001, username="hacker_agent", first_name="Виктор")
            session.add(user)
            await session.commit()

        mock_session.callback_answers.clear()
        cb_data = CampaignCallback(action="briefing", chapter=5).pack()
        await click_with_msg(adv_env, cb_data, "Briefing")

        assert len(mock_session.callback_answers) > 0
        has_alert = any(ans.show_alert is True for ans in mock_session.callback_answers)
        assert has_alert is True, "Forged briefing did not show alert for locked chapter 5"

    @pytest.mark.asyncio
    async def test_forged_victory_callback_does_not_award_exp_or_progress(self, adv_env: Dict):
        """Adversarial check: clicking camp:victory:5 or camp:debriefing:5 must NOT mark chapter completed in DB."""
        player: MockPlayerClient = adv_env["player"]
        mock_session: MockTelegramSession = adv_env["mock_session"]
        service: SessionService = adv_env["service"]

        async with service.session_factory() as session:
            user = User(id=888001, username="hacker_agent", first_name="Виктор", exp=0)
            session.add(user)
            await session.commit()

        mock_session.callback_answers.clear()
        cb_data = CampaignCallback(action="victory", chapter=5).pack()
        await player.click_button(cb_data)

        # Verify DB progress
        async with service.session_factory() as session:
            count = await CampaignService.get_completed_count(session, 888001)
            assert count == 0, "Forged victory callback must not record completion in DB"
            reloaded_user = await session.get(User, 888001)
            assert reloaded_user.exp == 0, "Forged victory callback must not award EXP"

    @pytest.mark.asyncio
    async def test_record_completion_out_of_bounds_validation(self, adv_service: SessionService):
        """Stress-test record_chapter_completion with invalid/out-of-bounds chapter IDs."""
        session_factory = adv_service.session_factory
        user_id = 888002

        async with session_factory() as session:
            user = User(id=user_id, username="fuzzer", first_name="Тест")
            session.add(user)
            await session.commit()

        # Calling with invalid chapter IDs should raise ValueError or be safely rejected
        for invalid_ch in [-1, 0, 6, 999]:
            async with session_factory() as session:
                try:
                    await CampaignService.record_chapter_completion(session, user_id, invalid_ch)
                    # If it didn't raise, verify it didn't pollute database with invalid chapter IDs
                    rec = (await session.execute(
                        select(CampaignProgress).where(
                            CampaignProgress.user_id == user_id,
                            CampaignProgress.chapter_id == invalid_ch,
                        )
                    )).scalar_one_or_none()
                    assert rec is None, f"Database polluted with invalid chapter_id={invalid_ch}!"
                except ValueError:
                    # Raising ValueError on invalid chapter is also correct defensive behavior
                    pass

    @pytest.mark.asyncio
    async def test_hostile_fuzzed_callback_data(self, adv_env: Dict):
        """Send hostile callback data strings with Unicode fuzzing, SQL injection, and buffer overflows."""
        player: MockPlayerClient = adv_env["player"]
        mock_session: MockTelegramSession = adv_env["mock_session"]

        fuzzed_payloads = [
            "camp:start:-99999999999999999999999",
            "camp:start:99999999999999999999999",
            "camp:start:0",
            "camp:start:6",
            "camp:locked:-1",
            "camp:locked:100",
            "camp:briefing:0",
            "camp:briefing:-5",
            "camp:unknown_action:1",
            "camp:;DROP TABLE users;--:1",
            "camp:🔥🚀💀:1",
            "camp:\u202e\u200b\ufeff:1",
            "camp:None:None",
            "camp:null:0",
            "camp::::",
            "camp:",
            "camp:start:abc",
        ]

        for payload in fuzzed_payloads:
            mock_session.callback_answers.clear()
            # None of these should raise unhandled exceptions
            try:
                await player.click_button(payload)
            except Exception as exc:
                pytest.fail(f"Unhandled exception processing hostile payload '{payload}': {exc}")


class TestAdversarialConcurrencyAndRaceConditions:
    """Stress-test high concurrency on campaign progression."""

    @pytest.mark.asyncio
    async def test_concurrent_first_completion_race(self, adv_service: SessionService):
        """Simultaneously execute record_chapter_completion from 10 concurrent coroutines for the same user."""
        user_id = 999101
        session_factory = adv_service.session_factory

        async with session_factory() as s:
            u = User(id=user_id, username="race_tester", first_name="Григорий", exp=0)
            s.add(u)
            await s.commit()

        # Run 10 concurrent completions of Chapter 1
        async def complete_ch1():
            async with session_factory() as s:
                return await CampaignService.record_chapter_completion(s, user_id, 1)

        results = await asyncio.gather(*[complete_ch1() for _ in range(10)], return_exceptions=True)

        # Ensure no unexpected exceptions crashed the tasks
        exceptions = [r for r in results if isinstance(r, Exception)]
        # Check if database locked or concurrency handled
        assert len(exceptions) == 0, f"Concurrent completions raised exceptions: {exceptions}"

        # Exactly one task should have received is_first_completion=True
        # Or if transactions raced, verify the final DB state is consistent
        async with session_factory() as s:
            recs = (await s.execute(
                select(CampaignProgress).where(
                    CampaignProgress.user_id == user_id,
                    CampaignProgress.chapter_id == 1,
                )
            )).scalars().all()
            # There must be EXACTLY 1 record in campaign_progress (unique constraint preserved)
            assert len(recs) == 1, f"Expected 1 progress record, found {len(recs)}"
            assert recs[0].is_completed is True
            assert recs[0].completion_count == 10

    @pytest.mark.asyncio
    async def test_rapid_concurrent_unlock_progression(self, adv_service: SessionService):
        """Verify sequential progression under rapid alternating checks and completions."""
        user_id = 999102
        session_factory = adv_service.session_factory

        async with session_factory() as s:
            u = User(id=user_id, username="speedrun_tester", first_name="Михаил", exp=0)
            s.add(u)
            await s.commit()

        for ch in range(1, 6):
            async with session_factory() as s:
                can_play, _ = await CampaignService.can_play_chapter(s, user_id, ch)
                assert can_play is True, f"Chapter {ch} should be playable"
                await CampaignService.record_chapter_completion(s, user_id, ch)

            # Chapters > ch+1 must still be locked
            if ch + 2 <= 5:
                async with session_factory() as s:
                    can_play_future, _ = await CampaignService.can_play_chapter(s, user_id, ch + 2)
                    assert can_play_future is False, f"Chapter {ch+2} should NOT be unlocked yet!"

        async with session_factory() as s:
            is_done = await CampaignService.is_campaign_completed(s, user_id)
            assert is_done is True


class TestAdversarialEndToEndCampaignGameplayLoop:
    """Empirical investigation into whether actual gameplay in campaign mode completes chapters.
    
    CRITICAL CHECK:
    When a player launches a campaign chapter in Telegram (/campaign -> Select Chapter 1 -> Start),
    plays the game, and solves it via submit_guess:
    - Does the system actually record completion in campaign_progress?
    - Does Chapter 2 become unlocked?
    - Is elevated campaign EXP awarded?
    - Does the victory screen transition to campaign debriefing or next chapter?
    """

    @pytest.mark.asyncio
    async def test_campaign_gameplay_to_victory_flow(self, adv_env: Dict):
        """Test actual end-to-end player solving Chapter 1 through bot handlers."""
        player: MockPlayerClient = adv_env["player"]
        mock_session: MockTelegramSession = adv_env["mock_session"]
        service: SessionService = adv_env["service"]
        user_id = player.user.id

        # 1. Start Chapter 1 via CampaignCallback start
        mock_session.callback_answers.clear()
        start_cb = CampaignCallback(action="start", chapter=1).pack()
        await click_with_msg(adv_env, start_cb, "Start Chapter 1")

        # 2. Retrieve created GameSession
        async with service.session_factory() as session:
            stmt = select(GameSession).where(
                GameSession.agent_id == user_id,
                GameSession.campaign_chapter == 1,
            )
            game = (await session.execute(stmt)).scalars().first()
            assert game is not None, "Campaign session for Chapter 1 was not created"
            assert game.status == SessionStatus.IN_PROGRESS
            assert game.is_solo is True
            assert game.campaign_chapter == 1

            # Get cipher mapping
            cipher_mapping = dict(game.cipher_mapping)
            invite_code = game.invite_code

        # 3. Simulate solving every letter in the cipher
        for char, code in cipher_mapping.items():
            res = await service.submit_guess(
                session_id_or_code=game.id,
                user_tg_id=user_id,
                symbol_code=code,
                letter=char,
            )

        # 4. Reload GameSession
        reloaded_game = await service.get_session_by_id(game.id)
        assert reloaded_game.status == SessionStatus.SOLVED, "Game was not solved"

        # 5. NOW CHECK: Did the gameplay victory record Chapter 1 in CampaignProgress?
        async with service.session_factory() as session:
            count = await CampaignService.get_completed_count(session, user_id)
            progress = await CampaignService.get_user_progress(session, user_id)
            can_play_ch2, reason = await CampaignService.can_play_chapter(session, user_id, 2)
            # Verify DB progress after gameplay victory
            # EMPIRICAL ORACLE ASSERTION:
            # If the implementation does not connect game victory to campaign_progress,
            # this assertion will fail and reveal the exact bug!
            assert count == 1, (
                f"BUG DETECTED: Game victory in campaign session did NOT record progress in DB! "
                f"Completed count is {count}, expected 1."
            )
            assert progress.get(1) is True, "BUG DETECTED: Chapter 1 is not marked as completed after victory!"
            assert can_play_ch2 is True, "BUG DETECTED: Chapter 2 remains locked after solving Chapter 1!"
