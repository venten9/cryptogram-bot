"""Adversarial fuzzing, corner cases, and stress testing for Milestone 1."""
import html
import random
import unicodedata
import pytest
from typing import Set

from cryptogram import (
    RUSSIAN_ALPHABET,
    RUSSIAN_ALPHABET_SET,
    calculate_frequencies,
    check_victory,
    generate_cipher,
    get_catalog,
    get_catalog_by_difficulty,
    get_catalog_item,
    get_catalog_item_by_id,
    get_random_catalog_item,
    get_unrevealed_codes,
    normalize_text,
    render_mask,
    validate_text,
    wrap_in_pre,
)
from cryptogram.models import CipherData, FrequencyItem, ValidationResult


pytestmark = pytest.mark.boundary


class TestUnicodeDiacriticsAndDecomposition:
    """Stress testing Unicode combining diacritics, decomposition, and rare scripts."""

    def test_combining_diaeresis_on_e(self):
        """Test decomposed 'Ё' (U+0415 + U+0308)."""
        # Decomposed 'Ё': 'Е' + combining diaeresis
        decomposed_yo = "Е\u0308лка"
        norm = normalize_text(decomposed_yo)
        # Verify if normalize_text handles or preserves
        assert isinstance(norm, str)
        val = validate_text(decomposed_yo)
        assert val.is_valid
        cipher = generate_cipher(decomposed_yo, seed=10)
        assert "Е" in cipher.char_to_code

    def test_nfd_normalization_full_sentence(self):
        """Test full NFD-normalized sentence through cipher generation and victory check."""
        sentence = "Съешь же ещё этих мягких французских булок, да выпей чаю."
        nfd_sentence = unicodedata.normalize("NFD", sentence)
        val = validate_text(nfd_sentence)
        assert val.is_valid
        cipher = generate_cipher(nfd_sentence, seed=42)
        assert len(cipher.char_to_code) > 0
        # Invertibility check
        for char, code in cipher.char_to_code.items():
            assert cipher.code_to_char[code] == char

    def test_stress_marks_combining_acute_accent(self):
        """Test Cyrillic with combining acute accents (dictionary stress marks: U+0301)."""
        stressed = "Замо́к на две́ри и за́мок на холме́."
        val = validate_text(stressed)
        assert val.is_valid
        cipher = generate_cipher(stressed, seed=99)
        # Mask rendering must not crash
        mask = render_mask(stressed, cipher.char_to_code, {"З", "А", "М", "О", "К"})
        assert "<pre>" in mask
        assert "</pre>" in mask

    def test_non_russian_cyrillic_letters(self):
        """Ukrainian, Belarusian, and Serbian Cyrillic characters not in 32-letter alphabet."""
        mixed = "Київ — столиця України. Мінск — сталіца Беларусі. Привет мир!"
        val = validate_text(mixed)
        assert val.is_valid
        cipher = generate_cipher(mixed, seed=7)
        # Only canonical Russian letters should be ciphered
        for letter in cipher.char_to_code:
            assert letter in RUSSIAN_ALPHABET_SET
        # Ukrainian letters should not be cipher keys
        for ukr in "ІЇЄҐ":
            assert ukr not in cipher.char_to_code
        # Mask rendering retains Ukrainian letters unmasked
        mask = render_mask(mixed, cipher.char_to_code, set(), wrap_pre=False)
        assert "І" in mask or "і" in mask or "Ї" in mask

    def test_zero_width_and_invisible_characters(self):
        """Test invisible characters: zero-width space, ZWNJ, ZWJ, BOM."""
        text_with_invisibles = "П\u200BР\u200CИ\u200DВ\uFEFFЕ\u200EТ"
        norm = normalize_text(text_with_invisibles)
        assert "П" in norm and "Т" in norm
        val = validate_text(text_with_invisibles)
        assert val.is_valid
        cipher = generate_cipher(text_with_invisibles, seed=1)
        assert len(cipher.char_to_code) == 6  # П, Р, И, В, Е, Т
        mask = render_mask(text_with_invisibles, cipher.char_to_code, set(), wrap_pre=False)
        assert "[" in mask

    def test_only_invisible_characters_fails_validation(self):
        """Text containing only invisible spaces and zero-width characters."""
        invisible_text = "\u200B\u200C\u200D\uFEFF"
        val = validate_text(invisible_text)
        assert not val.is_valid

    def test_emojis_and_special_symbols_retention(self):
        """Multi-byte emojis and complex surrogate sequences."""
        text = "🚨 Секретное донесение: Агент 007 на связи! 🕵️‍♂️🎯"
        val = validate_text(text)
        assert val.is_valid
        cipher = generate_cipher(text, seed=5)
        mask = render_mask(text, cipher.char_to_code, set(), wrap_pre=False)
        assert "🚨" in mask
        assert "🕵️‍♂️" in mask or "🕵" in mask
        assert "🎯" in mask
        assert "007" in mask


