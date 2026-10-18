"""Tier 1 Unit Tests: Pure mathematical invariants and core cryptogram engine logic."""
import dataclasses
import html
import pytest
from cryptogram.models import CipherData, FrequencyItem, CatalogItem
from cryptogram.engine import (
    RUSSIAN_ALPHABET,
    RUSSIAN_ALPHABET_SET,
    calculate_frequencies,
    check_victory,
    generate_cipher,
    get_unrevealed_codes,
    normalize_text,
    render_mask,
    validate_text,
    wrap_in_pre,
)
from cryptogram.catalog import (
    get_catalog,
    get_catalog_by_difficulty,
    get_catalog_item,
    get_catalog_item_by_id,
    get_random_catalog_item,
)


@pytest.mark.unit
class TestNormalization:
    """Tests for text normalization and Cyrillic character handling."""

    def test_normalize_uppercasing(self):
        text = "Шпион и аналитик"
        res = normalize_text(text)
        assert res == "ШПИОН И АНАЛИТИК"
        assert all(c.isupper() for c in res if c in RUSSIAN_ALPHABET_SET)

    def test_normalize_yo_substitution(self):
        text = "Ёлка, ежи и полёт ружья"
        res = normalize_text(text)
        assert "Ё" not in res and "ё" not in res
        assert res == "ЕЛКА, ЕЖИ И ПОЛЕТ РУЖЬЯ"

    def test_normalize_punctuation_preservation(self):
        text = "План № 1: начать в 05:00! Код «ГРОМ»."
        res = normalize_text(text)
        assert res == "ПЛАН № 1: НАЧАТЬ В 05:00! КОД «ГРОМ»."
        # Character-by-character preservation of punctuation & numbers
        for i, ch in enumerate(text):
            if not ch.isalpha():
                assert res[i] == ch

    def test_normalize_multiline_whitespace(self):
        text = "СТРОКА 1\n  СТРОКА 2\tТАБ"
        res = normalize_text(text)
        assert res == "СТРОКА 1\n  СТРОКА 2\tТАБ"
        assert "\n" in res
        assert "\t" in res
        assert "  " in res

    def test_normalize_idempotency(self, sample_texts):
        for text in sample_texts.values():
            assert normalize_text(normalize_text(text)) == normalize_text(text)

    def test_normalize_invalid_type_raises(self):
        with pytest.raises(TypeError):
            normalize_text(12345)  # type: ignore


@pytest.mark.unit
class TestCipherGeneration:
    """Tests for bijective collision-free cipher generation."""

    def test_cipher_structure_and_types(self, seeded_cipher):
        assert isinstance(seeded_cipher, CipherData)
        assert isinstance(seeded_cipher.char_to_code, dict)
        assert isinstance(seeded_cipher.code_to_char, dict)
        assert isinstance(seeded_cipher.frequencies, list)
        assert isinstance(seeded_cipher.total_letters, int)

        # Immutability check
        with pytest.raises(dataclasses.FrozenInstanceError):
            seeded_cipher.total_letters = 999  # type: ignore

    def test_cipher_bijectivity(self, sample_texts, assert_cipher_invariants):
        for name, text in sample_texts.items():
            cipher = generate_cipher(text, seed=100)
            assert_cipher_invariants(cipher)

    def test_cipher_deterministic_seed(self):
        text = "СОВЕРШЕННО СЕКРЕТНО"
        c1 = generate_cipher(text, seed=777)
        c2 = generate_cipher(text, seed=777)
        assert c1.char_to_code == c2.char_to_code
        assert c1.code_to_char == c2.code_to_char

    def test_cipher_distinct_seeds(self):
        text = "СОВЕРШЕННО СЕКРЕТНО: ДИРЕКТИВА ЦЕНТРА"
        c1 = generate_cipher(text, seed=101)
        c2 = generate_cipher(text, seed=202)
        # With 12+ unique letters, probability of identical random permutation is 1/12! ~ 2e-9
        assert c1.char_to_code != c2.char_to_code

    def test_cipher_total_letters_count(self):
        text = "Агент 007 вышел на связь!"
        cipher = generate_cipher(text, seed=1)
        # Russian letters in 'Агент вышел на связь': А,Г,Е,Н,Т (5) + В,Ы,Ш,Е,Л (5) + Н,А (2) + С,В,Я,З,Ь (5) = 17
        assert cipher.total_letters == 17


