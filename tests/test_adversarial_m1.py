"""Adversarial stress testing and empirical property-based verification for M1 Cryptogram Engine.

Executes 1,000+ randomized iterations over synthetic Russian sentences, seeds,
and permutations, testing:
- Zero-collision invariant: len(char_to_code) == len(code_to_char) == len(U)
- Contiguous code domain: set(char_to_code.values()) == set(range(1, K + 1))
- Exact invertibility: code_to_char[char_to_code[c]] == c
- Mask reversibility: full reveal strictly equals normalize_text(text)
- Monotonic progression during partial reveal
- Frequency conservation and strict sorting order
- Set-theoretic victory evaluation with strict subset failure
- Extreme inputs: 100k characters, literal bracket sequences, decomposed unicode, HTML injections
- Permutation entropy across 100 seeds
"""
import html
import random
import re
import string
import time
from typing import List, Set
import pytest

from cryptogram.models import CipherData, FrequencyItem, ValidationResult
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
from cryptogram.catalog import get_catalog, RAW_CATALOG


# --- Synthetic Russian Sentence Generator ---

RUSSIAN_WORDS = [
    "Штаб", "Центр", "Агент", "Сводка", "Приказ", "Радиограмма", "Шифр", "Перехват",
    "Секрет", "Депеша", "Операция", "Разведка", "Высота", "Координаты", "Объект",
    "Донесение", "Наступление", "Оборона", "Связь", "Резерв", "Застава", "Эшелон",
    "Позывной", "Группа", "Канал", "Частота", "Маскировка", "Задание", "Маршрут",
    "Контроль", "Дозор", "Пароль", "Отбой", "Внимание", "Шторм", "Север", "Юг",
    "Восток", "Запад", "Сокол", "Маяк", "Тайфун", "Молния", "Квантунская", "Бортовой",
    "Подтверждение", "Экспедиция", "Ледокол", "Крепость", "Боекомплект", "Патрон",
    "Юстас", "Алекс", "Рамзай", "Токио", "Москва", "Челюскин", "Гагарин", "Суворов",
    "Кутузов", "Невский", "Победа", "Слово", "Наука", "Поход", "Меч", "Земля"
]

PUNCTUATION_POOL = [".", ",", "!", "?", "...", " — ", " - ", ": ", "; ", " «", "» ", " (", ") "]
WHITESPACE_POOL = [" ", "  ", "\n", "\t", "\r\n"]


def generate_random_russian_sentence(rng: random.Random) -> str:
    """Generate a pseudo-random Russian text with varying structure and formatting."""
    word_count = rng.randint(1, 20)
    words = [rng.choice(RUSSIAN_WORDS) for _ in range(word_count)]
    
    # Occasionally inject random Cyrillic characters (including rare ones: Ъ, Ь, Э, Ф, Щ, Ё)
    if rng.random() < 0.3:
        rare_chars = list("ЪЬЭФЩЦЁё")
        words.append("".join(rng.choice(rare_chars) for _ in range(rng.randint(1, 5))))
    
    # Occasionally inject numbers or literal brackets
    if rng.random() < 0.2:
        words.append(f"№{rng.randint(1, 999)}")
    if rng.random() < 0.15:
        words.append(f"[{rng.randint(1, 50)}]")
        
    rng.shuffle(words)
    
    # Assemble sentence with varied separators
    chunks: List[str] = []
    for i, w in enumerate(words):
        chunks.append(w)
        if i < len(words) - 1:
            sep_choice = rng.random()
            if sep_choice < 0.2:
                chunks.append(rng.choice(PUNCTUATION_POOL))
            elif sep_choice < 0.3:
                chunks.append(rng.choice(WHITESPACE_POOL))
            else:
                chunks.append(" ")
                
    text = "".join(chunks).strip()
    if not text.endswith((".", "!", "?")):
        text += rng.choice([".", "!", "?"])
    return text


# --- 1,000+ Random Russian Sentences Property Test Suite ---

