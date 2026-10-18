"""Unit tests for military rank hierarchy, EXP calculation engine, and progress bar formatting."""
import dataclasses
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


class TestRankHierarchy:
    """Verifies all 19 military ranks, thresholds, and insignia."""

    def test_all_nineteen_ranks_exist(self):
        """Verify the rank table contains exactly 19 tiers in strictly increasing order."""
        assert len(RANKS) == 19

        # Verify level sequence 1..19
        for i, rank in enumerate(RANKS, start=1):
            assert rank.level == i
            assert isinstance(rank.name_ru, str) and len(rank.name_ru) > 0
            assert rank.title == rank.name_ru
            assert isinstance(rank.badge, str) and len(rank.badge) > 0

        # Verify thresholds are strictly monotonically increasing
        thresholds = [r.threshold_exp for r in RANKS]
        assert thresholds[0] == 0
        assert thresholds[-1] == 35000
        assert thresholds == sorted(thresholds)
        assert len(set(thresholds)) == 19

    @pytest.mark.parametrize(
        "exp, expected_level, expected_name, expected_badge",
        [
            (0, 1, "Рядовой", "🪖"),
            (100, 2, "Ефрейтор", "🎗️"),
            (250, 3, "Младший сержант", "🥉"),
            (450, 4, "Сержант", "🥈"),
            (700, 5, "Старший сержант", "🥇"),
            (1000, 6, "Старшина", "🎖️"),
            (1400, 7, "Прапорщик", "🔷"),
            (1900, 8, "Младший лейтенант", "⭐"),
            (2500, 9, "Лейтенант", "⭐⭐"),
            (3200, 10, "Старший лейтенант", "⭐⭐⭐"),
            (4000, 11, "Капитан", "⭐⭐⭐⭐"),
            (5000, 12, "Майор", "🌟"),
            (6500, 13, "Подполковник", "🌟🌟"),
            (8500, 14, "Полковник", "🌟🌟🌟"),
            (11000, 15, "Генерал-майор", "⚔️⭐"),
            (14500, 16, "Генерал-лейтенант", "⚔️⭐⭐"),
            (19000, 17, "Генерал-полковник", "⚔️⭐⭐⭐"),
            (25000, 18, "Генерал армии", "🦅⭐"),
            (35000, 19, "Маршал", "👑⭐"),
        ],
    )
    def test_exact_rank_thresholds(self, exp, expected_level, expected_name, expected_badge):
        """Verify each exact EXP threshold resolves to the expected rank tier."""
        rank = get_rank(exp)
        assert rank.level == expected_level
        assert rank.name_ru == expected_name
        assert rank.badge == expected_badge

    @pytest.mark.parametrize(
        "exp, expected_level, expected_name",
        [
            (99, 1, "Рядовой"),
            (249, 2, "Ефрейтор"),
            (449, 3, "Младший сержант"),
            (699, 4, "Сержант"),
            (999, 5, "Старший сержант"),
            (1399, 6, "Старшина"),
            (1899, 7, "Прапорщик"),
            (2499, 8, "Младший лейтенант"),
            (3199, 9, "Лейтенант"),
            (3999, 10, "Старший лейтенант"),
            (4999, 11, "Капитан"),
            (6499, 12, "Майор"),
            (8499, 13, "Подполковник"),
            (10999, 14, "Полковник"),
            (14499, 15, "Генерал-майор"),
            (18999, 16, "Генерал-лейтенант"),
            (24999, 17, "Генерал-полковник"),
            (34999, 18, "Генерал армии"),
        ],
    )
    def test_one_point_below_thresholds(self, exp, expected_level, expected_name):
        """Verify EXP one point below a threshold stays at the lower rank."""
        rank = get_rank(exp)
        assert rank.level == expected_level
        assert rank.name_ru == expected_name


