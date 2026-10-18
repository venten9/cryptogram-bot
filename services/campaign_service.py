"""Story Campaign service layer, business logic, progression gating, and elevated rewards.

«Сюжетная кампания: Секретные директивы Ставки (1941–1945)»

Features:
- Sequential progression gating: Chapter 1 open by default; Chapter N+1 unlocks exclusively
  after Chapter N is completed.
- Replayability without progress loss: completed chapters can be replayed infinitely
  incrementing completion_count while preserving unlock states of later chapters.
- Full campaign completion detection (all 5 chapters completed).
- Elevated EXP reward calculation: base 400 EXP (+200 first completion bonus, +100 clean work,
  +100 autonomy, +1000 chapter 5 grand triumph, 200 replay base).
- Backward-compatible static and instance method dispatch.
"""
import asyncio
import enum
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Union

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from database.models import CampaignProgress, GameSession, SessionStatus, User
from services.lock_manager import SessionLockManager

TOTAL_CAMPAIGN_CHAPTERS: int = 5
_campaign_lock_manager = SessionLockManager()


class ChapterAccessStatus(str, enum.Enum):
    """UI presentation and accessibility status of a campaign chapter."""
    COMPLETED = "COMPLETED"  # ✅ Chapter already solved (replayable)
    UNLOCKED = "UNLOCKED"    # 🔓 Currently available for decryption
    LOCKED = "LOCKED"        # 🔒 Classified / locked until prior chapter completed


@dataclass(frozen=True)
class ChapterState:
    """State descriptor for an individual chapter in the user's campaign."""
    chapter_id: int
    title: str
    status: ChapterAccessStatus
    is_completed: bool
    completion_count: int
    first_completed_at: Optional[datetime]
    last_completed_at: Optional[datetime]


@dataclass(frozen=True)
class UserCampaignSummary:
    """Aggregated campaign status for a user."""
    user_id: int
    completed_count: int        # 0 to 5
    total_chapters: int         # 5
    is_campaign_completed: bool
    current_active_chapter: int  # Lowest uncompleted unlocked chapter (1..5), or 5 if all done
    chapters: Dict[int, ChapterState]
    status_display: str         # e.g. "3/5 глав"