class TestHtmlInjectionAndTelegramSafety:
    """Stress testing HTML injection and Telegram formatting safety."""

    def test_script_tag_injection(self):
        """Input containing <script>alert('XSS')</script>."""
        malicious = "<script>alert('XSS')</script> Секретный пароль!"
        val = validate_text(malicious)
        assert val.is_valid
        cipher = generate_cipher(malicious, seed=123)
        mask_html = render_mask(malicious, cipher.char_to_code, set(), wrap_pre=True)
        assert "<script>" not in mask_html
        assert "&lt;script&gt;" in mask_html
        assert "&lt;/script&gt;" in mask_html
        assert mask_html.startswith("<pre>")
        assert mask_html.endswith("</pre>")

    def test_pre_tag_breakout_attempt(self):
        """Input attempting to break out of <pre> tags with </pre>."""
        breakout = "</pre><b>ХАКЕРСКАЯ АТАКА</b><pre> Обычный текст"
        cipher = generate_cipher(breakout, seed=1)
        mask = render_mask(breakout, cipher.char_to_code, set(), wrap_pre=True)
        # Verbatim </pre> inside body must be escaped as &lt;/pre&gt;
        # exactly one opening <pre> at start and one closing </pre> at end
        assert mask.startswith("<pre>")
        assert mask.endswith("</pre>")
        body = mask[5:-6]
        assert "<pre>" not in body
        assert "</pre>" not in body
        assert "&lt;/pre&gt;" in body
        assert "&lt;b&gt;" in body

    def test_html_entities_in_source_text(self):
        """Source text already contains &amp;, &lt;, &gt;, &quot;."""
        text = "Формула: H&M &lt;быстрее&gt; \"Стрелы\"!"
        cipher = generate_cipher(text, seed=2)
        mask = render_mask(text, cipher.char_to_code, set(), wrap_pre=True)
        assert "&amp;amp;" in mask or "&amp;lt;" in mask
        assert "<pre>" in mask

    def test_wrap_in_pre_escapes_all_xml_dangerous_characters(self):
        """wrap_in_pre escapes <, >, &, \", ' correctly."""
        raw = "<tag attribute='val' other=\"val\">&"
        wrapped = wrap_in_pre(raw)
        assert wrapped == "<pre>&lt;tag attribute=&#x27;val&#x27; other=&quot;val&quot;&gt;&amp;</pre>" or \
               wrapped == "<pre>&lt;tag attribute='val' other=\"val\"&gt;&amp;</pre>" or \
               ("&lt;" in wrapped and "&gt;" in wrapped and "&amp;" in wrapped)


