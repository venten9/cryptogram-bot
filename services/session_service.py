"""High-level game session lifecycle and business logic service."""
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cryptogram.catalog import get_catalog_item
from cryptogram.engine import (
    calculate_frequencies,
    check_victory,
    generate_cipher,
    get_unrevealed_codes,
    normalize_text,
    render_mask,
    validate_text,
)
from cryptogram.models import GuessResult, GuessStatus
from cryptogram.ranks import ExpBreakdown, calculate_mission_exp
from database.models import GameSession, HintLog, HintType, SessionStatus, User
from services.lock_manager import SessionLockManager


# ==========================================
# Domain Exceptions
# ==========================================

class SessionError(Exception):
    """Base exception for all game session domain errors."""
    pass


class SessionNotFoundError(SessionError, ValueError):
    """Raised when a requested session is not found."""
    pass


class SessionAlreadyStartedError(SessionError, ValueError):
    """Raised when a player attempts to join a session that is already in progress."""
    pass


SessionAlreadyFull = SessionAlreadyStartedError  # Semantic alias


class SessionInvalidStateError(SessionError, ValueError):
    """Raised when an operation cannot be performed in the current session state."""
    pass


InvalidSessionStateError = SessionInvalidStateError  # Semantic alias


class SessionFinishedError(SessionError, ValueError):
    """Raised when an operation is attempted on an already solved/cancelled session."""
    pass


SessionInactiveError = SessionFinishedError  # Semantic alias


class SelfJoinError(SessionError, ValueError):
    """Raised when the session creator (Analyst) attempts to join as Agent."""
    pass


CreatorCannotJoinAsAgent = SelfJoinError  # Semantic alias


class UnauthorizedActionError(SessionError, PermissionError):
    """Raised when an action is performed by a user without permission for that role."""
    pass


UnauthorizedSessionAction = UnauthorizedActionError  # Semantic alias


class InvalidGuessError(SessionError, ValueError):
    """Raised when a guess attempt has invalid syntax or arguments."""
    pass


class InvalidHintError(SessionError, ValueError):
    """Raised when hint parameters or state are invalid."""
    pass


# ==========================================
# Result Wrappers for Flexible Unpacking
# ==========================================

class SessionGuessResult(GuessResult):
    """Enhanced GuessResult supporting dictionary indexing, tuple unpacking, and session reference."""
    game: GameSession

    def __init__(
        self,
        status: GuessStatus,
        letter: str,
        symbol_code: int,
        is_victory: bool,
        revealed_letters: Set[str],
        message: str,
        game: GameSession,
    ) -> None:
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "letter", letter)
        object.__setattr__(self, "symbol_code", symbol_code)
        object.__setattr__(self, "is_victory", is_victory)
        object.__setattr__(self, "revealed_letters", revealed_letters)
        object.__setattr__(self, "message", message)
        object.__setattr__(self, "game", game)

    @property
    def is_correct(self) -> bool:
        return self.status in (GuessStatus.CORRECT, GuessStatus.VICTORY)

    def __iter__(self):
        yield self.game
        yield self

    def __getitem__(self, item: str) -> Any:
        if item == "solved":
            return self.is_victory
        if item == "status":
            return self.status.value if hasattr(self.status, "value") else str(self.status)
        if hasattr(self, item):
            return getattr(self, item)
        raise KeyError(item)


class SessionHintResult:
    """Enhanced HintLog result wrapper supporting dictionary indexing, tuple unpacking, and session reference."""

    def __init__(self, hint_log: HintLog, game: GameSession, status: str = "HINT_APPLIED") -> None:
        self.hint_log = hint_log
        self.game = game
        self.status = status

    @property
    def revealed_code(self) -> Optional[int]:
        return self.hint_log.symbol_code

    @property
    def symbol_code(self) -> Optional[int]:
        return self.hint_log.symbol_code

    @property
    def revealed_letter(self) -> Optional[str]:
        return self.hint_log.letter

    @property
    def letter(self) -> Optional[str]:
        return self.hint_log.letter

    @property
    def is_victory(self) -> bool:
        """True if the hint led to game completion."""
        return self.game.status == SessionStatus.SOLVED

    @property
    def is_solved(self) -> bool:
        """Alias for is_victory."""
        return self.game.status == SessionStatus.SOLVED

    def __iter__(self):
        yield self.game
        yield self.hint_log

    def __getitem__(self, item: str) -> Any:
        if item == "status":
            return self.status
        if hasattr(self.hint_log, item):
            val = getattr(self.hint_log, item)
            if hasattr(val, "value"):
                return val.value
            return val
        if hasattr(self, item):
            return getattr(self, item)
        raise KeyError(item)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.hint_log, name)

    def __repr__(self) -> str:
        return f"<SessionHintResult status={self.status} hint_log={self.hint_log}>"


