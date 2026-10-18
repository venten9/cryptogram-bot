"""Data models and value objects for the Cryptogram Engine."""
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Set, Optional


@dataclass(frozen=True)
class FrequencyItem:
    """Frequency statistics for a single Cyrillic letter in the text."""
    letter: str
    count: int
    percentage: float  # e.g., 10.97


@dataclass(frozen=True)
class CipherData:
    """Immutable bundle containing encrypted text mapping and frequency analytics."""
    original_text: str
    normalized_text: str
    char_to_code: Dict[str, int]
    code_to_char: Dict[int, str]
    frequencies: List[FrequencyItem]
    total_letters: int


class GuessStatus(str, Enum):
    """Result status of an agent's substitution guess."""
    CORRECT = "CORRECT"
    WRONG = "WRONG"
    ALREADY_REVEALED = "ALREADY_REVEALED"
    INVALID_CODE = "INVALID_CODE"
    VICTORY = "VICTORY"


@dataclass(frozen=True)
class GuessResult:
    """Detailed evaluation result returned when a guess is submitted."""
    status: GuessStatus
    letter: str
    symbol_code: int
    is_victory: bool
    revealed_letters: Set[str]
    message: str

    @property
    def is_correct(self) -> bool:
        """True if guess was evaluated as correct or led to victory."""
        return self.status in (GuessStatus.CORRECT, GuessStatus.VICTORY)


@dataclass(frozen=True)
class ValidationResult:
    """Result of validating custom input text for cryptogram creation."""
    is_valid: bool
    error_message: Optional[str] = None


@dataclass(frozen=True)
class CatalogItem:
    """Preset secret dispatch or historical quote from catalog."""
    id: str
    title: str
    text: str
    difficulty: str  # "easy", "medium", "hard"
    category: str = "general"
    source: str = ""
    char_count: int = 0
    unique_letters_count: int = 0