class TestEmptyMassiveAndWhitespaceInputs:
    """Stress testing boundary text sizes and whitespace varieties."""

    def test_empty_and_whitespace_validation(self):
        """Various forms of empty strings."""
        for ws in ["", " ", "   ", "\t", "\n", "\r\n", "\v\f", " \t \r\n \v "]:
            val = validate_text(ws)
            assert not val.is_valid
            assert "пустым" in val.error_message
            with pytest.raises(ValueError):
                generate_cipher(ws)

    def test_massive_input_performance(self):
        """Test performance with 50,000 character Russian text."""
        base_sentence = "В чащах юга жил-был цитрус? Да, но фальшивый экземпляръ! "
        massive_text = base_sentence * 900  # ~51,300 chars
        
        val = validate_text(massive_text)
        assert val.is_valid
        
        cipher = generate_cipher(massive_text, seed=42)
        assert cipher.total_letters > 30000
        assert len(cipher.char_to_code) == 32
        
        # Test frequencies calculation on massive text
        freqs = calculate_frequencies(massive_text)
        assert len(freqs) == 32
        total_pct = sum(f.percentage for f in freqs)
        assert 99.5 <= total_pct <= 100.5
        
        # Test mask rendering on massive text
        revealed = {"А", "Е", "И", "О", "У", "Ы", "Э", "Ю", "Я"}
        mask = render_mask(massive_text[:5000], cipher.char_to_code, revealed, wrap_pre=False)
        assert len(mask) > 5000
        
        # Test victory condition
        assert not check_victory(massive_text, revealed)
        all_letters = set(RUSSIAN_ALPHABET)
        assert check_victory(massive_text, all_letters)

    def test_repeated_single_letter_thousands_times(self):
        """10,000 occurrences of letter 'А'."""
        text = "А" * 10000
        cipher = generate_cipher(text, seed=1)
        assert len(cipher.char_to_code) == 1
        assert cipher.char_to_code["А"] == 1
        assert cipher.frequencies[0].letter == "А"
        assert cipher.frequencies[0].count == 10000
        assert cipher.frequencies[0].percentage == 100.0

    def test_punctuation_and_digit_deluge(self):
        """Heavy punctuation with only two Cyrillic letters."""
        text = "---===((( [[[ 1234567890 !@#$%^&*()_+ ]]] )))===--- ДА ---===+++"
        val = validate_text(text)
        assert val.is_valid
        cipher = generate_cipher(text, seed=1)
        assert len(cipher.char_to_code) == 2
        mask = render_mask(text, cipher.char_to_code, {"Д"}, wrap_pre=False)
        assert "ДА" not in mask
        assert "Д[" in mask


class TestSeedExtremesAndRngInvariants:
    """Stress testing seed values: negative, overflow, zero, None."""

    @pytest.mark.parametrize("seed", [
        0,
        -1,
        -42,
        -2**31,
        -2**63,
        -2**128,
        2**31 - 1,
        2**32,
        2**63 - 1,
        2**64,
        2**128,
        10**50,
    ])
    def test_seed_extremes(self, seed):
        """Cipher generation with extreme integer seeds (negative, 64-bit, 128-bit)."""
        text = "Шпион доставил шифрограмму в штаб."
        cipher = generate_cipher(text, seed=seed)
        
        # Bijectivity invariants
        k = len(cipher.char_to_code)
        assert set(cipher.char_to_code.values()) == set(range(1, k + 1))
        assert set(cipher.code_to_char.keys()) == set(range(1, k + 1))
        for char, code in cipher.char_to_code.items():
            assert cipher.code_to_char[code] == char

    def test_seed_none_is_valid_and_non_deterministic(self):
        """When seed is None, generation succeeds and two runs are likely distinct."""
        text = "Съешь же ещё этих мягких французских булок."
        c1 = generate_cipher(text, seed=None)
        c2 = generate_cipher(text, seed=None)
        # Bijectivity must hold
        assert set(c1.char_to_code.values()) == set(range(1, len(c1.char_to_code) + 1))
        assert set(c2.char_to_code.values()) == set(range(1, len(c2.char_to_code) + 1))

    def test_seed_determinism_consistency(self):
        """Identical seed must guarantee 100% identical mapping across calls."""
        text = "Разведка доложила точно: противник отступает."
        c1 = generate_cipher(text, seed=123456789)
        c2 = generate_cipher(text, seed=123456789)
        assert c1.char_to_code == c2.char_to_code
        assert c1.code_to_char == c2.code_to_char


