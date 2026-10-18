"""Reusable Pytest fixtures and assertion helpers for M1 Cryptogram Engine."""
import pytest
from typing import Dict, Set, Callable
from cryptogram.models import CipherData, FrequencyItem
from cryptogram.engine import generate_cipher, normalize_text, RUSSIAN_ALPHABET_SET
from cryptogram.catalog import get_catalog


@pytest.fixture
def sample_texts() -> Dict[str, str]:
    """Collection of standardized test texts representing various scenarios."""
    return {
        "simple": "ШТАБ ЦЕНТР",
        "medium": "СЕКРЕТНАЯ ОПЕРАЦИЯ «ТАЙФУН» НАЧАТА В ПОЛНОЧЬ.",
        "with_yo": "Ёлка, ёжик и полёт самолёта над рекой.",
        "with_numbers_punct": "Внимание! Объект №14 — в секторе 7-Б; координаты: [55.75, 37.61]...",
        "multiline": "ДЕПЕША №1\nСВЯЗЬ ЧЕРЕЗ АГЕНТА «СОВА».\nОТБОЙ.",
        "single_letter": "ААА АА А!",
        "pangram": "Съешь же ещё этих мягких французских булок, да выпей чаю.",
        "html_entities": "ШИФРОВКА <АЛЬФА & ОМЕГА> ПРИНЯТА.",
    }


@pytest.fixture
def seeded_cipher() -> CipherData:
    """Deterministic cipher instance for predictable assertion testing (seed=42)."""
    text = "СЕКРЕТНЫЙ ШТАБ"
    return generate_cipher(text, seed=42)


@pytest.fixture
def full_pangram_cipher() -> CipherData:
    """Cipher containing all 32 Russian letters for complete alphabet testing."""
    pangram = "Съешь же ещё этих мягких французских булок, да выпей чаю."
    return generate_cipher(pangram, seed=12345)


@pytest.fixture
def assert_cipher_invariants() -> Callable[[CipherData], None]:
    """Reusable assertion helper verifying core mathematical invariants."""
    def _verify(cipher: CipherData):
        norm = cipher.normalized_text
        cyrillic_unique = sorted(list(set(c for c in norm if c in RUSSIAN_ALPHABET_SET)))
        
        # 1. Bijective mapping cardinality
        assert len(cipher.char_to_code) == len(cyrillic_unique), "char_to_code size mismatch"
        assert len(cipher.code_to_char) == len(cyrillic_unique), "code_to_char size mismatch"
        
        # 2. Domain and Codomain
        assert set(cipher.char_to_code.keys()) == set(cyrillic_unique), "Keys do not match unique letters"
        expected_codes = set(range(1, len(cyrillic_unique) + 1))
        assert set(cipher.char_to_code.values()) == expected_codes, "Codes are not contiguous 1..|U|"
        assert set(cipher.code_to_char.keys()) == expected_codes, "Reverse codes are not contiguous 1..|U|"
        
        # 3. Exact invertibility
        for char, code in cipher.char_to_code.items():
            assert cipher.code_to_char[code] == char, f"Inversion failed for {char} -> {code}"
            
        # 4. Total letters count
        expected_total = sum(1 for c in norm if c in RUSSIAN_ALPHABET_SET)
        assert cipher.total_letters == expected_total, "total_letters metric mismatch"
        
        # 5. Frequency conservation
        if cipher.total_letters > 0:
            assert sum(f.count for f in cipher.frequencies) == cipher.total_letters, "Frequency count sum mismatch"
            assert pytest.approx(sum(f.percentage for f in cipher.frequencies), abs=0.5) == 100.0, "Percentage sum != 100%"

    return _verify
