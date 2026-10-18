"""Empirical Challenger stress testing and property-based verification for Milestone 1 (cryptogram/ranks.py).

Verifies:
- All 19 military ranks and exact experience thresholds (Private to Marshal).
- Monotonic progression across continuous and boundary EXP values.
- Progress bar mathematical accuracy across 1,500+ random and edge-case EXP inputs (0 to 10,000,000).
- Progress bar Unicode format: exactly 10 blocks inside brackets, monotonic fill, and integer percentage alignment.
- Specific boundary transitions: 0, 99, 100, 249, 250, 449, 450, 600, 34999, 35000, 100000.
- Mission EXP calculation engine, bonus criteria, and defensive type coercion.
"""
import random
import re
import pytest

from cryptogram.ranks import (
    RANKS,
    ExpBreakdown,
    RankInfo,
    calculate_mission_exp,
    calculate_rank_progress,
    get_next_rank,
    get_rank,
)

# Regex enforcing exact progress bar format:
# Starts with '[', followed by exactly 10 block characters (█ or ░), followed by ']', a space, and percentage (0..100)%
PROGRESS_BAR_REGEX = re.compile(r"^\[([█░]{10})\]\s*(\d+)%$")


class TestAdversarialRankHierarchy:
    """Rigorous structural and invariant tests for the 19 military ranks."""

    EXPECTED_TIERS = [
        (1, "Рядовой", 0, "🪖"),
        (2, "Ефрейтор", 100, "🎗️"),
        (3, "Младший сержант", 250, "🥉"),
        (4, "Сержант", 450, "🥈"),
        (5, "Старший сержант", 700, "🥇"),
        (6, "Старшина", 1000, "🎖️"),
        (7, "Прапорщик", 1400, "🔷"),
        (8, "Младший лейтенант", 1900, "⭐"),
        (9, "Лейтенант", 2500, "⭐⭐"),
        (10, "Старший лейтенант", 3200, "⭐⭐⭐"),
        (11, "Капитан", 4000, "⭐⭐⭐⭐"),
        (12, "Майор", 5000, "🌟"),
        (13, "Подполковник", 6500, "🌟🌟"),
        (14, "Полковник", 8500, "🌟🌟🌟"),
        (15, "Генерал-майор", 11000, "⚔️⭐"),
        (16, "Генерал-лейтенант", 14500, "⚔️⭐⭐"),
        (17, "Генерал-полковник", 19000, "⚔️⭐⭐⭐"),
        (18, "Генерал армии", 25000, "🦅⭐"),
        (19, "Маршал", 35000, "👑⭐"),
    ]

    def test_rank_table_invariants(self):
        """Verify the rank table satisfies all structural axioms."""
        assert len(RANKS) == 19, f"Expected exactly 19 ranks, got {len(RANKS)}"

        prev_threshold = -1
        for idx, (exp_level, exp_name, exp_thresh, exp_badge) in enumerate(self.EXPECTED_TIERS):
            rank = RANKS[idx]
            assert rank.level == exp_level
            assert rank.name_ru == exp_name
            assert rank.title == exp_name, "rank.title property must match name_ru"
            assert rank.threshold_exp == exp_thresh
            assert rank.badge == exp_badge
            # Strict monotonicity of thresholds
            assert rank.threshold_exp > prev_threshold, f"Threshold {rank.threshold_exp} not strictly greater than {prev_threshold}"
            prev_threshold = rank.threshold_exp

    def test_immutability(self):
        """Verify RankInfo instances are strictly immutable frozen dataclasses."""
        rank = RANKS[0]
        with pytest.raises(Exception):
            rank.level = 99  # type: ignore
        with pytest.raises(Exception):
            rank.threshold_exp = 99  # type: ignore
        with pytest.raises(Exception):
            rank.name_ru = "Hacker"  # type: ignore