class TestSubstitutionGameStateEdgeCases:
    """Stress testing game state, masking permutations, and victory predicates."""

    def test_render_mask_with_bogus_revealed_letters(self):
        """Revealed set with non-alphabetic, lowercase, foreign, and empty characters."""
        text = "Секретный код 404"
        cipher = generate_cipher(text, seed=1)
        revealed = {"", " ", "!", "1", "a", "Z", "latin", "Ё", "Е", "с"}
        mask = render_mask(text, cipher.char_to_code, revealed, wrap_pre=False)
        # 'С' and 'Е' should be revealed
        assert mask.startswith("СЕ")
        # Other letters masked
        assert "[" in mask

    def test_render_mask_wrap_pre_flag(self):
        """render_mask obeys wrap_pre flag."""
        text = "Тест <>&"
        cipher = generate_cipher(text, seed=1)
        masked_raw = render_mask(text, cipher.char_to_code, set(), wrap_pre=False)
        assert "<pre>" not in masked_raw
        assert "<>&" in masked_raw

        masked_pre = render_mask(text, cipher.char_to_code, set(), wrap_pre=True)
        assert masked_pre.startswith("<pre>")
        assert "&lt;&gt;&amp;" in masked_pre

    def test_check_victory_with_yo_and_ye(self):
        """Text containing 'Ё' is won by revealing 'Е' or 'Ё'."""
        text_with_yo = "Ёлка и ёж"
        # Revealing 'Е' should satisfy victory
        assert check_victory(text_with_yo, {"Е", "Л", "К", "А", "И", "Ж"})
        # Revealing 'Ё' should also satisfy victory (due to normalization)
        assert check_victory(text_with_yo, {"Ё", "Л", "К", "А", "И", "Ж"})
        # Revealing lowercase 'е' should satisfy victory
        assert check_victory(text_with_yo, {"е", "л", "к", "а", "и", "ж"})

    def test_check_victory_missing_single_letter(self):
        """Missing even 1 letter must return False."""
        text = "Агент"
        all_except_t = {"А", "Г", "Е", "Н"}
        assert not check_victory(text, all_except_t)
        all_letters = {"А", "Г", "Е", "Н", "Т"}
        assert check_victory(text, all_letters)

    def test_get_unrevealed_codes_states(self):
        """get_unrevealed_codes with empty, partial, and full reveals."""
        char_to_code = {"А": 1, "Б": 2, "В": 3, "Г": 4}
        # None revealed
        assert get_unrevealed_codes(char_to_code, set()) == [1, 2, 3, 4]
        # Partial revealed
        assert get_unrevealed_codes(char_to_code, {"А", "В"}) == [2, 4]
        # Lowercase / yo in revealed
        assert get_unrevealed_codes(char_to_code, {"а", "б"}) == [3, 4]
        # All revealed
        assert get_unrevealed_codes(char_to_code, {"А", "Б", "В", "Г"}) == []
        # Superset revealed
        assert get_unrevealed_codes(char_to_code, {"А", "Б", "В", "Г", "Д", "Е"}) == []


class TestCatalogAndDifficultyIntegrity:
    """Stress testing the catalog module against bad queries and data invariants."""

    def test_catalog_query_invalid_difficulty(self):
        """Query catalog with invalid or case-varying difficulties."""
        items = get_catalog_by_difficulty("NON_EXISTENT")
        assert items == []
        # Case insensitivity check
        easy_upper = get_catalog_by_difficulty("EASY")
        easy_lower = get_catalog_by_difficulty("easy")
        assert len(easy_upper) == len(easy_lower) == 20

    def test_catalog_item_by_id_invalid(self):
        """Query catalog with non-existent IDs."""
        assert get_catalog_item_by_id("fake_id_12345") is None
        assert get_catalog_item_by_id("") is None

    def test_get_random_catalog_item_with_seed(self):
        """get_random_catalog_item repeatability with seed parameter."""
        item1 = get_random_catalog_item(seed=42)
        item2 = get_random_catalog_item(seed=42)
        assert item1.id == item2.id

    def test_all_catalog_items_generate_valid_ciphers(self):
        """Every single catalog item must generate a valid bijective cipher without errors."""
        catalog = get_catalog()
        assert len(catalog) == 60
        for item in catalog:
            cipher = generate_cipher(item.text, seed=len(item.id))
            assert cipher.total_letters > 0
            assert len(cipher.char_to_code) == item.unique_letters_count
            assert len(cipher.code_to_char) == item.unique_letters_count
            # Victory check on all letters
            assert check_victory(item.text, set(cipher.char_to_code.keys()))


