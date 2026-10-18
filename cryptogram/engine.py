"""Pure functional domain engine for Russian cryptogram generation and substitution."""
import html
import random
import re
from collections import Counter
from typing import Dict, List, Set, Optional

from cryptogram.models import (
    CipherData,
    FrequencyItem,
    ValidationResult,
)

# Canonical 32-letter uppercase Russian alphabet ('Ё' normalized to 'Е')
RUSSIAN_ALPHABET = "АБВГДЕЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ"
RUSSIAN_ALPHABET_SET = frozenset(RUSSIAN_ALPHABET)

# Baseline letter frequencies (%) for Russian language from Russian National Corpus
RUSSIAN_CORPUS_FREQUENCIES: Dict[str, float] = {
    "О": 10.97, "Е": 8.45, "А": 8.01, "И": 7.35, "Н": 6.70,
    "Т": 6.26, "С": 5.47, "Р": 4.73, "В": 4.54, "Л": 4.40,
    "К": 3.49, "М": 3.21, "Д": 2.98, "П": 2.81, "У": 2.62,
    "Я": 2.01, "Ы": 1.90, "Ь": 1.74, "Г": 1.70, "З": 1.65,
    "Б": 1.59, "Ч": 1.44, "Й": 1.21, "Х": 0.97, "Ж": 0.94,
    "Ш": 0.73, "Ю": 0.64, "Ц": 0.48, "Щ": 0.36, "Э": 0.32,
    "Ф": 0.26, "Ъ": 0.04,
}


def normalize_text(text: str) -> str:
    """Normalize Russian text: uppercase conversion, 'Ё' -> 'Е'.
    
    Preserves whitespace, tabs, newlines, digits, and punctuation as-is.
    """
    if not isinstance(text, str):
        raise TypeError("Text must be a string")
    return text.upper().replace("Ё", "Е")


def validate_text(
    text: str,
    min_length: int = 1,
    max_length: Optional[int] = None,
    min_unique_letters: int = 1,
    disallow_latin: bool = False,
) -> ValidationResult:
    """Validate text suitability for cryptogram generation."""
    if not isinstance(text, str) or not text or not text.strip():
        return ValidationResult(is_valid=False, error_message="Текст не может быть пустым.")
    
    norm = normalize_text(text)
    
    if disallow_latin and re.search(r"[A-Za-z]", text):
        return ValidationResult(
            is_valid=False,
            error_message="Обнаружены латинские буквы! Текст должен содержать только русские буквы."
        )

    unique_letters = set(c for c in norm if c in RUSSIAN_ALPHABET_SET)
    if not unique_letters:
        return ValidationResult(
            is_valid=False,
            error_message="Текст не содержит русских букв для шифрования."
        )

    if len(norm) < min_length:
        return ValidationResult(
            is_valid=False,
            error_message=f"Текст слишком короткий! Минимальная длина — {min_length} символов."
        )

    if max_length is not None and len(norm) > max_length:
        return ValidationResult(
            is_valid=False,
            error_message=f"Текст слишком длинный! Максимальная длина — {max_length} символов."
        )

    if len(unique_letters) < min_unique_letters:
        return ValidationResult(
            is_valid=False,
            error_message=f"Слишком мало уникальных русских букв (минимум {min_unique_letters}, сейчас: {len(unique_letters)})."
        )

    return ValidationResult(is_valid=True)


def calculate_frequencies(text: str) -> List[FrequencyItem]:
    """Calculate absolute and relative frequencies of Cyrillic letters sorted descending."""
    norm = normalize_text(text)
    letters = [c for c in norm if c in RUSSIAN_ALPHABET_SET]
    total = len(letters)
    if total == 0:
        return []
    
    counts = Counter(letters)
    items = [
        FrequencyItem(
            letter=char,
            count=cnt,
            percentage=round((cnt / total) * 100.0, 2)
        )
        for char, cnt in counts.items()
    ]
    # Primary: count descending, Secondary: letter alphabetical ascending (strict tie-breaking)
    items.sort(key=lambda x: (-x.count, x.letter))
    return items


def generate_cipher(text: str, seed: Optional[int] = None) -> CipherData:
    """Generate a bijective collision-free cipher mapping unique Cyrillic letters to [1]..[K].
    
    Deterministic when seed is provided.
    """
    validation = validate_text(text)
    if not validation.is_valid:
        raise ValueError(validation.error_message)

    norm = normalize_text(text)
    # Strictly sorted unique Cyrillic letters for deterministic shuffling
    unique_letters = sorted(set(c for c in norm if c in RUSSIAN_ALPHABET_SET))
    k = len(unique_letters)

    rng = random.Random(seed)
    codes = list(range(1, k + 1))
    rng.shuffle(codes)

    char_to_code = {char: code for char, code in zip(unique_letters, codes)}
    code_to_char = {code: char for char, code in zip(unique_letters, codes)}
    frequencies = calculate_frequencies(norm)
    total_letters = sum(f.count for f in frequencies)

    return CipherData(
        original_text=text,
        normalized_text=norm,
        char_to_code=char_to_code,
        code_to_char=code_to_char,
        frequencies=frequencies,
        total_letters=total_letters,
    )


def render_mask(
    text: str,
    char_to_code: Dict[str, int],
    revealed_letters: Set[str],
    wrap_pre: bool = True
) -> str:
    """Render masked text with revealed characters and [code] for unrevealed.
    
    Non-Cyrillic characters (punctuation, numbers, spaces) are preserved verbatim.
    When wrap_pre is True, the output is safely HTML-escaped and wrapped in <pre> tags.
    """
    norm_revealed = {l.upper().replace("Ё", "Е") for l in revealed_letters}
    out: List[str] = []
    
    for ch in text:
        ch_norm = ch.upper().replace("Ё", "Е")
        if ch_norm in char_to_code:
            if ch_norm in norm_revealed:
                out.append(ch_norm)
            else:
                out.append(f"[{char_to_code[ch_norm]}]")
        else:
            out.append(ch)
            
    result = "".join(out)
    if wrap_pre:
        return f"<pre>{html.escape(result)}</pre>"
    return result


def check_victory(text: str, revealed_letters: Set[str]) -> bool:
    """Check if all unique Cyrillic letters in the text have been revealed.
    
    Returns False if text contains no Cyrillic letters or if any unique letter is unrevealed.
    """
    norm = normalize_text(text)
    required = set(c for c in norm if c in RUSSIAN_ALPHABET_SET)
    if not required:
        return False
    norm_revealed = {l.upper().replace("Ё", "Е") for l in revealed_letters}
    return required.issubset(norm_revealed)


def get_unrevealed_codes(char_to_code: Dict[str, int], revealed_letters: Set[str]) -> List[int]:
    """Get sorted list of cipher codes that remain unrevealed."""
    norm_revealed = {l.upper().replace("Ё", "Е") for l in revealed_letters}
    unrevealed = [code for char, code in char_to_code.items() if char not in norm_revealed]
    return sorted(unrevealed)


def wrap_in_pre(text: str) -> str:
    """Safely escape text and enclose inside Telegram HTML <pre> block."""
    return f"<pre>{html.escape(text)}</pre>"