class TestMonotonicProgressionStress:
    """Stress testing the monotonicity of rank assignment and boundary transitions."""

    def test_continuous_sweep_early_levels(self):
        """Verify non-decreasing rank levels across every single integer from -50 to 1,500."""
        prev_level = 1
        for exp in range(-50, 1500):
            current_rank = get_rank(exp)
            assert current_rank.level >= prev_level, f"Monotonicity violation at exp={exp}: level {current_rank.level} < {prev_level}"
            prev_level = current_rank.level

    def test_threshold_neighborhood_transitions(self):
        """Verify that every threshold T satisfies get_rank(T - 1).level + 1 == get_rank(T).level."""
        for rank in RANKS:
            t = rank.threshold_exp
            if t == 0:
                assert get_rank(-1).level == 1
                assert get_rank(0).level == 1
                assert get_rank(1).level == 1
            else:
                below = get_rank(t - 1)
                at = get_rank(t)
                above = get_rank(t + 1)
                assert below.level == rank.level - 1, f"At {t}-1, expected level {rank.level - 1}, got {below.level}"
                assert at.level == rank.level, f"At {t}, expected level {rank.level}, got {at.level}"
                assert above.level == rank.level, f"At {t}+1, expected level {rank.level}, got {above.level}"


class TestProgressBarMathAndFormatStress1500:
    """Property-based invariant oracle testing across 1,500+ random and edge-case EXP inputs."""

    def _verify_progress_oracle(self, exp: int):
        """Independent mathematical oracle validating calculate_rank_progress."""
        pct, next_thresh, bar = calculate_rank_progress(exp)

        # 1. Type and boundary assertions
        assert isinstance(pct, float), f"pct must be float, got {type(pct)}"
        assert 0.0 <= pct <= 100.0, f"pct {pct} out of bounds [0.0, 100.0] for exp={exp}"

        # 2. Progress bar string formatting and length
        match = PROGRESS_BAR_REGEX.match(bar)
        assert match is not None, f"Progress bar '{bar}' violates format regex for exp={exp}"
        blocks = match.group(1)
        bar_pct_int = int(match.group(2))

        # Exactly 10 blocks inside brackets
        assert len(blocks) == 10, f"Progress bar blocks count {len(blocks)} != 10 for exp={exp}"

        # Contiguous filled blocks followed by contiguous empty blocks
        filled_count = blocks.count("█")
        empty_count = blocks.count("░")
        assert filled_count + empty_count == 10, f"Blocks must only consist of █ and ░, got {blocks}"
        assert blocks == ("█" * filled_count + "░" * empty_count), f"Interleaved blocks detected: '{blocks}'"

        # Percentage alignment
        expected_filled = min(10, max(0, int(pct // 10)))
        assert filled_count == expected_filled, f"Expected {expected_filled} filled blocks for pct={pct}, got {filled_count}"
        assert bar_pct_int == int(pct), f"Bar text pct {bar_pct_int} does not match int(pct) {int(pct)}"

        # 3. Mathematical correctness against independent calculation
        current_rank = get_rank(exp)
        next_rank = get_next_rank(exp)

        if current_rank.level == 19:  # Marshal
            assert next_rank is None
            assert next_thresh is None
            assert pct == 100.0
            assert bar == "[██████████] 100%"
        else:
            assert next_rank is not None
            assert next_thresh == next_rank.threshold_exp
            safe_exp = max(0, exp)
            expected_raw = ((safe_exp - current_rank.threshold_exp) / (next_rank.threshold_exp - current_rank.threshold_exp)) * 100.0
            expected_pct = round(max(0.0, min(100.0, expected_raw)), 1)
            assert pct == expected_pct, f"Oracle mismatch for exp={exp}: expected {expected_pct}, got {pct}"

    def test_random_1500_exp_values(self):
        """Execute 1,500 property tests across diverse distributions."""
        rng = random.Random(42)  # Deterministic seed for reproducibility

        samples = []

        # 1. 600 samples uniformly distributed in [0, 100,000]
        samples.extend(rng.randint(0, 100_000) for _ in range(600))

        # 2. 500 samples clustered around all 19 thresholds (T - 50 to T + 50)
        for rank in RANKS:
            t = rank.threshold_exp
            for _ in range(30):
                samples.append(rng.randint(max(-100, t - 50), t + 50))

        # 3. 200 extreme values [100,001 to 10,000,000]
        samples.extend(rng.randint(100_001, 10_000_000) for _ in range(200))

        # 4. 200 negative values [-1,000,000 to -1]
        samples.extend(rng.randint(-1_000_000, -1) for _ in range(200))

        assert len(samples) >= 1500, f"Total samples {len(samples)} < 1500"

        for exp in samples:
            self._verify_progress_oracle(exp)


class TestExplicitBoundaryTransitions:
    """Verifies all boundary values explicitly required by the user and orchestrator."""

    @pytest.mark.parametrize(
        "exp, expected_rank_name, expected_pct, expected_next_exp, expected_bar",
        [
            (0, "Рядовой", 0.0, 100, "[░░░░░░░░░░] 0%"),
            (99, "Рядовой", 99.0, 100, "[█████████░] 99%"),
            (100, "Ефрейтор", 0.0, 250, "[░░░░░░░░░░] 0%"),
            (249, "Ефрейтор", 99.3, 250, "[█████████░] 99%"),
            (250, "Младший сержант", 0.0, 450, "[░░░░░░░░░░] 0%"),
            (449, "Младший сержант", 99.5, 450, "[█████████░] 99%"),
            (450, "Сержант", 0.0, 700, "[░░░░░░░░░░] 0%"),
            (600, "Сержант", 60.0, 700, "[██████░░░░] 60%"),
            (34999, "Генерал армии", 100.0, 35000, "[██████████] 100%"),
            (35000, "Маршал", 100.0, None, "[██████████] 100%"),
            (100000, "Маршал", 100.0, None, "[██████████] 100%"),
        ],
    )
    def test_authoritative_boundaries(
        self, exp, expected_rank_name, expected_pct, expected_next_exp, expected_bar
    ):
        """Verify the exact outputs for every critical threshold boundary."""
        rank = get_rank(exp)
        assert rank.name_ru == expected_rank_name

        pct, next_exp, bar = calculate_rank_progress(exp)
        assert pct == expected_pct
        assert next_exp == expected_next_exp
        assert bar == expected_bar

        # Enforce 10 blocks inside brackets
        match = PROGRESS_BAR_REGEX.match(bar)
        assert match is not None
        assert len(match.group(1)) == 10


class TestMissionExpEngineStress:
    """Adversarial testing of mission EXP calculation and defensive type coercions."""

    def test_full_matrix_rewards(self):
        """Test base EXP and bonuses across difficulties and performance parameters."""
        cases = [
            ("EASY", 0, 0, False, 100, 50, 50, 200),
            ("EASY", 1, 0, False, 100, 0, 50, 150),
            ("EASY", 0, 1, False, 100, 50, 0, 150),
            ("EASY", 3, 2, False, 100, 0, 0, 100),
            ("MEDIUM", 0, 0, False, 200, 50, 50, 300),
            ("MEDIUM", 1, 1, False, 200, 0, 0, 200),
            ("HARD", 0, 0, False, 300, 50, 50, 400),
            ("HARD", 5, 5, False, 300, 0, 0, 300),
            # Custom quote overrides base difficulty to MEDIUM (200)
            ("HARD", 0, 0, True, 200, 50, 50, 300),
            ("EASY", 0, 0, True, 200, 50, 50, 300),
            # Unknown difficulty defaults to MEDIUM (200)
            ("EXTREME_NONEXISTENT", 0, 0, False, 200, 50, 50, 300),
            ("", 0, 0, False, 200, 50, 50, 300),
            (None, 0, 0, False, 200, 50, 50, 300),
        ]

        for diff, mistakes, hints, custom, exp_base, exp_clean, exp_auto, exp_total in cases:
            res = calculate_mission_exp(diff, mistakes_count=mistakes, hints_used=hints, is_custom=custom)
            assert res.base_exp == exp_base, f"Failed base_exp for {diff}"
            assert res.clean_work_bonus == exp_clean, f"Failed clean_bonus for {diff}, mistakes={mistakes}"
            assert res.autonomy_bonus == exp_auto, f"Failed autonomy_bonus for {diff}, hints={hints}"
            assert res.total_exp == exp_total, f"Failed total_exp for {diff}"
            assert res.total_exp == res.base_exp + res.clean_work_bonus + res.autonomy_bonus