@pytest.mark.unit
class TestFrequencyCalculation:
    """Tests for Cyrillic letter frequency calculations."""

    def test_frequency_count_sum(self, seeded_cipher):
        assert sum(item.count for item in seeded_cipher.frequencies) == seeded_cipher.total_letters

    def test_frequency_percentage_sum(self, full_pangram_cipher):
        total_pct = sum(item.percentage for item in full_pangram_cipher.frequencies)
        assert pytest.approx(total_pct, abs=0.5) == 100.0

    def test_frequency_calculation_formula(self):
        text = "ААА ББ В"  # 3 'А', 2 'Б', 1 'В' -> Total = 6
        freqs = calculate_frequencies(text)
        assert len(freqs) == 3
        # А: 3/6 = 50.0%
        assert freqs[0].letter == "А"
        assert freqs[0].count == 3
        assert freqs[0].percentage == 50.0
        # Б: 2/6 = 33.33%
        assert freqs[1].letter == "Б"
        assert freqs[1].count == 2
        assert freqs[1].percentage == 33.33
        # В: 1/6 = 16.67%
        assert freqs[2].letter == "В"
        assert freqs[2].count == 1
        assert freqs[2].percentage == 16.67

    def test_frequency_sort_order(self):
        text = "А ББ ВВ ГГ ДДД"
        freqs = calculate_frequencies(text)
        # Counts should be non-increasing
        for i in range(len(freqs) - 1):
            assert freqs[i].count >= freqs[i + 1].count
            # If counts are equal, letters should be alphabetical
            if freqs[i].count == freqs[i + 1].count:
                assert freqs[i].letter <= freqs[i + 1].letter

    def test_frequency_ignores_non_letters(self):
        text = "123 !@# ABC АБВ"
        freqs = calculate_frequencies(text)
        letters = [item.letter for item in freqs]
        assert letters == ["А", "Б", "В"]
        assert all(ch in RUSSIAN_ALPHABET_SET for ch in letters)


@pytest.mark.unit
class TestMaskRendering:
    """Tests for masked text rendering with revealed letters and tags."""

    def test_render_mask_initial_all_masked(self, seeded_cipher):
        rendered = render_mask(seeded_cipher.original_text, seeded_cipher.char_to_code, revealed_letters=set())
        assert rendered.startswith("<pre>") and rendered.endswith("</pre>")
        content = rendered[5:-6]
        # No Cyrillic letters should remain in unrevealed mask
        assert not any(c in RUSSIAN_ALPHABET_SET for c in content)
        # Spaces should be retained
        assert " " in content

    def test_render_mask_partial_reveal(self):
        text = "ШТАБ ШТАБА"
        cipher = generate_cipher(text, seed=1)
        # Reveal 'Ш' and 'А'
        revealed = {"Ш", "А"}
        rendered = render_mask(text, cipher.char_to_code, revealed, wrap_pre=False)
        # Every 'Ш' and 'А' should be visible
        assert "Ш" in rendered and "А" in rendered
        # 'Т' and 'Б' should be masked
        t_code = cipher.char_to_code["Т"]
        b_code = cipher.char_to_code["Б"]
        assert f"[{t_code}]" in rendered
        assert f"[{b_code}]" in rendered
        assert rendered == f"Ш[{t_code}]А[{b_code}] Ш[{t_code}]А[{b_code}]А"

    def test_render_mask_fully_solved(self):
        text = "СЕКРЕТНЫЙ ШТАБ"
        cipher = generate_cipher(text, seed=42)
        all_letters = set(cipher.char_to_code.keys())
        rendered = render_mask(text, cipher.char_to_code, revealed_letters=all_letters)
        assert rendered == f"<pre>{cipher.normalized_text}</pre>"

    def test_render_mask_telegram_html_safety(self):
        text = "ШИФР <АЛЬФА & ОМЕГА> ПРИНЯТ!"
        cipher = generate_cipher(text, seed=1)
        rendered = render_mask(text, cipher.char_to_code, revealed_letters={"А"})
        assert "&lt;" in rendered
        assert "&amp;" in rendered
        assert "&gt;" in rendered
        # Telegram parser would fail on raw '<' or '&' outside tags
        inner = rendered[5:-6]
        assert "<" not in inner and ">" not in inner