class CampaignService:
    """Business logic service managing story campaign progression, unlocking, and victory rewards."""

    def __init__(
        self,
        session_factory: Optional[async_sessionmaker[AsyncSession]] = None,
        session_service: Optional[Any] = None,
    ) -> None:
        self.session_factory = session_factory
        self.session_service = session_service

    @asynccontextmanager
    async def _get_db(self, db_session: Optional[AsyncSession] = None):
        """Yield the provided session or open a new one from self.session_factory."""
        if db_session is not None:
            yield db_session
        elif self.session_factory is not None:
            async with self.session_factory() as session:
                yield session
        else:
            raise ValueError("No database session or session_factory provided.")

    @staticmethod
    def calculate_campaign_exp(
        chapter_id: int,
        is_first_completion: bool = True,
        mistakes_count: int = 0,
        hints_used: int = 0,
    ) -> int:
        """Calculate elevated experience points for campaign chapter victory.

        Base: 400 EXP (or 200 EXP on replay).
        First-time victory bonus: +200 EXP.
        Clean work bonus (0 mistakes): +100 EXP.
        Autonomy bonus (0 hints): +100 EXP.
        Grand campaign triumph (Chapter 5 first-time): +1,000 EXP.
        """
        base = 400 if is_first_completion else 200
        first_bonus = 200 if is_first_completion else 0
        clean_bonus = 100 if mistakes_count == 0 else 0
        autonomy_bonus = 100 if hints_used == 0 else 0
        campaign_victory_bonus = 1000 if (is_first_completion and chapter_id == 5) else 0
        return base + first_bonus + clean_bonus + autonomy_bonus + campaign_victory_bonus

    @staticmethod
    async def can_play_chapter(
        db: Optional[AsyncSession] = None,
        user_id: Optional[int] = None,
        chapter_id: Optional[int] = None,
        *,
        db_session: Optional[AsyncSession] = None,
        **kwargs,
    ) -> Tuple[bool, str]:
        """Verify whether a user is authorized to play chapter_id.

        Sequential rules:
        - Out-of-bounds chapter IDs (< 1 or > 5) are rejected.
        - Chapter 1 is always accessible to all users.
        - Chapter N (2..5) unlocks ONLY if Chapter N-1 has is_completed=True.
        - Completed chapters can always be replayed without restriction.

        Returns:
            Tuple[bool, str]: (can_play, lock_reason)
        """
        session = db_session if db_session is not None else (db if isinstance(db, AsyncSession) else None)
        if isinstance(db, int):
            target_user_id = db
            target_chapter_id = user_id if isinstance(user_id, int) else kwargs.get("chapter_id", 0)
        else:
            target_user_id = user_id if user_id is not None else kwargs.get("user_id", 0)
            target_chapter_id = chapter_id if chapter_id is not None else kwargs.get("chapter_id", 0)

        if target_chapter_id < 1 or target_chapter_id > TOTAL_CAMPAIGN_CHAPTERS:
            return (
                False,
                f"Недопустимый номер главы: {target_chapter_id}. В кампании доступны главы с 1 по {TOTAL_CAMPAIGN_CHAPTERS}.",
            )

        if target_chapter_id == 1:
            return (True, "")

        if session is not None and target_user_id:
            stmt = select(CampaignProgress.id).where(
                CampaignProgress.user_id == target_user_id,
                CampaignProgress.chapter_id == target_chapter_id - 1,
                CampaignProgress.is_completed.is_(True),
            )
            res = await session.execute(stmt)
            prev_completed = res.scalar_one_or_none() is not None
            if prev_completed:
                return (True, "")

        return (
            False,
            f"🔒 Глава {target_chapter_id} засекречена. Сначала расшифруйте предыдущую директиву (Глава {target_chapter_id - 1})!",
        )

    @staticmethod
    async def get_user_progress(
        db: Optional[AsyncSession] = None,
        user_id: Optional[int] = None,
        *,
        db_session: Optional[AsyncSession] = None,
        **kwargs,
    ) -> Dict[int, bool]:
        """Fetch completion dictionary for all campaign chapters {chapter_id: is_completed}."""
        session = db_session if db_session is not None else (db if isinstance(db, AsyncSession) else None)
        target_user_id = user_id if user_id is not None else (db if isinstance(db, int) else kwargs.get("user_id", 0))

        result = {ch: False for ch in range(1, TOTAL_CAMPAIGN_CHAPTERS + 1)}
        if session is not None and target_user_id:
            stmt = select(CampaignProgress.chapter_id, CampaignProgress.is_completed).where(
                CampaignProgress.user_id == target_user_id,
                CampaignProgress.chapter_id.between(1, TOTAL_CAMPAIGN_CHAPTERS),
            )
            rows = (await session.execute(stmt)).all()
            for ch_id, is_comp in rows:
                result[ch_id] = bool(is_comp)
        return result

    @staticmethod
    async def get_completed_count(
        db: Optional[AsyncSession] = None,
        user_id: Optional[int] = None,
        *,
        db_session: Optional[AsyncSession] = None,
        **kwargs,
    ) -> int:
        """Count unique completed chapters (0..5) for user."""
        progress = await CampaignService.get_user_progress(db=db, user_id=user_id, db_session=db_session, **kwargs)
        return min(sum(1 for comp in progress.values() if comp), TOTAL_CAMPAIGN_CHAPTERS)

    @staticmethod
    async def is_campaign_completed(
        db: Optional[AsyncSession] = None,
        user_id: Optional[int] = None,
        *,
        db_session: Optional[AsyncSession] = None,
        **kwargs,
    ) -> bool:
        """Check if user has completed all 5 campaign chapters."""
        count = await CampaignService.get_completed_count(db=db, user_id=user_id, db_session=db_session, **kwargs)
        return count >= TOTAL_CAMPAIGN_CHAPTERS

    @staticmethod
    async def get_chapter_status(
        db: Optional[AsyncSession] = None,
        user_id: Optional[int] = None,
        chapter_id: Optional[int] = None,
        *,
        db_session: Optional[AsyncSession] = None,
        **kwargs,
    ) -> ChapterAccessStatus:
        """Get the current UI access status of a chapter (COMPLETED, UNLOCKED, or LOCKED)."""
        session = db_session if db_session is not None else (db if isinstance(db, AsyncSession) else None)
        if isinstance(db, int):
            target_user_id = db
            target_chapter_id = user_id if isinstance(user_id, int) else kwargs.get("chapter_id", 0)
        else:
            target_user_id = user_id if user_id is not None else kwargs.get("user_id", 0)
            target_chapter_id = chapter_id if chapter_id is not None else kwargs.get("chapter_id", 0)

        if target_chapter_id < 1 or target_chapter_id > TOTAL_CAMPAIGN_CHAPTERS:
            return ChapterAccessStatus.LOCKED

        progress = await CampaignService.get_user_progress(session, target_user_id)
        if progress.get(target_chapter_id, False):
            return ChapterAccessStatus.COMPLETED

        can_play, _ = await CampaignService.can_play_chapter(session, target_user_id, target_chapter_id)
        if can_play:
            return ChapterAccessStatus.UNLOCKED
        return ChapterAccessStatus.LOCKED

    @staticmethod
    async def record_chapter_completion(
        db: Optional[AsyncSession] = None,
        user_id: Optional[int] = None,
        chapter_id: Optional[int] = None,
        mistakes_count: int = 0,
        hints_used: int = 0,
        *,
        db_session: Optional[AsyncSession] = None,
        **kwargs,
    ) -> Tuple[bool, int]:
        """Atomically record chapter completion, update timestamps, and award elevated EXP.

        Returns:
            Tuple[bool, int]: (is_first_completion, exp_awarded)
        """
        session = db_session if db_session is not None else (db if isinstance(db, AsyncSession) else None)
        if session is None:
            raise ValueError("AsyncSession must be provided to record_chapter_completion")

        if isinstance(db, int):
            target_user_id = db
            target_chapter_id = user_id if isinstance(user_id, int) else kwargs.get("chapter_id", 0)
        else:
            target_user_id = user_id if user_id is not None else kwargs.get("user_id", 0)
            target_chapter_id = chapter_id if chapter_id is not None else kwargs.get("chapter_id", 0)

        if target_chapter_id < 1 or target_chapter_id > TOTAL_CAMPAIGN_CHAPTERS:
            raise ValueError(
                f"Недопустимый номер главы: {target_chapter_id}. "
                f"Номер главы должен быть от 1 до {TOTAL_CAMPAIGN_CHAPTERS}."
            )

        mistakes = kwargs.get("mistakes_count", mistakes_count)
        hints = kwargs.get("hints_used", hints_used)

        async with _campaign_lock_manager.lock(f"campaign_progress:{target_user_id}:{target_chapter_id}"):
            now = datetime.now(timezone.utc)
            max_retries = 10
            for attempt in range(max_retries):
                stmt = select(CampaignProgress).where(
                    CampaignProgress.user_id == target_user_id,
                    CampaignProgress.chapter_id == target_chapter_id,
                )
                res = await session.execute(stmt)
                record = res.scalar_one_or_none()

                if record is None:
                    is_first_completion = True
                    record = CampaignProgress(
                        user_id=target_user_id,
                        chapter_id=target_chapter_id,
                        is_completed=True,
                        completion_count=1,
                        first_completed_at=now,
                        last_completed_at=now,
                    )
                    session.add(record)
                else:
                    if not record.is_completed:
                        is_first_completion = True
                        record.is_completed = True
                        record.first_completed_at = record.first_completed_at or now
                    else:
                        is_first_completion = False
                    record.completion_count += 1
                    record.last_completed_at = now

                exp_awarded = CampaignService.calculate_campaign_exp(
                    chapter_id=target_chapter_id,
                    is_first_completion=is_first_completion,
                    mistakes_count=mistakes,
                    hints_used=hints,
                )

                user = await session.get(User, target_user_id)
                if user is not None:
                    user.exp = (user.exp or 0) + exp_awarded
                    user.games_played = (user.games_played or 0) + 1
                    user.games_won = (user.games_won or 0) + 1
                    user.total_mistakes = (user.total_mistakes or 0) + mistakes
                    user.total_hints_used = (user.total_hints_used or 0) + hints

                try:
                    await session.commit()
                    return (is_first_completion, exp_awarded)
                except IntegrityError:
                    await session.rollback()
                    if attempt == max_retries - 1:
                        raise
                    await asyncio.sleep(0.01 * (attempt + 1))

    @staticmethod
    async def get_user_campaign_summary(
        db: Optional[AsyncSession] = None,
        user_id: Optional[int] = None,
        *,
        db_session: Optional[AsyncSession] = None,
        **kwargs,
    ) -> UserCampaignSummary:
        """Aggregate full campaign state, chapter access statuses, and dossier display string."""
        session = db_session if db_session is not None else (db if isinstance(db, AsyncSession) else None)
        target_user_id = user_id if user_id is not None else (db if isinstance(db, int) else kwargs.get("user_id", 0))

        from cryptogram.campaign_data import CAMPAIGN_CHAPTERS

        progress_records: Dict[int, CampaignProgress] = {}
        if session is not None and target_user_id:
            stmt = select(CampaignProgress).where(CampaignProgress.user_id == target_user_id)
            res = await session.execute(stmt)
            for r in res.scalars():
                progress_records[r.chapter_id] = r

        chapters_dict: Dict[int, ChapterState] = {}
        completed_count = 0

        for ch_id in range(1, TOTAL_CAMPAIGN_CHAPTERS + 1):
            rec = progress_records.get(ch_id)
            is_comp = bool(rec.is_completed) if rec else False
            comp_count = rec.completion_count if rec else 0
            first_at = rec.first_completed_at if rec else None
            last_at = rec.last_completed_at if rec else None

            if is_comp:
                status = ChapterAccessStatus.COMPLETED
                completed_count += 1
            elif ch_id == 1 or (ch_id - 1 in progress_records and progress_records[ch_id - 1].is_completed):
                status = ChapterAccessStatus.UNLOCKED
            else:
                status = ChapterAccessStatus.LOCKED

            ch_obj = CAMPAIGN_CHAPTERS.get(ch_id)
            title = ch_obj.title if ch_obj else f"Глава {ch_id}"

            chapters_dict[ch_id] = ChapterState(
                chapter_id=ch_id,
                title=title,
                status=status,
                is_completed=is_comp,
                completion_count=comp_count,
                first_completed_at=first_at,
                last_completed_at=last_at,
            )

        current_active = TOTAL_CAMPAIGN_CHAPTERS
        for ch_id in range(1, TOTAL_CAMPAIGN_CHAPTERS + 1):
            if chapters_dict[ch_id].status == ChapterAccessStatus.UNLOCKED:
                current_active = ch_id
                break

        return UserCampaignSummary(
            user_id=target_user_id,
            completed_count=completed_count,
            total_chapters=TOTAL_CAMPAIGN_CHAPTERS,
            is_campaign_completed=(completed_count >= TOTAL_CAMPAIGN_CHAPTERS),
            current_active_chapter=current_active,
            chapters=chapters_dict,
            status_display=f"{completed_count}/{TOTAL_CAMPAIGN_CHAPTERS} глав",
        )

    @staticmethod
    async def create_campaign_session(
        db: Optional[AsyncSession] = None,
        user_tg_id: int = 0,
        chapter_id: int = 1,
        username: Optional[str] = None,
        first_name: str = "",
        last_name: Optional[str] = None,
        seed: Optional[int] = None,
        *,
        db_session: Optional[AsyncSession] = None,
        **kwargs,
    ) -> GameSession:
        """Instantiate a solo GameSession configured for a specific story campaign chapter."""
        session = db_session if db_session is not None else (db if isinstance(db, AsyncSession) else None)
        if session is None:
            raise ValueError("AsyncSession must be provided to create_campaign_session")

        can_play, reason = await CampaignService.can_play_chapter(session, user_tg_id, chapter_id)
        if not can_play:
            raise ValueError(reason or f"Глава {chapter_id} недоступна для запуска.")

        from cryptogram.campaign_data import get_campaign_chapter
        ch = get_campaign_chapter(chapter_id)
        if ch is None:
            raise ValueError(f"Глава {chapter_id} не найдена в каталоге кампании.")

        from cryptogram.engine import generate_cipher
        cipher = generate_cipher(ch.clean_text, seed=seed)

        stmt = select(User).where(User.id == user_tg_id)
        user = (await session.execute(stmt)).scalar_one_or_none()
        if user is None:
            user = User(id=user_tg_id, username=username, first_name=first_name, last_name=last_name)
            session.add(user)
            await session.flush()

        import secrets
        invite_code = secrets.token_hex(4).lower()
        for _ in range(10):
            res = await session.execute(select(GameSession.id).where(GameSession.invite_code == invite_code))
            if res.scalar_one_or_none() is None:
                break
            invite_code = secrets.token_hex(4).lower()

        game = GameSession(
            invite_code=invite_code,
            analyst_id=user.id,
            agent_id=user.id,
            status=SessionStatus.IN_PROGRESS,
            original_text=cipher.original_text,
            normalized_text=cipher.normalized_text,
            cipher_mapping=cipher.char_to_code,
            guessed_mapping={},
            is_custom_quote=False,
            is_solo=True,
            catalog_id=ch.catalog_id,
            campaign_chapter=chapter_id,
            total_unique_letters=len(cipher.char_to_code),
        )
        session.add(game)
        await session.commit()
        await session.refresh(game)
        return game


__all__ = [
    "TOTAL_CAMPAIGN_CHAPTERS",
    "ChapterAccessStatus",
    "ChapterState",
    "UserCampaignSummary",
    "CampaignService",
]
