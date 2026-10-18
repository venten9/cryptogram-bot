"""Tier 2 Boundary Tests: Pathological edge cases, character distributions, and formatting extremes."""
import pytest
from cryptogram.models import CipherData
from cryptogram.engine import (
    RUSSIAN_ALPHABET,
    RUSSIAN_ALPHABET_SET,
    calculate_frequencies,
    check_victory,
    generate_cipher,
    normalize_text,
    render_mask,
    validate_text,
)


@pytest.mark.boundary
class TestInputValidationBoundaries:
    """Tests for edge case inputs and invalid data handling."""

    def test_empty_string_raises(self):
        with pytest.raises(ValueError, match="(?i)пуст|empty"):
            generate_cipher("")

    def test_whitespace_only_raises(self):
        with pytest.raises(ValueError, match="(?i)пуст|empty|нет"):
            generate_cipher("   \n\t\r\n   ")

    def test_punctuation_and_digits_only_raises(self):
        with pytest.raises(ValueError, match="(?i)нет русских букв|русск"):
            generate_cipher("1234567890 !@#$%^&*()_+-=[]{}:;\"',.?/\\")

    def test_latin_script_only_raises(self):
        with pytest.raises(ValueError, match="(?i)нет русских букв|русск"):
            generate_cipher("THE QUICK BROWN FOX JUMPS OVER THE LAZY DOG")

    def test_very_long_input(self):
        # 2,000+ characters Cyrillic text
        long_text = "СЕКРЕТНАЯ ДИРЕКТИВА ШТАБА. " * 100
        cipher = generate_cipher(long_text, seed=42)
        assert cipher.total_letters > 2000
        assert len(cipher.char_to_code) > 0

    def test_homoglyphs_handling(self):
        # Latin 'A' (U+0041) vs Cyrillic 'А' (U+0410)
        mixed_text = "Агент A-1"  # first 'А' is Cyrillic, second 'A' is Latin
        cipher = generate_cipher(mixed_text, seed=1)
        # Latin 'A' is not in cipher, Cyrillic 'А' is
        assert "А" in cipher.char_to_code
        assert "A" not in cipher.char_to_code
        rendered = render_mask(mixed_text, cipher.char_to_code, revealed_letters=set(), wrap_pre=False)
        assert "A-1" in rendered

    def test_length_limit_validation_flag(self):
        long_text = "СЕКРЕТНАЯ ДИРЕКТИВА ШТАБА. " * 20
        res = validate_text(long_text, max_length=250)
        assert res.is_valid is False
        assert "слишком длинный" in res.error_message



@pytest.mark.boundary
class TestAlphabetDistributionBoundaries:
    """Tests for alphabet cardinality extremes (single letter up to full 32-letter pangrams)."""

    def test_single_unique_letter_single_occurrence(self):
        text = "А"
        cipher = generate_cipher(text, seed=1)
        assert cipher.char_to_code == {"А": 1}
        assert cipher.code_to_char == {1: "А"}
        assert cipher.total_letters == 1
        assert len(cipher.frequencies) == 1
        assert cipher.frequencies[0].letter == "А"
        assert cipher.frequencies[0].percentage == 100.0

        # Mask rendering
        assert render_mask(text, cipher.char_to_code, revealed_letters=set()) == "<pre>[1]</pre>"
        assert render_mask(text, cipher.char_to_code, revealed_letters={"А"}) == "<pre>А</pre>"

        # Victory condition
        assert check_victory(text, set()) is False
        assert check_victory(text, {"А"}) is True

    def test_single_unique_letter_multiple_occurrences(self):
        text = "ААААА, ААА! АА?"  # 10 'А's
        cipher = generate_cipher(text, seed=99)
        assert cipher.char_to_code == {"А": 1}
        assert cipher.total_letters == 10
        assert cipher.frequencies[0].count == 10
        assert cipher.frequencies[0].percentage == 100.0

        # Guessing 'А' reveals all occurrences at once
        rendered = render_mask(text, cipher.char_to_code, revealed_letters={"А"}, wrap_pre=False)
        assert rendered == "ААААА, ААА! АА?"
        assert check_victory(text, {"А"}) is True

    def test_russian_pangram_all_32_letters(self, full_pangram_cipher):
        # Full 32 letters with 'Ё' normalized to 'Е'
        assert len(full_pangram_cipher.char_to_code) == 32
        assert len(full_pangram_cipher.code_to_char) == 32
        assert set(full_pangram_cipher.char_to_code.keys()) == RUSSIAN_ALPHABET_SET
        assert set(full_pangram_cipher.char_to_code.values()) == set(range(1, 33))

        # Victory check requires all 32 letters
        all_32 = RUSSIAN_ALPHABET_SET
        assert check_victory(full_pangram_cipher.original_text, all_32) is True
        # Missing only 1 letter fails victory
        assert check_victory(full_pangram_cipher.original_text, all_32 - {"Ъ"}) is False

    def test_rare_russian_letters(self):
        text = "ОБЪЕКТ ЭКЗЕМПЛЯР ФИЛИН ПОДЪЕЗД ЩУКА"
        cipher = generate_cipher(text, seed=12)
        rare_letters = {"Ъ", "Э", "Ф", "Щ"}
        for letter in rare_letters:
            assert letter in cipher.char_to_code
            code = cipher.char_to_code[letter]
            assert 1 <= code <= len(cipher.char_to_code)