class TestRankProgressAndEdgeCases:
    """Verifies progress calculation math, edge cases, and visual bar formatting."""

    def test_600_exp_exact_user_spec(self):
        """Verify that 600 EXP produces Sergeant and exactly '[██████░░░░] 60%'."""
        pct, next_exp, bar = calculate_rank_progress(600)
        assert pct == 60.0
        assert next_exp == 700
        assert bar == "[██████░░░░] 60%"

        rank = get_rank(600)
        assert rank.name_ru == "Сержант"
        assert rank.badge == "🥈"

    def test_zero_exp(self):
        """Verify fresh player with 0 EXP."""
        pct, next_exp, bar = calculate_rank_progress(0)
        assert pct == 0.0
        assert next_exp == 100
        assert bar == "[░░░░░░░░░░] 0%"
        assert get_rank(0).name_ru == "Рядовой"

    def test_negative_exp(self):
        """Verify negative EXP is defensively clamped to 0."""
        for neg_val in (-1, -50, -9999):
            rank = get_rank(neg_val)
            assert rank.name_ru == "Рядовой"
            pct, next_exp, bar = calculate_rank_progress(neg_val)
            assert pct == 0.0
            assert next_exp == 100
            assert bar == "[░░░░░░░░░░] 0%"

    def test_99_exp(self):
        """Verify 99 EXP produces 99% progress bar."""
        pct, next_exp, bar = calculate_rank_progress(99)
        assert pct == 99.0
        assert next_exp == 100
        assert bar == "[█████████░] 99%"
        assert get_rank(99).name_ru == "Рядовой"

    def test_100_exp(self):
        """Verify 100 EXP advances to Gefreiter with 0% progress to Junior Sergeant."""
        pct, next_exp, bar = calculate_rank_progress(100)
        assert pct == 0.0
        assert next_exp == 250
        assert bar == "[░░░░░░░░░░] 0%"
        assert get_rank(100).name_ru == "Ефрейтор"

    def test_249_exp(self):
        """Verify 249 EXP is 99.3% towards Junior Sergeant."""
        pct, next_exp, bar = calculate_rank_progress(249)
        assert pct == 99.3
        assert next_exp == 250
        assert bar == "[█████████░] 99%"

    def test_marshal_threshold_35000(self):
        """Verify Marshal at exact 35000 threshold."""
        pct, next_exp, bar = calculate_rank_progress(35000)
        assert pct == 100.0
        assert next_exp is None
        assert bar == "[██████████] 100%"
        assert get_rank(35000).name_ru == "Маршал"
        assert get_rank(35000).badge == "👑⭐"

    def test_far_above_marshal_100000(self):
        """Verify high EXP values (> 35000) remain Marshal with 100% progress."""
        for high_exp in (35001, 50000, 100000, 1000000):
            pct, next_exp, bar = calculate_rank_progress(high_exp)
            assert pct == 100.0
            assert next_exp is None
            assert bar == "[██████████] 100%"
            assert get_rank(high_exp).name_ru == "Маршал"


class TestNextRankResolution:
    """Verifies get_next_rank logic across all levels."""

    def test_next_rank_levels_1_to_18(self):
        """Verify every rank below Marshal correctly identifies the next rank."""
        for rank in RANKS[:-1]:
            next_rank = get_next_rank(rank.threshold_exp)
            assert next_rank is not None
            assert next_rank.level == rank.level + 1

    def test_next_rank_for_marshal_is_none(self):
        """Verify Marshal has no next rank."""
        assert get_next_rank(35000) is None
        assert get_next_rank(100000) is None


