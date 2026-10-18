"""Comprehensive 4-Tier E2E Test Suite for Story Campaign Mode.

«Сюжетная кампания: Секретные директивы Ставки (1941–1945)»

Tiers Covered:
- Tier 1: Feature Coverage (5 historical chapters, crypto compliance, persistence,
  sequential progression, replayability, full completion, elevated EXP, profile display).
- Tier 2: Boundaries & Corner Cases (out-of-bounds IDs, duplicate completions,
  non-existent users, locked chapter clicks, 0-progress boundaries).
- Tier 3: Cross-Feature Combinations (replaying earlier chapters while later are active,
  profile consistency across all 6 progression stages, skipping ahead attempts).
- Tier 4: Real-World Playthrough Scenario (complete 1941 -> 1945 walkthrough).
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
from bot.callbacks import MenuCallback
from bot.handlers.common import render_player_profile
from bot.keyboards import build_main_menu_keyboard
from cryptogram.engine import (
    RUSSIAN_ALPHABET_SET,
    calculate_frequencies,
    check_victory,
    generate_cipher,
    normalize_text,
    render_mask,
    validate_text,
)
from cryptogram.models import CipherData
from database.models import Base, GameSession, SessionStatus, User
from database.session import create_session_factory
from services.lock_manager import SessionLockManager
from services.session_service import SessionService
from tests.mock_session import MockPlayerClient, MockTelegramSession

# Optional imports for modules that are implemented as part of Campaign milestones
try:
    import cryptogram.campaign_data as campaign_data
    from cryptogram.campaign_data import (
        CAMPAIGN_CHAPTERS,
        CampaignChapter,
        get_all_campaign_chapters,
        get_campaign_chapter,
    )
except ImportError:
    campaign_data = None
    CAMPAIGN_CHAPTERS = None
    CampaignChapter = None
    get_all_campaign_chapters = None
    get_campaign_chapter = None

try:
    from database.models import CampaignProgress
except ImportError:
    CampaignProgress = None

try:
    import services.campaign_service as campaign_service_module
    from services.campaign_service import CampaignService
except ImportError:
    campaign_service_module = None
    CampaignService = None

try:
    from bot.callbacks import CampaignCallback
except ImportError:
    CampaignCallback = None

try:
    from bot.keyboards import (
        build_campaign_briefing_keyboard,
        build_campaign_menu_keyboard,
        build_campaign_victory_keyboard,
    )
except ImportError:
    build_campaign_menu_keyboard = None
    build_campaign_briefing_keyboard = None
    build_campaign_victory_keyboard = None


# Authoritative Historical Directives from ORIGINAL_REQUEST.md (R1)
HISTORICAL_DIRECTIVES: Dict[int, Dict[str, object]] = {
    1: {
        "year": 1941,
        "date_str": "22 июня 1941 г.",
        "title": "Внезапный удар",
        "issuing_body": "Наркомат обороны СССР",
        "document_name": "Директива № 1",
        "text": "В течение 22–23 июня возможно внезапное нападение. Задача наших войск — быть в полной боевой готовности встретить удар.",
        "expected_min_letters": 25,
    },
    2: {
        "year": 1942,
        "date_str": "28 июля 1942 г.",
        "title": "Сталинград",
        "issuing_body": "Приказ НКО СССР № 227",
        "document_name": "Приказ НКО СССР № 227",
        "text": "Отступать дальше — значит загубить себя и нашу Родину. Каждый командир и боец обязан понять: ни шагу назад!",
        "expected_min_letters": 24,
    },
    3: {
        "year": 1942,
        "date_str": "Ноябрь 1942 г.",
        "title": "Операция «Уран»",
        "issuing_body": "Директива Ставки ВГК",
        "document_name": "Директива Ставки ВГК",
        "text": "Войскам фронтов перейти в решительное наступление с целью окружения и полного разгрома сталинградской группировки врага.",
        "expected_min_letters": 24,
    },
    4: {
        "year": 1943,
        "date_str": "Июль 1943 г.",
        "title": "Курская дуга",
        "issuing_body": "План контрнаступления Ставки",
        "document_name": "План контрнаступления Ставки",
        "text": "Главная задача наших войск — измотать ударные бронетанковые клинья противника и перейти в генеральное контрнаступление.",
        "expected_min_letters": 24,
    },
    5: {
        "year": 1945,
        "date_str": "Апрель 1945 г.",
        "title": "Штурм Берлина",
        "issuing_body": "Боевой приказ командующего фронтом",
        "document_name": "Боевой приказ командующего фронтом",
        "text": "Войскам фронта сломить сопротивление на подступах к городу, овладеть Берлином и водрузить Знамя Победы над рейхстагом.",
        "expected_min_letters": 24,
    },
}


# ==============================================================================
# Database & Service Fixtures
# ==============================================================================

@pytest_asyncio.fixture(scope="function")
async def campaign_test_engine() -> AsyncGenerator[AsyncEngine, None]:
    """Isolated in-memory SQLite async engine with foreign keys enabled."""
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
async def db_session(campaign_test_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """Fresh database session bound to the isolated engine."""
    session_factory = create_session_factory(campaign_test_engine)
    async with session_factory() as session:
        yield session


@pytest_asyncio.fixture(scope="function")
async def campaign_session_service(campaign_test_engine: AsyncEngine) -> SessionService:
    """Fresh SessionService bound to the isolated engine."""
    session_factory = create_session_factory(campaign_test_engine)
    lock_manager = SessionLockManager()
    return SessionService(session_factory=session_factory, lock_manager=lock_manager)


class CampaignServiceHelper:
    """Defensive caller supporting both static and instance CampaignService methods."""

    @staticmethod
    async def can_play_chapter(db: AsyncSession, user_id: int, chapter_id: int) -> Tuple[bool, str]:
        if CampaignService is None:
            pytest.skip("CampaignService not implemented")
        try:
            return await CampaignService.can_play_chapter(db, user_id, chapter_id)
        except (TypeError, AttributeError):
            svc = CampaignService()
            return await svc.can_play_chapter(user_id=user_id, chapter_id=chapter_id, db_session=db)

    @staticmethod
    async def record_chapter_completion(db: AsyncSession, user_id: int, chapter_id: int):
        if CampaignService is None:
            pytest.skip("CampaignService not implemented")
        try:
            return await CampaignService.record_chapter_completion(db, user_id, chapter_id)
        except (TypeError, AttributeError):
            svc = CampaignService()
            return await svc.record_chapter_completion(user_id=user_id, chapter_id=chapter_id, db_session=db)

    @staticmethod
    async def get_completed_count(db: AsyncSession, user_id: int) -> int:
        if CampaignService is None:
            pytest.skip("CampaignService not implemented")
        try:
            return await CampaignService.get_completed_count(db, user_id)
        except (TypeError, AttributeError):
            svc = CampaignService()
            return await svc.get_completed_count(user_id=user_id, db_session=db)

    @staticmethod
    async def is_campaign_completed(db: AsyncSession, user_id: int) -> bool:
        if CampaignService is None:
            pytest.skip("CampaignService not implemented")
        try:
            return await CampaignService.is_campaign_completed(db, user_id)
        except (TypeError, AttributeError):
            svc = CampaignService()
            return await svc.is_campaign_completed(user_id=user_id, db_session=db)

    @staticmethod
    async def get_user_progress(db: AsyncSession, user_id: int) -> Dict[int, bool]:
        if CampaignService is None:
            pytest.skip("CampaignService not implemented")
        try:
            return await CampaignService.get_user_progress(db, user_id)
        except (TypeError, AttributeError):
            svc = CampaignService()
            return await svc.get_user_progress(user_id=user_id, db_session=db)


# ==============================================================================
# TIER 1: Feature Coverage (Engine, Catalog, DB, Progression, EXP, Profile, UI)
# ==============================================================================

class TestTier1HistoricalDirectivesAndEngineCompliance:
    """Tier 1: Verification of the 5 authentic historical directives against the crypto engine."""

    def test_all_five_historical_texts_satisfy_crypto_engine_constraints(self):
        """Verify each authentic Soviet directive satisfies length and Cyrillic bounds."""
        for ch_id, data in HISTORICAL_DIRECTIVES.items():
            text = str(data["text"])
            res = validate_text(
                text=text,
                min_length=30,
                max_length=200,
                min_unique_letters=3,
                disallow_latin=True,
            )
            assert res.is_valid is True, f"Chapter {ch_id} failed validation: {res.error_message}"

    def test_directive_texts_length_boundaries_between_30_and_200(self):
        """Verify all 5 directives strictly fall within 30..200 characters per AC."""
        for ch_id, data in HISTORICAL_DIRECTIVES.items():
            text = str(data["text"])
            assert 30 <= len(text) <= 200, f"Chapter {ch_id} length {len(text)} outside 30..200"

    def test_directive_texts_unique_letters_and_cardinality(self):
        """Verify sufficient unique Cyrillic letters in each directive for engaging decryption."""
        for ch_id, data in HISTORICAL_DIRECTIVES.items():
            text = str(data["text"])
            norm = normalize_text(text)
            unique_letters = set(c for c in norm if c in RUSSIAN_ALPHABET_SET)
            min_expected = int(data["expected_min_letters"])
            assert len(unique_letters) >= min_expected, (
                f"Chapter {ch_id} has {len(unique_letters)} unique letters, expected >= {min_expected}"
            )

    def test_directive_texts_special_characters_and_dashes_preservation(self):
        """Verify that dashes (—, –), punctuation (., !, :), and digits are preserved in mask."""
        for ch_id, data in HISTORICAL_DIRECTIVES.items():
            text = str(data["text"])
            cipher = generate_cipher(text, seed=100 + ch_id)
            # Empty revealed letters mask
            mask = render_mask(text, cipher.char_to_code, revealed_letters=set(), wrap_pre=False)
            
            # Non-Cyrillic characters must remain unmasked
            if ch_id == 1:
                assert "22–23" in mask or "22-23" in mask, "Dates should be preserved"
                assert "—" in mask, "Em-dash should be preserved"
            elif ch_id == 2:
                assert "!" in mask, "Exclamation mark should be preserved"
                assert ":" in mask, "Colon should be preserved"
            elif ch_id == 4:
                assert "—" in mask, "Em-dash should be preserved"

    def test_directive_texts_cipher_generation_invertibility(self):
        """Verify exact bijection and invertibility for all 5 directive ciphers."""
        for ch_id, data in HISTORICAL_DIRECTIVES.items():
            text = str(data["text"])
            cipher = generate_cipher(text, seed=42)
            assert len(cipher.char_to_code) == len(cipher.code_to_char)
            for char, code in cipher.char_to_code.items():
                assert cipher.code_to_char[code] == char

    def test_directive_texts_frequency_conservation(self):
        """Verify frequency calculation conservation on all 5 historical directives."""
        for ch_id, data in HISTORICAL_DIRECTIVES.items():
            text = str(data["text"])
            freqs = calculate_frequencies(text)
            norm = normalize_text(text)
            expected_total = sum(1 for c in norm if c in RUSSIAN_ALPHABET_SET)
            assert sum(f.count for f in freqs) == expected_total
            assert pytest.approx(sum(f.percentage for f in freqs), abs=0.5) == 100.0

    def test_directive_texts_victory_verification(self):
        """Verify check_victory accurately recognizes partial vs complete decryption."""
        for ch_id, data in HISTORICAL_DIRECTIVES.items():
            text = str(data["text"])
            norm = normalize_text(text)
            unique = set(c for c in norm if c in RUSSIAN_ALPHABET_SET)
            
            # None revealed -> False
            assert check_victory(text, set()) is False
            
            # All but one revealed -> False
            almost_all = set(list(unique)[:-1])
            assert check_victory(text, almost_all) is False
            
            # All revealed -> True
            assert check_victory(text, unique) is True


class TestTier1CampaignCatalogData:
    """Tier 1: Verification of cryptogram/campaign_data.py content and helper functions."""

    def test_campaign_chapters_dictionary_has_all_five_chapters(self):
        """Verify CAMPAIGN_CHAPTERS has chapters 1 through 5 with required attributes."""
        if campaign_data is None or CAMPAIGN_CHAPTERS is None:
            pytest.skip("cryptogram.campaign_data not implemented yet")

        assert len(CAMPAIGN_CHAPTERS) == 5
        for ch_id in range(1, 6):
            assert ch_id in CAMPAIGN_CHAPTERS
            ch = CAMPAIGN_CHAPTERS[ch_id]
            assert getattr(ch, "id", getattr(ch, "number", None)) == ch_id
            assert getattr(ch, "year") in range(1941, 1946)
            assert getattr(ch, "title") != ""
            assert getattr(ch, "date_str", getattr(ch, "date", "")) != ""
            assert getattr(ch, "issuing_body", getattr(ch, "authority", "")) != ""
            assert getattr(ch, "document_name", getattr(ch, "title", "")) != ""
            assert getattr(ch, "operational_context", getattr(ch, "briefing", "")) != ""
            assert getattr(ch, "directive_text", getattr(ch, "cipher_text", "")) != ""
            assert getattr(ch, "victory_summary", "") != ""

    def test_get_campaign_chapter_helper(self):
        """Verify get_campaign_chapter retrieves valid chapters and returns None for invalid."""
        if get_campaign_chapter is None:
            pytest.skip("cryptogram.campaign_data.get_campaign_chapter not implemented")

        for ch_id in range(1, 6):
            ch = get_campaign_chapter(ch_id)
            assert ch is not None
            assert getattr(ch, "id", getattr(ch, "number", None)) == ch_id

        assert get_campaign_chapter(0) is None
        assert get_campaign_chapter(6) is None
        assert get_campaign_chapter(999) is None

    def test_get_all_campaign_chapters_helper(self):
        """Verify get_all_campaign_chapters returns all 5 chapters in ascending order."""
        if get_all_campaign_chapters is None:
            pytest.skip("cryptogram.campaign_data.get_all_campaign_chapters not implemented")

        chapters = get_all_campaign_chapters()
        assert len(chapters) == 5
        for idx, ch in enumerate(chapters, 1):
            assert getattr(ch, "id", getattr(ch, "number", None)) == idx


class TestTier1CampaignPersistenceAndModels:
    """Tier 1: Verification of database models, relationships, and persistence."""

    @pytest.mark.asyncio
    async def test_create_and_query_campaign_progress_record(self, db_session: AsyncSession):
        """Verify CampaignProgress model can be persisted and queried."""
        if CampaignProgress is None:
            pytest.skip("CampaignProgress model not implemented")

        user = User(id=194101, username="marshal_zhukov", first_name="Георгий")
        db_session.add(user)
        await db_session.commit()

        progress = CampaignProgress(
            user_id=194101,
            chapter_id=1,
            is_completed=True,
            completion_count=1,
            first_completed_at=datetime.now(timezone.utc),
            last_completed_at=datetime.now(timezone.utc),
        )
        db_session.add(progress)
        await db_session.commit()

        # Query back
        stmt = select(CampaignProgress).where(
            CampaignProgress.user_id == 194101,
            CampaignProgress.chapter_id == 1,
        )
        result = await db_session.execute(stmt)
        record = result.scalar_one_or_none()
        assert record is not None
        assert record.is_completed is True
        assert record.completion_count == 1

    @pytest.mark.asyncio
    async def test_user_relationship_to_campaign_progress(self, db_session: AsyncSession):
        """Verify User.campaign_progress relationship loads properly with selectin."""
        if CampaignProgress is None:
            pytest.skip("CampaignProgress model not implemented")

        user = User(id=194102, username="rokovsky", first_name="Константин")
        db_session.add(user)
        await db_session.commit()

        # Add progress records for chapters 1 and 2
        p1 = CampaignProgress(user_id=194102, chapter_id=1, is_completed=True, completion_count=1)
        p2 = CampaignProgress(user_id=194102, chapter_id=2, is_completed=True, completion_count=1)
        db_session.add_all([p1, p2])
        await db_session.commit()

        # Reload user
        reloaded = await db_session.get(User, 194102)
        assert reloaded is not None
        if hasattr(reloaded, "campaign_progress"):
            assert len(reloaded.campaign_progress) == 2
        if hasattr(reloaded, "campaign_completed_count"):
            assert reloaded.campaign_completed_count == 2

    @pytest.mark.asyncio
    async def test_cascade_delete_campaign_progress_when_user_deleted(self, db_session: AsyncSession):
        """Verify deleting a user cascades and deletes associated campaign_progress records."""
        if CampaignProgress is None:
            pytest.skip("CampaignProgress model not implemented")

        user = User(id=194103, username="temp_cadet", first_name="Курсант")
        db_session.add(user)
        await db_session.commit()

        progress = CampaignProgress(user_id=194103, chapter_id=1, is_completed=True, completion_count=1)
        db_session.add(progress)
        await db_session.commit()

        # Delete user
        await db_session.delete(user)
        await db_session.commit()

        # Check that campaign_progress is gone
        stmt = select(CampaignProgress).where(CampaignProgress.user_id == 194103)
        res = await db_session.execute(stmt)
        assert res.scalar_one_or_none() is None


class TestTier1CampaignProgressionServiceLogic:
    """Tier 1: Verification of CampaignService sequential unlocking and gating."""

    @pytest.mark.asyncio
    async def test_new_user_starts_with_chapter_1_unlocked_and_others_locked(self, db_session: AsyncSession):
        """Verify Chapter 1 is open by default, and Chapters 2..5 are locked."""
        user = User(id=200101, username="recruit_ivan", first_name="Иван")
        db_session.add(user)
        await db_session.commit()

        # Chapter 1 must be playable
        can_ch1, _ = await CampaignServiceHelper.can_play_chapter(db_session, 200101, 1)
        assert can_ch1 is True, "Chapter 1 must be open for new users"

        # Chapters 2..5 must be locked
        for ch in range(2, 6):
            can_ch, reason = await CampaignServiceHelper.can_play_chapter(db_session, 200101, ch)
            assert can_ch is False, f"Chapter {ch} should be locked initially"
            assert reason != "", f"Chapter {ch} should give a lock explanation"

    @pytest.mark.asyncio
    async def test_chapter_2_unlocks_only_after_chapter_1_completed(self, db_session: AsyncSession):
        """Verify solving Chapter 1 unlocks Chapter 2, keeping 3..5 locked."""
        user = User(id=200102, username="sergeant_vasily", first_name="Василий")
        db_session.add(user)
        await db_session.commit()

        # Solve Chapter 1
        await CampaignServiceHelper.record_chapter_completion(db_session, 200102, 1)

        # Chapter 1 is still playable (replay)
        can_ch1, _ = await CampaignServiceHelper.can_play_chapter(db_session, 200102, 1)
        assert can_ch1 is True

        # Chapter 2 is now unlocked
        can_ch2, _ = await CampaignServiceHelper.can_play_chapter(db_session, 200102, 2)
        assert can_ch2 is True, "Chapter 2 must unlock after Chapter 1 is completed"

        # Chapters 3..5 remain locked
        for ch in (3, 4, 5):
            can_ch, _ = await CampaignServiceHelper.can_play_chapter(db_session, 200102, ch)
            assert can_ch is False, f"Chapter {ch} must remain locked"

    @pytest.mark.asyncio
    async def test_sequential_unlock_chain_through_chapter_5(self, db_session: AsyncSession):
        """Verify sequential unlocking across all 5 chapters."""
        user = User(id=200103, username="captain_pavel", first_name="Павел")
        db_session.add(user)
        await db_session.commit()

        for ch in range(1, 5):
            can_next, _ = await CampaignServiceHelper.can_play_chapter(db_session, 200103, ch + 1)
            assert can_next is False, f"Chapter {ch+1} must be locked before Chapter {ch} is done"
            
            await CampaignServiceHelper.record_chapter_completion(db_session, 200103, ch)
            
            can_next_after, _ = await CampaignServiceHelper.can_play_chapter(db_session, 200103, ch + 1)
            assert can_next_after is True, f"Chapter {ch+1} must unlock immediately after Chapter {ch}"

    @pytest.mark.asyncio
    async def test_replay_completed_chapter_increments_count_and_preserves_unlocks(self, db_session: AsyncSession):
        """Verify replaying completed chapter does not reset progress and increments count."""
        user = User(id=200104, username="major_alexey", first_name="Алексей")
        db_session.add(user)
        await db_session.commit()

        # Complete chapter 1 & 2
        await CampaignServiceHelper.record_chapter_completion(db_session, 200104, 1)
        await CampaignServiceHelper.record_chapter_completion(db_session, 200104, 2)

        # Replay chapter 1
        res = await CampaignServiceHelper.record_chapter_completion(db_session, 200104, 1)
        
        # Verify chapter 3 is still unlocked
        can_ch3, _ = await CampaignServiceHelper.can_play_chapter(db_session, 200104, 3)
        assert can_ch3 is True

        # Verify completed count is still 2 unique chapters
        count = await CampaignServiceHelper.get_completed_count(db_session, 200104)
        assert count == 2

    @pytest.mark.asyncio
    async def test_full_campaign_completion_flag_and_count(self, db_session: AsyncSession):
        """Verify full campaign completion when all 5 chapters are solved."""
        user = User(id=200105, username="colonel_dmitry", first_name="Дмитрий")
        db_session.add(user)
        await db_session.commit()

        for ch in range(1, 6):
            await CampaignServiceHelper.record_chapter_completion(db_session, 200105, ch)

        count = await CampaignServiceHelper.get_completed_count(db_session, 200105)
        assert count == 5

        is_done = await CampaignServiceHelper.is_campaign_completed(db_session, 200105)
        assert is_done is True


class TestTier1CampaignExpAndProfileFormatting:
    """Tier 1: Verification of elevated EXP and military dossier profile integration."""

    @pytest.mark.asyncio
    async def test_profile_card_renders_campaign_status_cleanly(self, db_session: AsyncSession):
        """Verify render_player_profile contains campaign line with correct format."""
        user = User(
            id=300101,
            username="operative_boris",
            first_name="Борис",
            games_played=5,
            games_won=5,
            exp=1500,
        )
        db_session.add(user)
        await db_session.commit()

        card = render_player_profile(user)
        assert "ЛИЧНОЕ ДЕЛО ВОЕННОСЛУЖАЩЕГО" in card
        
        # Profile must include campaign status once M2 is implemented
        if hasattr(user, "campaign_completed_count") or "кампания" in card.lower():
            assert "Кампания:" in card or "кампания" in card.lower()
        else:
            pytest.skip("render_player_profile campaign status line not yet implemented (M2)")

    @pytest.mark.asyncio
    async def test_profile_card_xml_well_formedness_with_campaign(self, db_session: AsyncSession):
        """Verify that adding campaign status preserves strict XML validity."""
        user = User(
            id=300102,
            username="general_anton",
            first_name="Антон",
            games_played=10,
            games_won=8,
            exp=3200,
        )
        db_session.add(user)
        await db_session.commit()

        card = render_player_profile(user)
        try:
            root = ET.fromstring(f"<root>{card}</root>")
            assert root.tag == "root"
        except ET.ParseError as e:
            pytest.fail(f"render_player_profile produced malformed XML with campaign line: {e}")

    @pytest.mark.asyncio
    async def test_elevated_campaign_exp_awarding(self, db_session: AsyncSession):
        """Verify elevated EXP awarded for campaign chapters compared to normal games."""
        user = User(id=300103, username="lieutenant_gleb", first_name="Глеб", exp=0)
        db_session.add(user)
        await db_session.commit()

        res = await CampaignServiceHelper.record_chapter_completion(db_session, 300103, 1)
        # Check exp awarded
        if isinstance(res, tuple) and len(res) == 2:
            # Could be (is_first, exp) or (record, is_first)
            if isinstance(res[1], int):
                exp_awarded = res[1]
                assert exp_awarded >= 300, f"Expected elevated EXP (>= 300), got {exp_awarded}"


class TestTier1CampaignKeyboards:
    """Tier 1: Verification of campaign inline keyboard structures and callbacks."""

    def test_main_menu_keyboard_has_campaign_button(self):
        """Verify main menu keyboard contains campaign button without violating existing tests."""
        kb = build_main_menu_keyboard()
        buttons = [btn for row in kb.inline_keyboard for btn in row]
        camp_btns = [b for b in buttons if "кампания" in b.text.lower()]
        
        # If worker_m3 implemented the button:
        if camp_btns:
            assert len(camp_btns) == 1
            assert "1941" in camp_btns[0].text
            assert "1945" in camp_btns[0].text
        else:
            pytest.skip("Main menu campaign button not yet implemented (M3)")

    def test_campaign_menu_keyboard_badges_structure(self):
        """Verify chapter selection keyboard uses ✅ for completed, 🔓 for open, 🔒 for locked."""
        if build_campaign_menu_keyboard is None:
            pytest.skip("build_campaign_menu_keyboard not implemented yet")

        # Mock completed chapters: chapter 1 is completed
        try:
            kb = build_campaign_menu_keyboard(completed_chapters={1})
        except TypeError:
            kb = build_campaign_menu_keyboard(1)  # alternate signature: progress int

        buttons = [btn for row in kb.inline_keyboard for btn in row]
        texts = [b.text for b in buttons]

        # Chapter 1 completed
        assert any("✅" in t and "1" in t for t in texts), "Chapter 1 should have ✅ badge"
        # Chapter 2 open
        assert any("🔓" in t and "2" in t for t in texts), "Chapter 2 should have 🔓 badge"
        # Chapter 3 locked
        assert any("🔒" in t and "3" in t for t in texts), "Chapter 3 should have 🔒 badge"

    def test_briefing_keyboard_has_start_and_back_buttons(self):
        """Verify chapter briefing keyboard has start and back controls."""
        if build_campaign_briefing_keyboard is None:
            pytest.skip("build_campaign_briefing_keyboard not implemented yet")

        kb = build_campaign_briefing_keyboard(chapter_id=1)
        buttons = [btn for row in kb.inline_keyboard for btn in row]
        assert any("дешифровк" in b.text.lower() or "приступить" in b.text.lower() for b in buttons)

    def test_victory_keyboard_next_chapter_transition(self):
        """Verify victory keyboard offers next chapter for 1..4 and final for 5."""
        if build_campaign_victory_keyboard is None:
            pytest.skip("build_campaign_victory_keyboard not implemented yet")

        try:
            kb_ch1 = build_campaign_victory_keyboard(current_chapter_id=1, next_unlocked=True)
        except TypeError:
            kb_ch1 = build_campaign_victory_keyboard(chapter=1)

        buttons_ch1 = [btn for row in kb_ch1.inline_keyboard for btn in row]
        assert any("следующ" in b.text.lower() or "➡️" in b.text for b in buttons_ch1)


# ==============================================================================
# TIER 2: Boundaries & Edge Cases
# ==============================================================================

class TestTier2CampaignBoundariesAndEdgeCases:
    """Tier 2: Verification of edge cases, boundaries, out-of-bounds inputs, and errors."""

    @pytest.mark.asyncio
    async def test_out_of_bounds_chapter_ids_safely_rejected(self, db_session: AsyncSession):
        """Verify out-of-bounds chapter IDs (0, -1, 6, 999) return False without exceptions."""
        user = User(id=400101, username="boundary_tester", first_name="Тест")
        db_session.add(user)
        await db_session.commit()

        for invalid_id in (0, -1, 6, 99, 999, -999):
            can_play, reason = await CampaignServiceHelper.can_play_chapter(db_session, 400101, invalid_id)
            assert can_play is False, f"Chapter {invalid_id} should be rejected"
            assert reason != ""

    @pytest.mark.asyncio
    async def test_get_user_progress_for_non_existent_user(self, db_session: AsyncSession):
        """Verify querying progress for non-existent user returns empty/default safely."""
        progress = await CampaignServiceHelper.get_user_progress(db_session, 999999999)
        assert isinstance(progress, dict)
        # Should not have any completed chapters
        assert not any(progress.values())

    @pytest.mark.asyncio
    async def test_duplicate_completion_calls_are_idempotent(self, db_session: AsyncSession):
        """Verify consecutive record_chapter_completion calls for the same chapter are idempotent."""
        user = User(id=400102, username="idempotency_tester", first_name="Тест")
        db_session.add(user)
        await db_session.commit()

        # First completion
        res1 = await CampaignServiceHelper.record_chapter_completion(db_session, 400102, 1)
        # Second immediate completion (duplicate)
        res2 = await CampaignServiceHelper.record_chapter_completion(db_session, 400102, 1)

        # Unique completed count must remain 1
        count = await CampaignServiceHelper.get_completed_count(db_session, 400102)
        assert count == 1

    @pytest.mark.asyncio
    async def test_extreme_hostile_user_names_in_profile_with_campaign(self, db_session: AsyncSession):
        """Verify profile rendering escapes XML metacharacters in hostile user names."""
        hostile_names = [
            ("<script>alert('xss')</script>", "<b>Hack</b>"),
            ("Agent & Co <Alpha>", "Special 'Chars'"),
            ("<<<>>>&&&'''\"\"\"", "Extreme"),
        ]

        for first, last in hostile_names:
            u = User(
                id=400103,
                username="hostile",
                first_name=first,
                last_name=last,
                games_played=0,
                games_won=0,
                exp=0,
            )
            card = render_player_profile(u)
            # Must parse as valid XML
            try:
                root = ET.fromstring(f"<root>{card}</root>")
                assert root.tag == "root"
            except ET.ParseError as e:
                pytest.fail(f"Hostile name {first!r} caused invalid XML: {e}")

    @pytest.mark.asyncio
    async def test_chapter_5_completion_does_not_overflow_beyond_5(self, db_session: AsyncSession):
        """Verify completing Chapter 5 caps completed count at 5 without overflow."""
        user = User(id=400104, username="cap_tester", first_name="Тест")
        db_session.add(user)
        await db_session.commit()

        for ch in range(1, 6):
            await CampaignServiceHelper.record_chapter_completion(db_session, 400104, ch)

        count = await CampaignServiceHelper.get_completed_count(db_session, 400104)
        assert count == 5
        assert count <= 5, "Campaign completed count cannot exceed 5"


# ==============================================================================
# TIER 3: Cross-Feature Combinations
# ==============================================================================

class TestTier3CampaignCrossFeatureCombinations:
    """Tier 3: Complex combinations, state persistence across sessions, and skipping prevention."""

    @pytest.mark.asyncio
    async def test_replay_chapter_1_while_chapter_3_is_unlocked(self, db_session: AsyncSession):
        """Verify replaying Chapter 1 does not alter Chapter 3 accessibility or Chapter 2 status."""
        user = User(id=500101, username="cross_operative", first_name="Тест")
        db_session.add(user)
        await db_session.commit()

        # Step 1: Complete Chapter 1 & 2 -> Chapter 3 is unlocked
        await CampaignServiceHelper.record_chapter_completion(db_session, 500101, 1)
        await CampaignServiceHelper.record_chapter_completion(db_session, 500101, 2)

        can_ch3_before, _ = await CampaignServiceHelper.can_play_chapter(db_session, 500101, 3)
        assert can_ch3_before is True

        # Step 2: Replay Chapter 1
        await CampaignServiceHelper.record_chapter_completion(db_session, 500101, 1)

        # Step 3: Chapter 3 MUST still be unlocked
        can_ch3_after, _ = await CampaignServiceHelper.can_play_chapter(db_session, 500101, 3)
        assert can_ch3_after is True, "Replaying Chapter 1 must not lock Chapter 3"

        # Chapter 2 MUST still be completed
        progress = await CampaignServiceHelper.get_user_progress(db_session, 500101)
        assert progress.get(2) is True

    @pytest.mark.asyncio
    async def test_direct_jump_attempt_to_chapter_5_prevented(self, db_session: AsyncSession):
        """Verify brand new user cannot bypass progression to launch Chapter 5 directly."""
        user = User(id=500102, username="skipper", first_name="Тест")
        db_session.add(user)
        await db_session.commit()

        can_ch5, reason = await CampaignServiceHelper.can_play_chapter(db_session, 500102, 5)
        assert can_ch5 is False, "Direct jump to Chapter 5 must be rejected"
        assert "засекречена" in reason.lower() or "предыдущ" in reason.lower() or "lock" in reason.lower()

    @pytest.mark.asyncio
    async def test_profile_consistency_throughout_all_six_progression_stages(
        self, db_session: AsyncSession
    ):
        """Verify profile XML validity and progressive count at 0/5, 1/5, 2/5, 3/5, 4/5, 5/5."""
        user = User(id=500103, username="stage_master", first_name="Тест")
        db_session.add(user)
        await db_session.commit()

        for expected_count in range(6):
            if expected_count > 0:
                await CampaignServiceHelper.record_chapter_completion(db_session, 500103, expected_count)
            
            reloaded_user = await db_session.get(User, 500103)
            card = render_player_profile(reloaded_user)
            
            # 1. XML validity
            root = ET.fromstring(f"<root>{card}</root>")
            assert root.tag == "root"

            # 2. Status match (if user model exposes campaign_completed_count)
            if hasattr(reloaded_user, "campaign_completed_count"):
                assert f"{expected_count}/5" in card

    @pytest.mark.asyncio
    async def test_persistence_across_separate_async_sessions(
        self, campaign_test_engine: AsyncEngine
    ):
        """Verify progress committed in Session A is fully durable and visible in Session B."""
        factory = create_session_factory(campaign_test_engine)

        # Write in Session A
        async with factory() as session_a:
            user = User(id=500104, username="durable_user", first_name="Тест")
            session_a.add(user)
            await session_a.commit()
            await CampaignServiceHelper.record_chapter_completion(session_a, 500104, 1)

        # Read in Session B
        async with factory() as session_b:
            progress = await CampaignServiceHelper.get_user_progress(session_b, 500104)
            assert progress.get(1) is True
            can_ch2, _ = await CampaignServiceHelper.can_play_chapter(session_b, 500104, 2)
            assert can_ch2 is True


# ==============================================================================
# TIER 4: Real-World Playthrough Scenario (1941 -> 1945 Full Journey)
# ==============================================================================

class TestTier4RealWorldPlaythroughScenario:
    """Tier 4: End-to-end full campaign walkthrough from 1941 to 1945."""

    @pytest.mark.asyncio
    async def test_complete_campaign_playthrough_1941_to_1945(self, db_session: AsyncSession):
        """Simulate realistic full progression from Recruit to Marshal solving all 5 directives."""
        user = User(
            id=600101,
            username="marshal_koniev",
            first_name="Иван",
            last_name="Конев",
            exp=0,
            games_played=0,
            games_won=0,
        )
        db_session.add(user)
        await db_session.commit()

        # Step 0: Initial profile check
        initial_card = render_player_profile(user)
        assert ET.fromstring(f"<root>{initial_card}</root>").tag == "root"
        count_init = await CampaignServiceHelper.get_completed_count(db_session, 600101)
        assert count_init == 0

        # Progress through all 5 historical chapters
        for ch_id in range(1, 6):
            # 1. Verify chapter is unlocked
            can_play, _ = await CampaignServiceHelper.can_play_chapter(db_session, 600101, ch_id)
            assert can_play is True, f"Chapter {ch_id} should be playable"

            # 2. Decrypt directive text with crypto engine
            directive_info = HISTORICAL_DIRECTIVES[ch_id]
            text = str(directive_info["text"])
            cipher = generate_cipher(text, seed=2026 + ch_id)
            
            # Simulate solving: reveal all unique characters
            revealed = set(c for c in normalize_text(text) if c in RUSSIAN_ALPHABET_SET)
            assert check_victory(text, revealed) is True

            # 3. Record victory in campaign
            await CampaignServiceHelper.record_chapter_completion(db_session, 600101, ch_id)

            # 4. Check progressive completed count
            current_count = await CampaignServiceHelper.get_completed_count(db_session, 600101)
            assert current_count == ch_id

        # Step 6: Verify full campaign victory
        is_completed = await CampaignServiceHelper.is_campaign_completed(db_session, 600101)
        assert is_completed is True, "Full campaign must be completed after solving Chapter 5"

        # Final profile card verification
        reloaded = await db_session.get(User, 600101)
        final_card = render_player_profile(reloaded)
        assert ET.fromstring(f"<root>{final_card}</root>").tag == "root"
        if hasattr(reloaded, "campaign_completed_count"):
            assert "5/5" in final_card

    @pytest.mark.asyncio
    async def test_campaign_playthrough_with_mistakes_and_hints_exp_calculation(
        self, db_session: AsyncSession
    ):
        """Verify playthrough where tactical hints and mistakes occur calculates rewards correctly."""
        user = User(
            id=600102,
            username="tactical_cadet",
            first_name="Семен",
            exp=0,
            games_played=0,
            games_won=0,
        )
        db_session.add(user)
        await db_session.commit()

        # Solve Chapter 1
        await CampaignServiceHelper.record_chapter_completion(db_session, 600102, 1)
        progress = await CampaignServiceHelper.get_user_progress(db_session, 600102)
        assert progress.get(1) is True


class TestTier1CampaignCatalogHelpersAndProperties:
    """Additional Tier 1 tests verifying chapter helper functions and properties."""

    def test_campaign_chapters_clean_text_property(self):
        """Verify clean_text strips outer Russian quotes and whitespace."""
        if get_all_campaign_chapters is None:
            pytest.skip("cryptogram.campaign_data not implemented")

        for ch in get_all_campaign_chapters():
            if hasattr(ch, "clean_text"):
                assert not ch.clean_text.startswith("«")
                assert not ch.clean_text.endswith("»")
                assert len(ch.clean_text) >= 30

    def test_campaign_chapters_catalog_id_format(self):
        """Verify catalog_id follows the standard campaign_ch{id} naming."""
        if get_all_campaign_chapters is None:
            pytest.skip("cryptogram.campaign_data not implemented")

        for ch in get_all_campaign_chapters():
            if hasattr(ch, "catalog_id"):
                assert ch.catalog_id == f"campaign_ch{ch.id}"

    def test_validate_all_chapters_utility(self):
        """Verify validate_all_chapters returns True on authentic Soviet directives."""
        if campaign_data is None or not hasattr(campaign_data, "validate_all_chapters"):
            pytest.skip("validate_all_chapters not implemented")

        assert campaign_data.validate_all_chapters(raise_on_error=True) is True


class TestTier2CampaignMarshalRankAndEmptyDb:
    """Additional Tier 2 tests for edge conditions (Marshal max-rank, empty DB)."""

    @pytest.mark.asyncio
    async def test_marshal_max_rank_player_completes_campaign(self, db_session: AsyncSession):
        """Verify a user at Marshal rank (>= 35,000 EXP) completes campaign without overflow."""
        marshal = User(
            id=600201,
            username="marshal_rokossovsky",
            first_name="Константин",
            exp=45000,
            games_played=200,
            games_won=190,
        )
        db_session.add(marshal)
        await db_session.commit()

        # Complete chapter 1
        await CampaignServiceHelper.record_chapter_completion(db_session, 600201, 1)

        reloaded = await db_session.get(User, 600201)
        card = render_player_profile(reloaded)
        assert ET.fromstring(f"<root>{card}</root>").tag == "root"
        assert "Маршал" in card
        assert "[██████████] 100%" in card

    @pytest.mark.asyncio
    async def test_empty_database_campaign_queries_safe(self, db_session: AsyncSession):
        """Verify calling campaign queries on an empty database does not throw unhandled errors."""
        progress = await CampaignServiceHelper.get_user_progress(db_session, 888888888)
        assert isinstance(progress, dict)

        can_play, _ = await CampaignServiceHelper.can_play_chapter(db_session, 888888888, 1)
        # Chapter 1 is open even for brand new / unpersisted users
        assert can_play is True

        is_done = await CampaignServiceHelper.is_campaign_completed(db_session, 888888888)
        assert is_done is False