@pytest.mark.boundary
class TestFormattingAndCharacterBoundaries:
    """Tests for tricky formatting, literal brackets, emojis, and unicode symbols."""

    def test_literal_brackets_in_text(self):
        text = "ОБЪЕКТ [14] ПРИБЫЛ В СЕКТОР [Б]"
        cipher = generate_cipher(text, seed=1)
        rendered = render_mask(text, cipher.char_to_code, revealed_letters=set(), wrap_pre=False)
        # Check that literal brackets are preserved and cipher bracket tags nest properly
        assert "[14]" in rendered
        b_code = cipher.char_to_code["Б"]
        assert f"[[{b_code}]]" in rendered

    def test_multiline_mixed_newlines(self):
        text = "СТРОКА 1\nСТРОКА 2\r\nСТРОКА 3"
        cipher = generate_cipher(text, seed=1)
        rendered = render_mask(text, cipher.char_to_code, revealed_letters=set(), wrap_pre=False)
        assert "\n" in rendered
        assert "\r\n" in rendered

    def test_unicode_punctuation_and_symbols(self):
        text = "«ВНИМАНИЕ» — СКАЗАЛ ОН — „СТОП“… №1 / 100%"
        cipher = generate_cipher(text, seed=1)
        all_letters = set(cipher.char_to_code.keys())
        rendered = render_mask(text, cipher.char_to_code, revealed_letters=all_letters, wrap_pre=False)
        assert rendered == text

    def test_emojis_and_special_symbols(self):
        text = "ВНИМАНИЕ 🚨 ТРЕВОГА! ⚠️ СВЯЗЬ ЧЕРЕЗ 📡"
        cipher = generate_cipher(text, seed=1)
        rendered = render_mask(text, cipher.char_to_code, revealed_letters=set(), wrap_pre=False)
        assert "🚨" in rendered
        assert "⚠️" in rendered
        assert "📡" in rendered


@pytest.mark.boundary
class TestMaskAndGuessBoundaries:
    """Tests for edge cases in reveal sets and guess evaluation."""

    def test_irrelevant_letters_in_revealed_set(self):
        text = "КОТ"
        cipher = generate_cipher(text, seed=1)
        # Revealed set contains letters not present in text
        revealed = {"Я", "Ю", "Э", "К"}
        rendered = render_mask(text, cipher.char_to_code, revealed, wrap_pre=False)
        o_code = cipher.char_to_code["О"]
        t_code = cipher.char_to_code["Т"]
        assert rendered == f"К[{o_code}][{t_code}]"

    def test_lowercase_in_revealed_set(self):
        text = "КОТ"
        cipher = generate_cipher(text, seed=1)
        revealed = {"к", "о"}
        rendered = render_mask(text, cipher.char_to_code, revealed, wrap_pre=False)
        t_code = cipher.char_to_code["Т"]
        assert rendered == f"КО[{t_code}]"

    def test_check_victory_non_cyrillic_returns_false(self):
        # A text with no Cyrillic letters cannot be won
        assert check_victory("12345 !!!", {"А", "Б"}) is False