@pytest.mark.unit
class TestVictoryCheck:
    """Tests for set-theoretic victory evaluation."""

    def test_check_victory_empty_revealed(self):
        text = "ОПЕРАЦИЯ"
        assert check_victory(text, set()) is False

    def test_check_victory_partial_revealed(self):
        text = "ОПЕРАЦИЯ"
        cipher = generate_cipher(text, seed=1)
        all_unique = set(cipher.char_to_code.keys())
        # Missing one letter
        missing_one = all_unique - {"О"}
        assert check_victory(text, missing_one) is False

    def test_check_victory_exact_match(self):
        text = "ОПЕРАЦИЯ"
        cipher = generate_cipher(text, seed=1)
        all_unique = set(cipher.char_to_code.keys())
        assert check_victory(text, all_unique) is True

    def test_check_victory_superset(self):
        text = "ОПЕРАЦИЯ"
        cipher = generate_cipher(text, seed=1)
        all_unique = set(cipher.char_to_code.keys())
        superset = all_unique | {"Z", "9", "X", "Ъ"}
        assert check_victory(text, superset) is True

    def test_check_victory_case_insensitivity(self):
        text = "ШТАБ"
        assert check_victory(text, {"ш", "т", "а", "б"}) is True
        assert check_victory(text, {"Ш", "т", "А", "б"}) is True

    def test_check_victory_with_yo(self):
        text = "САМОЛЁТ"
        # Since 'Ё' is normalized to 'Е', revealing 'Е' completes 'САМОЛЁТ'
        assert check_victory(text, {"С", "А", "М", "О", "Л", "Е", "Т"}) is True


@pytest.mark.unit
@pytest.mark.catalog
class TestCatalog:
    """Tests for the curated dispatch and quote catalog."""

    def test_catalog_minimum_size(self):
        catalog = get_catalog()
        assert len(catalog) >= 15
        assert len(catalog) == 20

    def test_catalog_item_schema(self):
        catalog = get_catalog()
        for item in catalog:
            assert isinstance(item, CatalogItem)
            assert item.id and isinstance(item.id, str)
            assert item.title and isinstance(item.title, str)
            assert item.text and isinstance(item.text, str)
            assert item.difficulty in ("EASY", "MEDIUM", "HARD")
            assert item.category in ("military", "intelligence", "historical")
            assert len(item.text) >= 30, f"Quote {item.id} too short ({len(item.text)})"
            assert len(item.text) <= 200, f"Quote {item.id} too long ({len(item.text)})"

    def test_catalog_items_cipher_generation(self, assert_cipher_invariants):
        for item in get_catalog():
            cipher = generate_cipher(item.text, seed=42)
            assert len(cipher.char_to_code) >= 5
            assert_cipher_invariants(cipher)

    def test_catalog_lookup_and_random(self):
        catalog = get_catalog()
        first = catalog[0]
        assert get_catalog_item(first.id) == first
        assert get_catalog_item_by_id(first.id) == first
        assert get_catalog_item("non_existent_id") is None

        # Filter by difficulty
        easy_items = get_catalog_by_difficulty("EASY")
        assert len(easy_items) >= 5
        assert all(i.difficulty == "EASY" for i in easy_items)

        # Random quote retrieval
        rand_item = get_random_catalog_item(difficulty="HARD", seed=123)
        assert rand_item.difficulty == "HARD"


@pytest.mark.unit
class TestHelperFunctions:
    """Tests for miscellaneous helper functions."""

    def test_get_unrevealed_codes(self):
        c2c = {"А": 1, "Б": 2, "В": 3, "Г": 4}
        revealed = {"А", "В"}
        unrevealed = get_unrevealed_codes(c2c, revealed)
        assert unrevealed == [2, 4]

    def test_wrap_in_pre(self):
        text = "Тест <&>"
        assert wrap_in_pre(text) == f"<pre>{html.escape(text)}</pre>"


@pytest.mark.unit
class TestValidation:
    """Tests for validate_text functionality and parameter constraints."""

    def test_valid_text_passes(self):
        res = validate_text("ПРИВЕТ МИР")
        assert res.is_valid is True
        assert res.error_message is None

    def test_min_length_violation(self):
        res = validate_text("ДА", min_length=5)
        assert res.is_valid is False
        assert "слишком короткий" in res.error_message

    def test_max_length_violation(self):
        res = validate_text("ОЧЕНЬ ДЛИННЫЙ ТЕКСТ", max_length=10)
        assert res.is_valid is False
        assert "слишком длинный" in res.error_message

    def test_min_unique_letters_violation(self):
        res = validate_text("ААА АА ААА", min_unique_letters=3)
        assert res.is_valid is False
        assert "мало уникальных" in res.error_message

    def test_disallow_latin_flag(self):
        res = validate_text("ТЕКСТ С LATIN БУКВАМИ", disallow_latin=True)
        assert res.is_valid is False
        assert "латинские" in res.error_message

        res_allowed = validate_text("ТЕКСТ С LATIN БУКВАМИ", disallow_latin=False)
        assert res_allowed.is_valid is True