# ==========================================
# SessionService Implementation
# ==========================================

class SessionService:
    """Manages game room creation, player joins, guess submissions, hints, and victory evaluations."""

    def __init__(
        self,
        session_factory: Optional[async_sessionmaker[AsyncSession]] = None,
        lock_manager: Optional[SessionLockManager] = None,
    ) -> None:
        self.session_factory = session_factory
        self.lock_manager = lock_manager or SessionLockManager()
        self._code_to_id: Dict[str, int] = {}

    def _register_session_cache(self, game: Optional[GameSession]) -> None:
        """Register game session identifiers in cache for fast lock key canonicalization."""
        if game is not None and game.id is not None:
            self._code_to_id[str(game.id)] = game.id
            if game.invite_code:
                self._code_to_id[game.invite_code.strip().lower()] = game.id
            if game.room_uuid:
                self._code_to_id[str(game.room_uuid).strip().lower()] = game.id
                self._code_to_id[str(game.room_uuid).strip()] = game.id

    async def _get_canonical_session_id(
        self,
        session_id_or_code: Union[str, int],
        db_session: Optional[AsyncSession] = None,
    ) -> Optional[int]:
        """Resolve any session identifier (integer PK, invite code, room UUID) to the canonical integer PK."""
        if isinstance(session_id_or_code, int):
            self._code_to_id[str(session_id_or_code)] = session_id_or_code
            return session_id_or_code

        clean = str(session_id_or_code).strip()
        clean_lower = clean.lower()

        if clean_lower in self._code_to_id:
            return self._code_to_id[clean_lower]
        if clean in self._code_to_id:
            return self._code_to_id[clean]

        async with self._get_db(db_session) as db:
            if clean.isdigit():
                stmt = select(GameSession.id, GameSession.invite_code, GameSession.room_uuid).where(
                    (GameSession.id == int(clean)) | (GameSession.invite_code == clean_lower)
                )
            elif len(clean_lower) == 8 and "-" not in clean_lower:
                stmt = select(GameSession.id, GameSession.invite_code, GameSession.room_uuid).where(
                    GameSession.invite_code == clean_lower
                )
            elif len(clean) == 36 and "-" in clean:
                stmt = select(GameSession.id, GameSession.invite_code, GameSession.room_uuid).where(
                    GameSession.room_uuid == clean
                )
            else:
                stmt = select(GameSession.id, GameSession.invite_code, GameSession.room_uuid).where(
                    (GameSession.invite_code == clean_lower) | (GameSession.room_uuid == clean)
                )

            res = await db.execute(stmt)
            row = res.first()
            if row is not None:
                gid, inv, uuid_val = row
                self._code_to_id[str(gid)] = gid
                if inv:
                    self._code_to_id[inv.strip().lower()] = gid
                if uuid_val:
                    self._code_to_id[str(uuid_val).strip().lower()] = gid
                    self._code_to_id[str(uuid_val).strip()] = gid
                return gid

        if clean.isdigit():
            return int(clean)

        return None

    @asynccontextmanager
    async def _get_db(self, db_session: Optional[AsyncSession] = None):
        """Context manager yielding provided session or opening a fresh one from session_factory."""
        if db_session is not None:
            yield db_session
        elif self.session_factory is not None:
            async with self.session_factory() as session:
                yield session
        else:
            raise RuntimeError("Neither db_session nor session_factory is configured for SessionService.")

    async def _resolve_session(
        self,
        db: AsyncSession,
        session_id_or_code: Union[str, int],
    ) -> Optional[GameSession]:
        """Lookup GameSession by Integer PK, 8-character invite code, or 36-char room UUID."""
        if isinstance(session_id_or_code, int):
            stmt = select(GameSession).where(GameSession.id == session_id_or_code)
        else:
            val = str(session_id_or_code).strip()
            if val.isdigit():
                stmt = select(GameSession).where(
                    (GameSession.id == int(val)) | (GameSession.invite_code == val.lower())
                )
            elif len(val) == 8 and "-" not in val:
                stmt = select(GameSession).where(GameSession.invite_code == val.lower())
            elif len(val) == 36 and "-" in val:
                stmt = select(GameSession).where(GameSession.room_uuid == val)
            else:
                stmt = select(GameSession).where(
                    (GameSession.invite_code == val.lower()) | (GameSession.room_uuid == val)
                )
        res = await db.execute(stmt)
        game = res.scalar_one_or_none()
        if game is not None:
            self._register_session_cache(game)
        return game

    async def _generate_unique_invite_code(self, db: AsyncSession) -> str:
        """Generate a collision-free 8-character hex invite code."""
        for _ in range(10):
            code = secrets.token_hex(4).lower()
            stmt = select(GameSession.id).where(GameSession.invite_code == code)
            res = await db.execute(stmt)
            if res.scalar_one_or_none() is None:
                return code
        raise RuntimeError("Failed to generate unique invite code after 10 attempts.")

    # ------------------------------------------------------------------
    # User Management
    # ------------------------------------------------------------------

    async def get_or_create_user(
        self,
        telegram_id: int,
        username: Optional[str] = None,
        first_name: str = "",
        last_name: Optional[str] = None,
        db_session: Optional[AsyncSession] = None,
    ) -> User:
        """Fetch existing user by 64-bit Telegram ID or register a new user entity."""
        async with self._get_db(db_session) as db:
            stmt = select(User).where(User.id == telegram_id)
            res = await db.execute(stmt)
            user = res.scalar_one_or_none()
            if user is None:
                user = User(
                    id=telegram_id,
                    username=username,
                    first_name=first_name,
                    last_name=last_name,
                )
                db.add(user)
                await db.commit()
                await db.refresh(user)
            else:
                changed = False
                if username is not None and user.username != username:
                    user.username = username
                    changed = True
                if first_name and user.first_name != first_name:
                    user.first_name = first_name
                    changed = True
                if last_name is not None and user.last_name != last_name:
                    user.last_name = last_name
                    changed = True
                if changed:
                    await db.commit()
                    await db.refresh(user)
            return user

    # ------------------------------------------------------------------
    # Game Room Lifecycle Operations
    # ------------------------------------------------------------------

    async def create_session(
        self,
        creator_tg_id: int,
        text: str,
        is_custom: bool = False,
        catalog_id: Optional[str] = None,
        seed: Optional[int] = None,
        username: Optional[str] = None,
        first_name: str = "",
        last_name: Optional[str] = None,
        db_session: Optional[AsyncSession] = None,
    ) -> GameSession:
        """Create a new game room with bijective cipher mapping in WAITING_AGENT status."""
        validation = validate_text(text)
        if not validation.is_valid:
            raise ValueError(validation.error_message or "Текст не подходит для создания шифрограммы.")

        cipher_data = generate_cipher(text, seed=seed)

        async with self._get_db(db_session) as db:
            creator = await self.get_or_create_user(
                telegram_id=creator_tg_id,
                username=username,
                first_name=first_name,
                last_name=last_name,
                db_session=db,
            )
            invite_code = await self._generate_unique_invite_code(db)

            game = GameSession(
                invite_code=invite_code,
                analyst_id=creator.id,
                status=SessionStatus.WAITING_AGENT,
                original_text=cipher_data.original_text,
                normalized_text=cipher_data.normalized_text,
                cipher_mapping=cipher_data.char_to_code,
                guessed_mapping={},
                is_custom_quote=is_custom,
                catalog_id=catalog_id,
                total_unique_letters=len(cipher_data.char_to_code),
            )
            db.add(game)
            await db.commit()
            await db.refresh(game)
            self._register_session_cache(game)
            return game

    async def create_solo_session(
        self,
        player_tg_id: int,
        difficulty: str = "MEDIUM",
        catalog_id: Optional[str] = None,
        custom_text: Optional[str] = None,
        seed: Optional[int] = None,
        username: Optional[str] = None,
        first_name: str = "",
        last_name: Optional[str] = None,
        db_session: Optional[AsyncSession] = None,
    ) -> GameSession:
        """Create a solo game room where player acts as Agent against automated Headquarters.

        Session immediately begins in IN_PROGRESS status.
        Automated HQ bot is assigned as analyst, and player is assigned as agent.
        """
        import random
        from cryptogram.catalog import get_catalog, get_catalog_item

        text: str
        cat_id: Optional[str] = None
        is_custom = False

        if custom_text:
            text = custom_text
            is_custom = True
        elif catalog_id:
            cat_item = get_catalog_item(catalog_id)
            if not cat_item:
                raise ValueError(f"Депеша с идентификатором '{catalog_id}' не найдена в каталоге.")
            text = cat_item.text
            cat_id = cat_item.id
        else:
            catalog = get_catalog()
            diff_upper = difficulty.strip().upper() if difficulty else "MEDIUM"
            if diff_upper in ("EASY", "MEDIUM", "HARD"):
                candidates = [c for c in catalog if c.difficulty == diff_upper]
            else:
                candidates = catalog
            if not candidates:
                candidates = catalog
            cat_item = random.choice(candidates)
            text = cat_item.text
            cat_id = cat_item.id

        validation = validate_text(text)
        if not validation.is_valid:
            raise ValueError(validation.error_message or "Текст не подходит для создания шифрограммы.")

        cipher_data = generate_cipher(text, seed=seed)

        async with self._get_db(db_session) as db:
            # Player is the field agent
            player = await self.get_or_create_user(
                telegram_id=player_tg_id,
                username=username,
                first_name=first_name,
                last_name=last_name,
                db_session=db,
            )

            # Automated Headquarters bot user (ID = 0)
            hq_bot = await self.get_or_create_user(
                telegram_id=0,
                username="hq_analyst",
                first_name="Штаб-квартира (ИИ)",
                db_session=db,
            )

            invite_code = await self._generate_unique_invite_code(db)

            game = GameSession(
                invite_code=invite_code,
                analyst_id=hq_bot.id,
                agent_id=player.id,
                status=SessionStatus.IN_PROGRESS,
                original_text=cipher_data.original_text,
                normalized_text=cipher_data.normalized_text,
                cipher_mapping=cipher_data.char_to_code,
                guessed_mapping={},
                is_custom_quote=is_custom,
                catalog_id=cat_id,
                is_solo=True,
                total_unique_letters=len(cipher_data.char_to_code),
            )
            # Record participation for player
            player.games_played += 1
            player.games_as_agent += 1

            db.add(game)
            await db.commit()
            await db.refresh(game)
            self._register_session_cache(game)
            return game

    async def join_session(
        self,
        invite_code: str,
        agent_tg_id: int,
        username: Optional[str] = None,
        first_name: str = "",
        db_session: Optional[AsyncSession] = None,
    ) -> GameSession:
        """Join an existing waiting game session as the Field Agent.

        Validates session exists, is WAITING_AGENT, and agent is not creator.
        Transitions status to IN_PROGRESS and increments games_played statistics.
        """
        clean_code = invite_code.strip().lower()
        canonical_id = self._code_to_id.get(clean_code)
        lock_key = f"session:{canonical_id}" if canonical_id is not None else f"join:{clean_code}"

        # Concurrency barrier preventing double-joins across distinct coroutines
        async with self.lock_manager.lock(lock_key):
            async with self._get_db(db_session) as db:
                game = await self._resolve_session(db, clean_code)
                if game is None:
                    raise SessionNotFoundError(f"SESSION_NOT_FOUND: Игра с кодом '{invite_code}' не найдена.")

                if game.status == SessionStatus.IN_PROGRESS:
                    raise SessionAlreadyStartedError("SESSION_ALREADY_TAKEN: К этой игре уже подключился другой Агент.")
                if game.status in (SessionStatus.SOLVED, SessionStatus.CANCELLED):
                    raise SessionFinishedError(f"SESSION_INACTIVE: Игра уже завершена (статус: {game.status.value}).")
                if game.status != SessionStatus.WAITING_AGENT:
                    raise SessionInvalidStateError(
                        f"INVALID_SESSION_STATE: Текущий статус игры — '{game.status.value}'."
                    )

                agent_user = await self.get_or_create_user(
                    telegram_id=agent_tg_id,
                    username=username,
                    first_name=first_name,
                    db_session=db,
                )
                if game.analyst_id == agent_user.id:
                    raise SelfJoinError(
                        "CREATOR_CANNOT_JOIN: Создатель игры (Аналитик) не может присоединиться к ней как Агент!"
                    )

                game.agent_id = agent_user.id
                game.status = SessionStatus.IN_PROGRESS

                # Record participation
                if game.analyst is None and game.analyst_id:
                    game.analyst = await db.get(User, game.analyst_id)

                if game.analyst:
                    game.analyst.games_played += 1
                    game.analyst.games_as_analyst += 1
                agent_user.games_played += 1
                agent_user.games_as_agent += 1

                await db.commit()
                await db.refresh(game)
                self._register_session_cache(game)
                return game

    async def _process_session_victory(
        self,
        game: GameSession,
        db: AsyncSession,
    ) -> ExpBreakdown:
        """Centralized atomic victory processing for game sessions.

        Sets game status to SOLVED, calculates and awards EXP to participants,
        increments games_won, and accumulates total_hints_used and total_mistakes.
        """
        game.status = SessionStatus.SOLVED
        game.solved_at = datetime.now(timezone.utc)

        if game.campaign_chapter:
            from services.campaign_service import CampaignService

            target_user_id = game.agent_id or game.analyst_id
            if target_user_id:
                is_first, exp_awarded = await CampaignService.record_chapter_completion(
                    db=db,
                    user_id=target_user_id,
                    chapter_id=game.campaign_chapter,
                    mistakes_count=game.mistakes_count,
                    hints_used=game.hints_used,
                )
                return ExpBreakdown(
                    base_exp=exp_awarded,
                    clean_work_bonus=0,
                    autonomy_bonus=0,
                    total_exp=exp_awarded,
                )

        # Resolve quote difficulty for EXP engine
        difficulty = "MEDIUM"
        is_custom = bool(game.is_custom_quote or not game.catalog_id)
        if game.catalog_id:
            catalog_item = get_catalog_item(game.catalog_id)
            if catalog_item and catalog_item.difficulty:
                difficulty = catalog_item.difficulty

        exp_breakdown = calculate_mission_exp(
            difficulty=difficulty,
            mistakes_count=game.mistakes_count,
            hints_used=game.hints_used,
            is_custom=is_custom,
        )

        # Ensure analyst entity is loaded
        if game.analyst is None and game.analyst_id:
            game.analyst = await db.get(User, game.analyst_id)

        if game.analyst:
            game.analyst.games_won += 1
            game.analyst.exp += exp_breakdown.total_exp
            game.analyst.total_hints_used += game.hints_used
            game.analyst.total_mistakes += game.mistakes_count

        # Ensure agent entity is loaded
        if game.agent is None and game.agent_id:
            game.agent = await db.get(User, game.agent_id)

        if game.agent:
            game.agent.games_won += 1
            game.agent.exp += exp_breakdown.total_exp
            game.agent.total_hints_used += game.hints_used
            game.agent.total_mistakes += game.mistakes_count

        return exp_breakdown

    async def submit_guess(
        self,
        session_id_or_code: Union[str, int],
        user_tg_id: int,
        symbol_code: int,
        letter: str,
        db_session: Optional[AsyncSession] = None,
    ) -> SessionGuessResult:
        """Submit a letter substitution guess for an encrypted symbol.

        Evaluates correctness, updates guessed_mapping, checks victory condition,
        and transitions status to SOLVED if all letters are uncovered.
        """
        canonical_id = await self._get_canonical_session_id(session_id_or_code, db_session)
        if canonical_id is None:
            raise SessionNotFoundError(f"SESSION_NOT_FOUND: Игра '{session_id_or_code}' не найдена.")

        async with self.lock_manager.lock(f"session:{canonical_id}"):
            async with self._get_db(db_session) as db:
                game = await self._resolve_session(db, canonical_id)
                if game is None:
                    raise SessionNotFoundError(f"SESSION_NOT_FOUND: Игра '{session_id_or_code}' не найдена.")

                if game.status == SessionStatus.WAITING_AGENT:
                    raise SessionInvalidStateError(
                        "INVALID_SESSION_STATE: Игра еще ожидает подключения второго игрока (Агента)."
                    )
                if game.status in (SessionStatus.SOLVED, SessionStatus.CANCELLED):
                    raise SessionFinishedError("SESSION_FINISHED: Игра уже завершена.")
                if game.status != SessionStatus.IN_PROGRESS:
                    raise SessionInvalidStateError(
                        f"INVALID_SESSION_STATE: Игра не в процессе (текущий статус: {game.status.value})."
                    )

                if not game.agent_id or game.agent_id != user_tg_id:
                    raise UnauthorizedActionError(
                        "UNAUTHORIZED_ACTION: Только назначенный Полевой Агент может предлагать догадки."
                    )

                norm_letter = normalize_text(letter).strip()
                symbol_code = int(symbol_code)
                code_to_char = game.code_to_char_map
                revealed = set(game.guessed_mapping.values())

                if symbol_code not in code_to_char:
                    return SessionGuessResult(
                        status=GuessStatus.INVALID_CODE,
                        letter=norm_letter,
                        symbol_code=symbol_code,
                        is_victory=False,
                        revealed_letters=revealed,
                        message=f"Символ [{symbol_code}] не существует в этой шифрограмме.",
                        game=game,
                    )

                if str(symbol_code) in game.guessed_mapping:
                    already = game.guessed_mapping[str(symbol_code)]
                    return SessionGuessResult(
                        status=GuessStatus.ALREADY_REVEALED,
                        letter=already,
                        symbol_code=symbol_code,
                        is_victory=False,
                        revealed_letters=revealed,
                        message=f"Символ [{symbol_code}] уже разгадан как '{already}'.",
                        game=game,
                    )

                expected = code_to_char[symbol_code]
                if norm_letter != expected:
                    game.mistakes_count += 1
                    await db.commit()
                    return SessionGuessResult(
                        status=GuessStatus.WRONG,
                        letter=norm_letter,
                        symbol_code=symbol_code,
                        is_victory=False,
                        revealed_letters=revealed,
                        message=f"Неверно! Символ [{symbol_code}] — это не буква '{norm_letter}'.",
                        game=game,
                    )

                # Correct guess!
                game.record_guess(symbol_code, norm_letter)
                revealed = set(game.guessed_mapping.values())

                is_victory = check_victory(game.original_text, revealed)
                if is_victory:
                    await self._process_session_victory(game, db)
                    status = GuessStatus.VICTORY
                    msg = "Победа! Все символы расшифрованы! Шифрограмма полностью раскрыта."
                else:
                    status = GuessStatus.CORRECT
                    msg = f"Верно! Символ [{symbol_code}] — это буква '{norm_letter}'."

                await db.commit()
                return SessionGuessResult(
                    status=status,
                    letter=norm_letter,
                    symbol_code=symbol_code,
                    is_victory=is_victory,
                    revealed_letters=revealed,
                    message=msg,
                    game=game,
                )

    async def give_hint(
        self,
        session_id_or_code: Union[str, int],
        user_tg_id: int,
        hint_type: Union[HintType, str, int] = HintType.REVEAL_LETTER,
        symbol_code: Optional[int] = None,
        db_session: Optional[AsyncSession] = None,
    ) -> SessionHintResult:
        """Send a hint from Headquarters Analyst to Agent (letter reveal or frequency report)."""
        # Ergonomic support: allow symbol_code as 3rd positional argument
        if isinstance(hint_type, int) and symbol_code is None:
            symbol_code = hint_type
            hint_type = HintType.REVEAL_LETTER

        if isinstance(hint_type, str):
            if hint_type in ("LETTER_REVEAL", "REVEAL_LETTER"):
                hint_t = HintType.REVEAL_LETTER
            elif hint_type == "FREQUENCY_REPORT":
                hint_t = HintType.FREQUENCY_REPORT
            elif hint_type == "AGENT_SOS":
                hint_t = HintType.AGENT_SOS
            else:
                hint_t = HintType.RADIO_DEPECHE
        else:
            hint_t = hint_type

        canonical_id = await self._get_canonical_session_id(session_id_or_code, db_session)
        if canonical_id is None:
            raise SessionNotFoundError("SESSION_NOT_FOUND: Игра не найдена.")

        async with self.lock_manager.lock(f"session:{canonical_id}"):
            async with self._get_db(db_session) as db:
                game = await self._resolve_session(db, canonical_id)
                if game is None:
                    raise SessionNotFoundError("SESSION_NOT_FOUND: Игра не найдена.")

                if game.status != SessionStatus.IN_PROGRESS:
                    raise SessionInvalidStateError(
                        f"INVALID_SESSION_STATE: Нельзя отправить подсказку: игра не в процессе (статус: {game.status.value})."
                    )

                if game.analyst_id != user_tg_id and not (game.is_solo and user_tg_id == game.agent_id):
                    raise UnauthorizedActionError(
                        "UNAUTHORIZED_ACTION: Только Штабной Аналитик может отправлять подсказки."
                    )

                letter = None
                code_to_char = game.code_to_char_map
                revealed = set(game.guessed_mapping.values())

                if hint_t in (HintType.REVEAL_LETTER, HintType.LETTER_REVEAL):
                    if symbol_code is None:
                        unrevealed = get_unrevealed_codes(game.cipher_mapping, revealed)
                        if not unrevealed:
                            raise InvalidHintError("Все символы уже открыты.")
                        symbol_code = unrevealed[0]
                    else:
                        symbol_code = int(symbol_code)
                        if symbol_code not in code_to_char:
                            raise InvalidHintError(f"Символ [{symbol_code}] не существует в шифрограмме.")
                        if str(symbol_code) in game.guessed_mapping:
                            raise InvalidHintError(f"Символ [{symbol_code}] уже раскрыт.")

                    letter = code_to_char[symbol_code]
                    game.record_guess(symbol_code, letter)
                    game.hints_used += 1
                    content = f"Штаб раскрыл символ [{symbol_code}]: это буква '{letter}'"

                    revealed = set(game.guessed_mapping.values())
                    if check_victory(game.original_text, revealed):
                        await self._process_session_victory(game, db)

                elif hint_t == HintType.FREQUENCY_REPORT:
                    freqs = calculate_frequencies(game.original_text)
                    top_lines = [f"{item.letter}: {item.count} ({item.percentage}%)" for item in freqs[:5]]
                    content = "Частотный анализ депеши (топ-5 букв):\n" + "\n".join(top_lines)
                    game.hints_used += 1
                else:
                    content = "Радиодепеша от Штаба: дополнительная информация передана на терминал."
                    game.hints_used += 1

                hint_log = HintLog(
                    session_id=game.id,
                    sender_id=game.analyst_id,
                    hint_type=hint_t,
                    symbol_code=symbol_code,
                    letter=letter,
                    content=content,
                )
                db.add(hint_log)
                await db.commit()
                await db.refresh(hint_log)
                return SessionHintResult(hint_log, game, status="HINT_APPLIED")

    async def request_help(
        self,
        session_id_or_code: Union[str, int],
        user_tg_id: int,
        symbol_code: Optional[int] = None,
        message: str = "",
        db_session: Optional[AsyncSession] = None,
    ) -> HintLog:
        """Agent logs an emergency SOS request to Headquarters."""
        canonical_id = await self._get_canonical_session_id(session_id_or_code, db_session)
        if canonical_id is None:
            raise SessionNotFoundError("SESSION_NOT_FOUND: Игра не найдена.")

        async with self.lock_manager.lock(f"session:{canonical_id}"):
            async with self._get_db(db_session) as db:
                game = await self._resolve_session(db, canonical_id)
                if game is None:
                    raise SessionNotFoundError("SESSION_NOT_FOUND: Игра не найдена.")

                if game.status != SessionStatus.IN_PROGRESS:
                    raise SessionInvalidStateError(
                        f"INVALID_SESSION_STATE: Нельзя запросить помощь: игра не в процессе (статус: {game.status.value})."
                    )

                if not game.agent_id or game.agent_id != user_tg_id:
                    raise UnauthorizedActionError(
                        "UNAUTHORIZED_ACTION: Только Полевой Агент может запрашивать помощь Штаба."
                    )

                content = message.strip() or "🚨 Агент запрашивает срочную радиограмму-подсказку от Штаба!"
                hint_log = HintLog(
                    session_id=game.id,
                    sender_id=game.agent_id,
                    hint_type=HintType.AGENT_SOS,
                    symbol_code=symbol_code,
                    content=content,
                )
                db.add(hint_log)
                await db.commit()
                await db.refresh(hint_log)
                return hint_log

    async def cancel_session(
        self,
        session_identifier: Union[int, str],
        user_tg_id: int,
        db_session: Optional[AsyncSession] = None,
    ) -> GameSession:
        """Cancel an open waiting session by its Analyst before an Agent joins."""
        canonical_id = await self._get_canonical_session_id(session_identifier, db_session)
        if canonical_id is None:
            raise SessionNotFoundError(f"SESSION_NOT_FOUND: Игра '{session_identifier}' не найдена.")

        async with self.lock_manager.lock(f"session:{canonical_id}"):
            async with self._get_db(db_session) as db:
                game = await self._resolve_session(db, canonical_id)
                if game is None:
                    raise SessionNotFoundError(f"SESSION_NOT_FOUND: Игра '{session_identifier}' не найдена.")

                if game.is_solo:
                    if game.agent_id != user_tg_id:
                        raise UnauthorizedActionError(
                            "UNAUTHORIZED_ACTION: Только участник операции может отменить её."
                        )
                    if game.status != SessionStatus.IN_PROGRESS:
                        raise SessionInvalidStateError(
                            f"INVALID_SESSION_STATE: Невозможно отменить операцию в статусе {game.status.value}."
                        )
                else:
                    if game.analyst_id != user_tg_id:
                        raise UnauthorizedActionError(
                            "UNAUTHORIZED_ACTION: Только создатель сессии (Штабной Аналитик) может отменить операцию."
                        )

                    if game.status != SessionStatus.WAITING_AGENT:
                        raise SessionInvalidStateError(
                            f"INVALID_SESSION_STATE: Невозможно отменить операцию в статусе {game.status.value}. "
                            "Отмена доступна только в режиме ожидания Агента."
                        )

                game.status = SessionStatus.CANCELLED
                await db.commit()
                await db.refresh(game)
                return game

    # ------------------------------------------------------------------
    # Query & Utility Methods
    # ------------------------------------------------------------------

    async def get_session(
        self,
        session_id_or_code: Union[str, int],
        db_session: Optional[AsyncSession] = None,
    ) -> Optional[GameSession]:
        """Fetch game session by ID, code, or UUID."""
        async with self._get_db(db_session) as db:
            return await self._resolve_session(db, session_id_or_code)

    async def get_session_by_id(
        self,
        session_id: Union[int, str],
        db_session: Optional[AsyncSession] = None,
    ) -> Optional[GameSession]:
        """Fetch game session by Primary Key or room UUID."""
        async with self._get_db(db_session) as db:
            return await self._resolve_session(db, session_id)

    async def get_session_by_code(
        self,
        invite_code: str,
        db_session: Optional[AsyncSession] = None,
    ) -> Optional[GameSession]:
        """Fetch game session by 8-character invite code."""
        clean_code = invite_code.strip().lower()
        async with self._get_db(db_session) as db:
            stmt = select(GameSession).where(GameSession.invite_code == clean_code)
            res = await db.execute(stmt)
            return res.scalar_one_or_none()

    async def render_session_mask(
        self,
        session: GameSession,
        wrap_pre: bool = True,
    ) -> str:
        """Render the current masked text with uncovered letters and [code] placeholders."""
        return render_mask(
            session.original_text,
            session.cipher_mapping,
            session.revealed_letters,
            wrap_pre=wrap_pre,
        )

    async def update_message_ids(
        self,
        session_id_or_code: Union[str, int],
        analyst_chat_id: Optional[int] = None,
        analyst_message_id: Optional[int] = None,
        agent_chat_id: Optional[int] = None,
        agent_message_id: Optional[int] = None,
        db_session: Optional[AsyncSession] = None,
    ) -> None:
        """Store Telegram message coordinates for in-place UI editing synchronization."""
        canonical_id = await self._get_canonical_session_id(session_id_or_code, db_session)
        if canonical_id is None:
            return

        async with self.lock_manager.lock(f"session:{canonical_id}"):
            async with self._get_db(db_session) as db:
                game = await self._resolve_session(db, canonical_id)
                if game:
                    if analyst_chat_id is not None:
                        game.analyst_chat_id = analyst_chat_id
                    if analyst_message_id is not None:
                        game.analyst_message_id = analyst_message_id
                    if agent_chat_id is not None:
                        game.agent_chat_id = agent_chat_id
                    if agent_message_id is not None:
                        game.agent_message_id = agent_message_id
                    await db.commit()