class TestMissionExpEngine:
    """Verifies base EXP, clean work bonus, autonomy bonus, and edge cases."""

    def test_easy_difficulty_rewards(self):
        """Verify EASY: Base=100."""
        # 0 mistakes, 0 hints -> 100 + 50 + 50 = 200
        res = calculate_mission_exp("EASY", mistakes_count=0, hints_used=0)
        assert res.base_exp == 100
        assert res.clean_work_bonus == 50
        assert res.autonomy_bonus == 50
        assert res.total_exp == 200

        # Mistakes > 0, 0 hints -> 100 + 0 + 50 = 150
        res = calculate_mission_exp("EASY", mistakes_count=2, hints_used=0)
        assert res.clean_work_bonus == 0
        assert res.autonomy_bonus == 50
        assert res.total_exp == 150

        # 0 mistakes, Hints > 0 -> 100 + 50 + 0 = 150
        res = calculate_mission_exp("EASY", mistakes_count=0, hints_used=1)
        assert res.clean_work_bonus == 50
        assert res.autonomy_bonus == 0
        assert res.total_exp == 150

        # Both mistakes > 0 and hints > 0 -> 100 + 0 + 0 = 100
        res = calculate_mission_exp("EASY", mistakes_count=1, hints_used=3)
        assert res.clean_work_bonus == 0
        assert res.autonomy_bonus == 0
        assert res.total_exp == 100

    def test_medium_difficulty_rewards(self):
        """Verify MEDIUM: Base=200."""
        res = calculate_mission_exp("MEDIUM", mistakes_count=0, hints_used=0)
        assert res.base_exp == 200
        assert res.total_exp == 300

        res = calculate_mission_exp("MEDIUM", mistakes_count=1, hints_used=1)
        assert res.total_exp == 200

    def test_hard_difficulty_rewards(self):
        """Verify HARD: Base=300."""
        res = calculate_mission_exp("HARD", mistakes_count=0, hints_used=0)
        assert res.base_exp == 300
        assert res.clean_work_bonus == 50
        assert res.autonomy_bonus == 50
        assert res.total_exp == 400

        res = calculate_mission_exp("HARD", mistakes_count=5, hints_used=2)
        assert res.total_exp == 300

    def test_custom_quote_override(self):
        """Verify custom quotes always use MEDIUM base EXP (200), regardless of difficulty passed."""
        res = calculate_mission_exp("HARD", mistakes_count=0, hints_used=0, is_custom=True)
        assert res.base_exp == 200
        assert res.clean_work_bonus == 50
        assert res.autonomy_bonus == 50
        assert res.total_exp == 300

    def test_unknown_difficulty_defaults_to_medium(self):
        """Verify unknown difficulty strings default to MEDIUM (200 base)."""
        res = calculate_mission_exp("CUSTOM_UNKNOWN", mistakes_count=0, hints_used=0)
        assert res.base_exp == 200
        assert res.total_exp == 300

    def test_case_insensitivity(self):
        """Verify difficulty matching is case-insensitive."""
        assert calculate_mission_exp("easy", 0, 0).base_exp == 100
        assert calculate_mission_exp("Medium", 0, 0).base_exp == 200
        assert calculate_mission_exp("HaRd", 0, 0).base_exp == 300

    def test_dataclasses_frozen_immutability(self):
        """Verify ExpBreakdown and RankInfo cannot be modified at runtime."""
        breakdown = calculate_mission_exp("EASY", 0, 0)
        with pytest.raises(dataclasses.FrozenInstanceError):
            breakdown.total_exp = 999  # type: ignore

        rank = get_rank(0)
        with pytest.raises(dataclasses.FrozenInstanceError):
            rank.name_ru = "Генералиссимус"  # type: ignore


class TestPackageExports:
    """Verifies that cryptogram package re-exports all ranks symbols in __all__."""

    def test_reexported_symbols(self):
        """Verify all 7 ranks symbols are present in cryptogram and __all__."""
        import cryptogram

        expected_symbols = [
            "ExpBreakdown",
            "RankInfo",
            "RANKS",
            "calculate_mission_exp",
            "calculate_rank_progress",
            "get_next_rank",
            "get_rank",
        ]
        for sym in expected_symbols:
            assert hasattr(cryptogram, sym), f"Symbol {sym} missing from cryptogram root package"
            assert sym in cryptogram.__all__, f"Symbol {sym} missing from cryptogram.__all__"
            assert getattr(cryptogram, sym) is globals()[sym]

