"""SQLAlchemy 2.0 ORM models and declarative schema for Cooperative Cryptogram Bot.

Features:
- DeclarativeBase with AsyncAttrs mixin for non-blocking asynchronous relationship resolution.
- Strongly-typed Mapped columns with Python 3.14+ typing.
- JSON mapping columns with MutableDict for automatic mutation and dirty-state tracking.
- Bi-directional typed relationships with cascade delete-orphan and lazy="selectin".
- Cryptogram engine interoperability helper properties:
  - revealed_letters -> Set[str]
  - code_to_char_map -> Dict[int, str]
  - symbol_to_revealed_char -> Dict[int, str]
  - is_solved -> bool
  - is_finished -> bool
  - progress_percentage -> float
- Composite and foreign key indices for optimized SQLite query performance.
"""
import enum
import uuid
from datetime import datetime
from typing import Dict, List, Optional, Set

from cryptogram.ranks import calculate_rank_progress, get_next_rank, get_rank
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    JSON,
    func,
)
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, synonym


class Base(AsyncAttrs, DeclarativeBase):
    """Declarative Base class equipped with AsyncAttrs for async attribute resolution."""
    pass


class SessionStatus(str, enum.Enum):
    """Lifecycle state machine for a cooperative cryptogram game session."""
    WAITING_AGENT = "WAITING_AGENT"
    IN_PROGRESS = "IN_PROGRESS"
    SOLVED = "SOLVED"
    CANCELLED = "CANCELLED"


class HintType(str, enum.Enum):
    """Categorization of tactical assistance transmissions."""
    REVEAL_LETTER = "REVEAL_LETTER"        # HQ Analyst discloses a specific symbol/letter
    LETTER_REVEAL = "LETTER_REVEAL"        # Semantic alias for REVEAL_LETTER
    FREQUENCY_REPORT = "FREQUENCY_REPORT"  # HQ Analyst sends letter frequency distribution
    AGENT_SOS = "AGENT_SOS"                # Field Agent requests immediate HQ intervention
    RADIO_DEPECHE = "RADIO_DEPECHE"        # Custom or narrative radio hint transmission