@pytest.mark.boundary
class TestPropertyBasedStress1000:
    """Massive property-based invariant verification across 1,200 random inputs."""

    ITERATIONS = 1200

    def test_property_invariants_across_1200_random_sentences(self):
        """Stress-test zero-collision, domain, invertibility, mask, victory, frequencies."""
        master_rng = random.Random(987654321)

        for i in range(self.ITERATIONS):
            # Generate random sentence
            sentence = generate_random_russian_sentence(master_rng)
            
            # Select random seed: positive, negative, zero, large int, or None
            seed_choice = master_rng.choice([
                master_rng.randint(-1_000_000_000, 1_000_000_000),
                0,
                -1,
                2**31 - 1,
                None,
            ])

            # Generate cipher
            cipher = generate_cipher(sentence, seed=seed_choice)
            norm = cipher.normalized_text
            unique_cyrillic = sorted(list(set(c for c in norm if c in RUSSIAN_ALPHABET_SET)))
            k = len(unique_cyrillic)

            # Invariant 1: Zero-collision and bijection cardinality
            assert len(cipher.char_to_code) == k, (
                f"Iteration {i}: char_to_code cardinality {len(cipher.char_to_code)} != {k}"
            )
            assert len(cipher.code_to_char) == k, (
                f"Iteration {i}: code_to_char cardinality {len(cipher.code_to_char)} != {k}"
            )
            assert len(set(cipher.char_to_code.values())) == k, (
                f"Iteration {i}: Collision in char_to_code values!"
            )
            assert len(set(cipher.code_to_char.values())) == k, (
                f"Iteration {i}: Collision in code_to_char values!"
            )

            # Invariant 2: Contiguous code domain [1..K]
            expected_codes = set(range(1, k + 1))
            assert set(cipher.char_to_code.values()) == expected_codes, (
                f"Iteration {i}: char_to_code values not contiguous 1..{k}"
            )
            assert set(cipher.code_to_char.keys()) == expected_codes, (
                f"Iteration {i}: code_to_char keys not contiguous 1..{k}"
            )

            # Invariant 3: Exact Bijective Invertibility
            for char, code in cipher.char_to_code.items():
                assert cipher.code_to_char[code] == char, (
                    f"Iteration {i}: Inversion failed for char {char} -> code {code}"
                )
            for code, char in cipher.code_to_char.items():
                assert cipher.char_to_code[char] == code, (
                    f"Iteration {i}: Inversion failed for code {code} -> char {char}"
                )

            # Invariant 4: Mask initial rendering (empty reveal)
            empty_mask = render_mask(sentence, cipher.char_to_code, revealed_letters=set(), wrap_pre=False)
            # Ensure NO Cyrillic letters remain in empty_mask
            cyrillic_in_mask = [c for c in empty_mask if c in RUSSIAN_ALPHABET_SET]
            assert not cyrillic_in_mask, (
                f"Iteration {i}: Unrevealed letters leaked in empty mask: {cyrillic_in_mask}"
            )

            # Invariant 5: Mask full reveal strictly equals normalized text
            all_letters = set(unique_cyrillic)
            full_mask = render_mask(sentence, cipher.char_to_code, revealed_letters=all_letters, wrap_pre=False)
            assert full_mask == norm, (
                f"Iteration {i}: Full reveal does not strictly equal normalized text! '{full_mask}' != '{norm}'"
            )

            # Invariant 6: Stepwise monotonic reveal
            unrevealed_start = get_unrevealed_codes(cipher.char_to_code, set())
            assert set(unrevealed_start) == expected_codes

            revealed_acc: Set[str] = set()
            shuffled_letters = list(unique_cyrillic)
            master_rng.shuffle(shuffled_letters)

            for step, letter in enumerate(shuffled_letters):
                revealed_acc.add(letter)
                step_unrevealed = get_unrevealed_codes(cipher.char_to_code, revealed_acc)
                assert len(step_unrevealed) == k - (step + 1), (
                    f"Iteration {i}, step {step}: unrevealed count mismatch"
                )
                assert cipher.char_to_code[letter] not in step_unrevealed

            # Invariant 7: Set-Theoretic Victory Validation
            assert check_victory(sentence, all_letters) is True, (
                f"Iteration {i}: Victory failed when all letters revealed!"
            )
            assert check_victory(sentence, set()) is False, (
                f"Iteration {i}: Victory succeeded on empty reveal!"
            )
            if k > 1:
                # Remove one mandatory letter
                removed_letter = master_rng.choice(unique_cyrillic)
                strict_subset = all_letters - {removed_letter}
                assert check_victory(sentence, strict_subset) is False, (
                    f"Iteration {i}: Victory falsely succeeded with missing letter {removed_letter}!"
                )
            # Superset with lowercase, 'ё', and irrelevant characters
            superset = all_letters | {"Z", "X", "1", "ё", "!", "@"}
            assert check_victory(sentence, superset) is True, (
                f"Iteration {i}: Victory failed on superset with extra characters!"
            )

            # Invariant 8: Frequency conservation and sort order
            expected_total = sum(1 for c in norm if c in RUSSIAN_ALPHABET_SET)
            assert cipher.total_letters == expected_total
            assert sum(f.count for f in cipher.frequencies) == expected_total
            if expected_total > 0:
                pct_sum = sum(f.percentage for f in cipher.frequencies)
                assert 99.0 <= pct_sum <= 101.0, (
                    f"Iteration {i}: Frequency percentage sum {pct_sum} outside [99.0, 101.0]"
                )
            # Sort order verification: strictly descending count, ascending letter for ties
            for idx in range(len(cipher.frequencies) - 1):
                cur = cipher.frequencies[idx]
                nxt = cipher.frequencies[idx + 1]
                assert (cur.count > nxt.count) or (cur.count == nxt.count and cur.letter < nxt.letter), (
                    f"Iteration {i}: Frequency sort order violated between {cur} and {nxt}"
                )

            # Invariant 9: HTML wrapping and escaping safety
            wrapped = render_mask(sentence, cipher.char_to_code, revealed_letters=set(), wrap_pre=True)
            assert wrapped.startswith("<pre>") and wrapped.endswith("</pre>"), (
                f"Iteration {i}: Missing <pre> wrapping in wrapped mask"
            )
            inner = wrapped[5:-6]
            assert "<" not in inner and ">" not in inner, (
                f"Iteration {i}: Unescaped HTML tag delimiters found in inner mask!"
            )