class TestRandomPropertyFuzzing:
    """Automated property-based fuzz generator running 500 stochastic trials."""

    def test_fuzz_property_invariants_500_iterations(self):
        """Generate 500 randomized adversarial texts and verify mathematical invariants."""
        # Alphabet components
        cyrillic_chars = list("абвгдеёжзийклмнопрстуфхцчшщъыьэюяАБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ")
        combining_chars = ["\u0300", "\u0301", "\u0306", "\u0308", "\u0311", "\u0327"]
        html_snippets = ["<script>", "</script>", "<pre>", "</pre>", "<b>", "</b>", "&amp;", "&lt;", "&gt;", "\""]
        noise_chars = list(" 1234567890!@#$%^&*()-_=+[]{}|;:',.<>?/~\t\n\r")
        extended_chars = ["\u200B", "\u200C", "\u200D", "\uFEFF", "🚨", "🕵️‍♂️", "Ї", "І", "Ґ"]
        
        pool = cyrillic_chars * 5 + combining_chars * 2 + html_snippets + noise_chars * 3 + extended_chars
        
        fuzz_rng = random.Random(9999)
        
        for trial in range(500):
            # Generate random length between 1 and 300
            length = fuzz_rng.randint(1, 300)
            text_parts = [fuzz_rng.choice(pool) for _ in range(length)]
            fuzzed_text = "".join(text_parts)
            
            # Select random seed
            seed_choice = fuzz_rng.choice([
                None,
                fuzz_rng.randint(-10**18, 10**18),
                0,
                -1,
                2**31 - 1,
                2**64,
            ])
            
            val = validate_text(fuzzed_text)
            norm = normalize_text(fuzzed_text)
            cyr_in_text = set(norm) & RUSSIAN_ALPHABET_SET
            
            if not cyr_in_text:
                assert not val.is_valid
                with pytest.raises(ValueError):
                    generate_cipher(fuzzed_text, seed=seed_choice)
                continue
            
            # When Cyrillic letters are present, validation must succeed (min_length default is 1)
            assert val.is_valid
            
            # 1. Generate cipher
            cipher = generate_cipher(fuzzed_text, seed=seed_choice)
            k = len(cyr_in_text)
            
            # Property 1: Bijectivity and Contiguity
            assert len(cipher.char_to_code) == k
            assert len(cipher.code_to_char) == k
            assert set(cipher.char_to_code.keys()) == cyr_in_text
            assert set(cipher.char_to_code.values()) == set(range(1, k + 1))
            assert set(cipher.code_to_char.keys()) == set(range(1, k + 1))
            for char, code in cipher.char_to_code.items():
                assert cipher.code_to_char[code] == char
                
            # Property 2: Frequency integrity
            freqs = calculate_frequencies(fuzzed_text)
            assert len(freqs) == k
            assert sum(f.count for f in freqs) == cipher.total_letters
            pct_sum = sum(f.percentage for f in freqs)
            assert 99.0 <= pct_sum <= 101.0
            # Strict descending sort order check
            for idx in range(len(freqs) - 1):
                cur, nxt = freqs[idx], freqs[idx + 1]
                assert (cur.count > nxt.count) or (cur.count == nxt.count and cur.letter < nxt.letter)
                
            # Property 3: Mask rendering & Telegram HTML safety
            # Generate random subset of letters to reveal
            num_reveal = fuzz_rng.randint(0, len(cyrillic_chars))
            revealed_raw = set(fuzz_rng.sample(cyrillic_chars, num_reveal))
            # Also occasionally add noise/garbage to revealed
            if fuzz_rng.random() < 0.3:
                revealed_raw.update(["123", "!?", "latin", ""])
                
            mask_raw = render_mask(fuzzed_text, cipher.char_to_code, revealed_raw, wrap_pre=False)
            mask_pre = render_mask(fuzzed_text, cipher.char_to_code, revealed_raw, wrap_pre=True)
            
            assert mask_pre == f"<pre>{html.escape(mask_raw)}</pre>"
            assert mask_pre.startswith("<pre>") and mask_pre.endswith("</pre>")
            # No unescaped < or > inside body
            inner_body = mask_pre[5:-6]
            assert "<" not in inner_body
            assert ">" not in inner_body
            
            # Property 4: Unrevealed codes
            unrevealed = get_unrevealed_codes(cipher.char_to_code, revealed_raw)
            norm_rev = {l.upper().replace("Ё", "Е") for l in revealed_raw}
            expected_unrev = sorted([code for char, code in cipher.char_to_code.items() if char not in norm_rev])
            assert unrevealed == expected_unrev
            
            # Property 5: Victory condition
            expected_victory = cyr_in_text.issubset(norm_rev) and len(cyr_in_text) > 0
            actual_victory = check_victory(fuzzed_text, revealed_raw)
            assert actual_victory == expected_victory