class User(Base):
    """Telegram bot user entity tracking player identity and cooperative statistics."""
    __tablename__ = "users"

    # Telegram user IDs exceed 32-bit signed integers (> 2 billion), BigInteger PK is required.
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    telegram_id = synonym("id")

    username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    first_name: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    last_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # Player aggregate statistics
    games_played: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    games_won: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    exp: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    games_as_analyst: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    games_as_agent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_hints_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_mistakes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Audit timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Bi-directional relationships with lazy="selectin" to prevent MissingGreenlet in async context
    hosted_sessions: Mapped[List["GameSession"]] = relationship(
        "GameSession",
        foreign_keys="[GameSession.analyst_id]",
        back_populates="analyst",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    joined_sessions: Mapped[List["GameSession"]] = relationship(
        "GameSession",
        foreign_keys="[GameSession.agent_id]",
        back_populates="agent",
        lazy="selectin",
    )
    hints_sent: Mapped[List["HintLog"]] = relationship(
        "HintLog",
        foreign_keys="[HintLog.sender_id]",
        back_populates="sender",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    @property
    def display_name(self) -> str:
        """Formatted human-readable user representation."""
        if self.last_name:
            return f"{self.first_name} {self.last_name}".strip()
        return self.first_name or (f"@{self.username}" if self.username else f"Agent_{self.id}")

    @property
    def win_rate(self) -> float:
        """Percentage of games won."""
        if self.games_played <= 0:
            return 0.0
        return round((self.games_won / self.games_played) * 100, 1)

    @property
    def rank_title(self) -> str:
        """Russian military rank title based on accumulated EXP."""
        return get_rank(self.exp if self.exp is not None else 0).name_ru

    @property
    def current_rank_badge(self) -> str:
        """Badge emoji or insignia for current military rank."""
        return get_rank(self.exp if self.exp is not None else 0).badge

    @property
    def next_rank_exp(self) -> Optional[int]:
        """EXP threshold for next rank, or None if Marshal."""
        next_r = get_next_rank(self.exp if self.exp is not None else 0)
        return next_r.threshold_exp if next_r is not None else None

    @property
    def rank_progress_pct(self) -> float:
        """Progress percentage towards next rank (0.0 to 100.0)."""
        return calculate_rank_progress(self.exp if self.exp is not None else 0)[0]

    @property
    def rank_progress_bar(self) -> str:
        """10-segment Unicode progress bar string."""
        return calculate_rank_progress(self.exp if self.exp is not None else 0)[2]

    def __repr__(self) -> str:
        return f"<User id={self.id} username={self.username!r}>"


class GameSession(Base):
    """Cooperative cryptogram game session connecting Analyst and Agent."""
    __tablename__ = "game_sessions"
    __table_args__ = (
        Index("ix_game_sessions_status_analyst", "status", "analyst_id"),
        Index("ix_game_sessions_status_agent", "status", "agent_id"),
    )

    # Primary identification
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    room_uuid: Mapped[str] = mapped_column(String(36), unique=True, index=True, default=lambda: str(uuid.uuid4()))
    invite_code: Mapped[str] = mapped_column(String(16), unique=True, index=True, nullable=False)

    # Participants
    analyst_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    agent_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)

    # Room status
    status: Mapped[SessionStatus] = mapped_column(
        SQLEnum(SessionStatus, native_enum=False),
        default=SessionStatus.WAITING_AGENT,
        nullable=False,
        index=True,
    )

    # Cryptogram texts
    original_text: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_text: Mapped[str] = mapped_column(Text, nullable=False)

    # JSON mappings with MutableDict for automatic dirty state tracking
    cipher_mapping: Mapped[Dict[str, int]] = mapped_column(
        MutableDict.as_mutable(JSON), nullable=False
    )
    guessed_mapping: Mapped[Dict[str, str]] = mapped_column(
        MutableDict.as_mutable(JSON), default=dict, nullable=False
    )

    # Game configuration source
    is_custom_quote: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_custom = synonym("is_custom_quote")
    catalog_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # Active Telegram UI coordinates for in-place edit_message_text synchronization
    analyst_chat_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    analyst_message_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    agent_chat_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    agent_message_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Mission metrics & optimistic concurrency version
    total_unique_letters: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    revealed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    hints_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    mistakes_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    # Audit timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    solved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at = synonym("solved_at")

    # Relationships with lazy="selectin" to prevent MissingGreenlet in async context
    analyst: Mapped["User"] = relationship("User", foreign_keys=[analyst_id], back_populates="hosted_sessions", lazy="selectin")
    agent: Mapped[Optional["User"]] = relationship("User", foreign_keys=[agent_id], back_populates="joined_sessions", lazy="selectin")
    hints: Mapped[List["HintLog"]] = relationship(
        "HintLog",
        back_populates="session",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="HintLog.created_at.asc()",
    )

    # Domain helper properties
    @property
    def revealed_letters(self) -> Set[str]:
        """Set of unique Cyrillic letters revealed so far (compatible with cryptogram engine)."""
        return set(self.guessed_mapping.values())

    @property
    def symbol_to_revealed_char(self) -> Dict[int, str]:
        """Mapping from integer symbol code to revealed character."""
        return {int(code): char for code, char in self.guessed_mapping.items()}

    @property
    def code_to_char_map(self) -> Dict[int, str]:
        """Inverted cipher mapping: integer code -> true Cyrillic character."""
        return {int(code): char for char, code in self.cipher_mapping.items()}

    @property
    def is_solved(self) -> bool:
        """Check if game session is solved."""
        return self.status == SessionStatus.SOLVED

    @property
    def is_finished(self) -> bool:
        """Check if game session has reached terminal status."""
        return self.status in (SessionStatus.SOLVED, SessionStatus.CANCELLED)

    @property
    def progress_percentage(self) -> float:
        """Current decryption progress percentage (0.0 to 100.0)."""
        total = self.total_unique_letters or len(self.cipher_mapping)
        if total <= 0:
            return 0.0
        return round((len(self.revealed_letters) / total) * 100, 1)

    @property
    def total_letters(self) -> int:
        """Total Cyrillic letters count in normalized text."""
        return sum(1 for c in (self.normalized_text or "") if "А" <= c <= "Я")

    @property
    def total_letters_count(self) -> int:
        """Alias for total_letters."""
        return self.total_letters

    def record_guess(self, symbol_code: int, letter: str) -> None:
        """Defensively update guessed_mapping and revealed_count with automatic dirty flag."""
        mapping = dict(self.guessed_mapping)
        mapping[str(symbol_code)] = letter
        self.guessed_mapping = mapping
        self.revealed_count = len(mapping)

    def __repr__(self) -> str:
        return f"<GameSession id={self.id} code={self.invite_code} status={self.status}>"


class HintLog(Base):
    """Audit and communication record for tactical assistance in a session."""
    __tablename__ = "hint_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("game_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    sender_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    hint_type: Mapped[HintType] = mapped_column(SQLEnum(HintType, native_enum=False), nullable=False, index=True)
    symbol_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    revealed_code = synonym("symbol_code")
    letter: Mapped[Optional[str]] = mapped_column(String(4), nullable=True)
    revealed_letter = synonym("letter")
    payload: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    session: Mapped["GameSession"] = relationship("GameSession", back_populates="hints", lazy="selectin")
    sender: Mapped["User"] = relationship("User", foreign_keys=[sender_id], back_populates="hints_sent", lazy="selectin")

    def __repr__(self) -> str:
        return f"<HintLog id={self.id} type={self.hint_type} session={self.session_id}>"