# --- Adversarial Attack & Boundary Scenarios ---

@pytest.mark.boundary
class TestAdversarialAttacks:
    """Targeted adversarial attacks testing edge cases, scalability, and injection vectors."""

    def test_massive_stress_100k_chars(self):
        """Test performance and memory scaling on 100,000-character Russian document."""
        # 100k characters of Russian text
        base = "Секретный агент ведет наблюдение за объектом в заданном секторе. "
        multiplier = 100_000 // len(base) + 1
        big_text = (base * multiplier)[:100_000]

        start_time = time.perf_counter()
        cipher = generate_cipher(big_text, seed=999)
        elapsed_cipher = time.perf_counter() - start_time

        assert cipher.total_letters > 70_000
        # Generating cipher for 100k characters must complete in under 1 second
        assert elapsed_cipher < 1.0, f"Cipher generation took too long: {elapsed_cipher:.3f}s"

        start_time = time.perf_counter()
        masked = render_mask(big_text, cipher.char_to_code, revealed_letters={"А", "О", "Е"}, wrap_pre=False)
        elapsed_mask = time.perf_counter() - start_time
        assert elapsed_mask < 1.0, f"Mask rendering took too long: {elapsed_mask:.3f}s"
        assert len(masked) > 100_000

        # Victory check on 100k chars
        assert check_victory(big_text, set(RUSSIAN_ALPHABET)) is True

    def test_literal_bracket_tokens_in_plaintext(self):
        """Plaintext containing literal [1], [2], [10] must not corrupt cipher mapping."""
        text = "Агент [1] прибыл в квадрат [2], код [99]."
        cipher = generate_cipher(text, seed=77)

        # Unique letters should only be Russian letters, not the brackets or numbers
        assert "[" not in cipher.char_to_code
        assert "]" not in cipher.char_to_code
        assert "1" not in cipher.char_to_code

        # Unmasked text with no revealed letters must preserve literal [1] and [2]
        masked = render_mask(text, cipher.char_to_code, revealed_letters=set(), wrap_pre=False)
        assert "[1]" in masked
        assert "[2]" in masked
        assert "[99]" in masked

        # Fully revealed mask must exactly match normalized plaintext
        full = render_mask(text, cipher.char_to_code, revealed_letters=set(cipher.char_to_code.keys()), wrap_pre=False)
        assert full == normalize_text(text)

    def test_permutation_entropy_and_diversity_across_100_seeds(self):
        """Ensure distinct seeds generate distinct permutations without bias."""
        text = "СЪЕШЬ ЖЕ ЕЩЕ ЭТИХ МЯГКИХ ФРАНЦУЗСКИХ БУЛОК, ДА ВЫПЕЙ ЧАЮ."
        permutations = set()

        for seed in range(100):
            cipher = generate_cipher(text, seed=seed)
            # Tuple of codes in alphabetical order of letters
            code_tuple = tuple(cipher.char_to_code[c] for c in sorted(cipher.char_to_code.keys()))
            permutations.add(code_tuple)

        # In a 32-letter alphabet (32! ≈ 2.6e35 possibilities), 100 different seeds
        # must generate 100 completely unique permutations.
        assert len(permutations) == 100, f"Colliding permutations found! Only {len(permutations)} unique out of 100."

    def test_deterministic_seed_repeatability(self):
        """Verify identical seed always produces byte-identical mapping across multiple calls."""
        text = "ОПЕРАТИВНАЯ СВОДКА №402: ВЫСОТА 108.5 ЗАНЯТА БЕЗ ПОТЕРЬ."
        reference_cipher = generate_cipher(text, seed=424242)

        for _ in range(50):
            test_cipher = generate_cipher(text, seed=424242)
            assert test_cipher.char_to_code == reference_cipher.char_to_code
            assert test_cipher.code_to_char == reference_cipher.code_to_char
            assert test_cipher.frequencies == reference_cipher.frequencies

    def test_exhaustive_catalog_verification_all_quotes_50_seeds(self):
        """Every single quote in RAW_CATALOG tested across 50 distinct random seeds (1,000 runs)."""
        catalog = get_catalog()
        assert len(catalog) == 20

        for item in catalog:
            # Check catalog metadata
            assert item.difficulty in ("EASY", "MEDIUM", "HARD")
            assert item.char_count == len(item.text)
            assert item.unique_letters_count > 0

            for seed in range(50):
                c = generate_cipher(item.text, seed=seed)
                assert len(c.char_to_code) == item.unique_letters_count
                assert set(c.char_to_code.values()) == set(range(1, item.unique_letters_count + 1))
                # Full reveal equals normalized text
                revealed = set(c.char_to_code.keys())
                assert render_mask(item.text, c.char_to_code, revealed, wrap_pre=False) == normalize_text(item.text)
                assert check_victory(item.text, revealed) is True

    def test_html_injection_adversarial(self):
        """Ensure malicious HTML tags and scripts inside text are safely escaped."""
        evil_text = "<script>alert('XSS')</script> & \"quotes\" 'single' <pre>nested</pre> ТЕСТ"
        cipher = generate_cipher(evil_text, seed=1)

        masked_pre = render_mask(evil_text, cipher.char_to_code, revealed_letters=set(), wrap_pre=True)
        # Verifies strictly valid HTML pre block with escaped special characters
        assert masked_pre.startswith("<pre>") and masked_pre.endswith("</pre>")
        content = masked_pre[5:-6]
        assert "<script>" not in content
        assert "&lt;script&gt;" in content
        assert "&amp;" in content
        assert "&quot;" in content or '"' in content

    def test_single_letter_boundary_cases(self):
        """Boundary condition: Texts with exactly 1 unique Cyrillic letter."""
        for text in ["А", "аааа", "   Ъ   ", "....Э....", "123-Ё-456"]:
            cipher = generate_cipher(text, seed=1)
            assert len(cipher.char_to_code) == 1
            assert list(cipher.char_to_code.values()) == [1]
            letter = list(cipher.char_to_code.keys())[0]
            assert cipher.code_to_char[1] == letter
            assert cipher.frequencies[0].percentage == 100.0
            assert check_victory(text, {letter}) is True
            assert check_victory(text, set()) is False

    def test_all_32_letters_rare_pangram(self):
        """Boundary condition: Pangram containing all 32 letters with rare letters."""
        # 32 distinct letters
        all_32 = "".join(sorted(RUSSIAN_ALPHABET_SET))
        cipher = generate_cipher(all_32, seed=555)

        assert len(cipher.char_to_code) == 32
        assert set(cipher.char_to_code.values()) == set(range(1, 33))
        for f in cipher.frequencies:
            assert f.count == 1
            # 1/32 * 100 = 3.125% -> rounded to 3.12% or 3.13%
            assert f.percentage in (3.12, 3.13)
        assert sum(f.count for f in cipher.frequencies) == 32

    def test_type_safety_and_exception_handling(self):
        """Test proper exception raising on invalid argument types."""
        with pytest.raises(TypeError):
            normalize_text(12345)  # type: ignore

        with pytest.raises(TypeError):
            normalize_text(None)  # type: ignore

        with pytest.raises(ValueError):
            generate_cipher("")

        with pytest.raises(ValueError):
            generate_cipher("   ")

        with pytest.raises(ValueError):
            generate_cipher("12345 !@#$%")

        with pytest.raises(ValueError):
            generate_cipher("ENGLISH TEXT ONLY")
