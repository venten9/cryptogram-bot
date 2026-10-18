"""Empirical Challenger 2 Adversarial Test Suite for M1: EXP Engine & Dataclasses.

Empirically challenges:
1. Casing variations and difficulty normalization ("easy", "Medium", "HARD", "custom", "invalid", whitespace, None).
2. Combinations matrix of mistakes (0, 1, 100, negatives, large numbers) and hints (0, 1, 50, negatives, large numbers).
3. Dataclass immutability (FrozenInstanceError on mutation, deletion, new attributes) and hashability.
4. Root package exports from `cryptogram` and preservation of existing exports.
"""
import dataclasses
import pytest

import cryptogram
import cryptogram.ranks as ranks_mod
from cryptogram.ranks import (
    RANKS,
    ExpBreakdown,
    RankInfo,
    calculate_mission_exp,
    calculate_rank_progress,
    get_next_rank,
    get_rank,
    DIFFICULTY_BASE_EXP,
    CLEAN_WORK_BONUS_EXP,
    AUTONOMY_BONUS_EXP,
)


class TestCasingVariationsAndNormalization:
    """Stress-tests casing, whitespace, and difficulty normalization."""

    @pytest.mark.parametrize(
        "difficulty, expected_base",
        [
            ("EASY", 100),
            ("easy", 100),
            ("Easy", 100),
            ("eAsY", 100),
            ("  easy  ", 100),
            ("\teasy\n", 100),
            ("MEDIUM", 200),
            ("medium", 200),
            ("Medium", 200),
            ("mEdIuM", 200),
            ("  medium  ", 200),
            ("\nMEDIUM\r\n", 200),
            ("HARD", 300),
            ("hard", 300),
            ("Hard", 300),
            ("hArD", 300),
            ("  hard  ", 300),
            ("\tHARD\t", 300),
            ("custom", 200),
            ("CUSTOM", 200),
            ("Custom", 200),
            ("invalid", 200),
            ("UNKNOWN", 200),
            ("EXTREME_MODE", 200),
            ("", 200),
            ("   ", 200),
            (None, 200),
        ],
    )
    def test_casing_and_unrecognized_difficulties(self, difficulty, expected_base):
        """Verify casing variations and unknown difficulties normalize properly."""
        res = calculate_mission_exp(difficulty, mistakes_count=0, hints_used=0, is_custom=False)
        assert res.base_exp == expected_base
        assert res.clean_work_bonus == 50
        assert res.autonomy_bonus == 50
        assert res.total_exp == expected_base + 100

    @pytest.mark.parametrize(
        "difficulty",
        ["EASY", "easy", "MEDIUM", "medium", "HARD", "hard", "custom", "invalid", "", None],
    )
    def test_custom_flag_always_overrides_to_medium(self, difficulty):
        """Verify that is_custom=True forces base_exp=200 regardless of difficulty string."""
        res = calculate_mission_exp(difficulty, mistakes_count=0, hints_used=0, is_custom=True)
        assert res.base_exp == 200
        assert res.total_exp == 300


class TestMistakesAndHintsMatrix:
    """Stress-tests combinations of mistakes and hints with invariant checking."""

    @pytest.mark.parametrize(
        "mistakes, hints, expected_clean, expected_auto",
        [
            (0, 0, 50, 50),
            (0, 1, 50, 0),
            (0, 50, 50, 0),
            (1, 0, 0, 50),
            (1, 1, 0, 0),
            (1, 50, 0, 0),
            (100, 0, 0, 50),
            (100, 1, 0, 0),
            (100, 50, 0, 0),
        ],
    )
    def test_authoritative_mistakes_hints_grid(
        self, mistakes, hints, expected_clean, expected_auto
    ):
        """Verify the exact combinations mandated in dispatch: mistakes (0, 1, 100) and hints (0, 1, 50)."""
        res = calculate_mission_exp("HARD", mistakes_count=mistakes, hints_used=hints)
        assert res.base_exp == 300
        assert res.clean_work_bonus == expected_clean
        assert res.autonomy_bonus == expected_auto
        assert res.total_exp == 300 + expected_clean + expected_auto

    def test_exhaustive_combinatorial_grid_search(self):
        """Property-based invariant check across 2,200 parameter combinations."""
        mistakes_grid = [-100, -1, 0, 1, 2, 5, 10, 50, 100, 1000]
        hints_grid = [-50, -1, 0, 1, 2, 5, 10, 50, 100, 1000]
        diffs = ["EASY", "easy", "MEDIUM", "medium", "HARD", "hard", "custom", "invalid", "", "   "]
        custom_flags = [False, True]

        for diff in diffs:
            for is_cust in custom_flags:
                norm_key = (diff or "").strip().upper()
                if is_cust or norm_key not in ("EASY", "MEDIUM", "HARD"):
                    expected_base = 200
                else:
                    expected_base = {"EASY": 100, "MEDIUM": 200, "HARD": 300}[norm_key]

                for m in mistakes_grid:
                    for h in hints_grid:
                        res = calculate_mission_exp(diff, mistakes_count=m, hints_used=h, is_custom=is_cust)

                        # Invariants
                        assert res.base_exp == expected_base
                        assert res.clean_work_bonus == (50 if m <= 0 else 0)
                        assert res.autonomy_bonus == (50 if h <= 0 else 0)
                        assert res.total_exp == res.base_exp + res.clean_work_bonus + res.autonomy_bonus
                        assert 100 <= res.total_exp <= 400

    def test_extreme_numerical_limits(self):
        """Verify huge and negative numbers do not crash or overflow."""
        # 1 billion mistakes and hints
        res = calculate_mission_exp("EASY", mistakes_count=1_000_000_000, hints_used=1_000_000_000)
        assert res.base_exp == 100
        assert res.clean_work_bonus == 0
        assert res.autonomy_bonus == 0
        assert res.total_exp == 100

        # Negative mistakes and hints (defensive <= 0 check)
        res_neg = calculate_mission_exp("HARD", mistakes_count=-999, hints_used=-999)
        assert res_neg.clean_work_bonus == 50
        assert res_neg.autonomy_bonus == 50
        assert res_neg.total_exp == 400


class TestDataclassImmutabilityAndHashability:
    """Verifies that RankInfo and ExpBreakdown are strictly frozen, immutable, and hashable."""

    def test_exp_breakdown_immutability(self):
        """Verify ExpBreakdown rejects attribute modification, deletion, and addition."""
        eb = calculate_mission_exp("EASY", 0, 0)
        for field in ("base_exp", "clean_work_bonus", "autonomy_bonus", "total_exp"):
            with pytest.raises(dataclasses.FrozenInstanceError):
                setattr(eb, field, 9999)
            with pytest.raises(dataclasses.FrozenInstanceError):
                delattr(eb, field)

        with pytest.raises(dataclasses.FrozenInstanceError):
            eb.arbitrary_attr = "attack"

    def test_exp_breakdown_hashable(self):
        """Verify ExpBreakdown can be stored in sets and used as dict keys."""
        eb1 = calculate_mission_exp("EASY", 0, 0)
        eb2 = calculate_mission_exp("EASY", 0, 0)
        assert hash(eb1) == hash(eb2)
        s = {eb1, eb2}
        assert len(s) == 1
        d = {eb1: "val"}
        assert d[eb2] == "val"

    def test_rank_info_immutability_all_19_ranks(self):
        """Verify every RankInfo in RANKS rejects mutation, deletion, and dynamic attributes."""
        for rank in RANKS:
            for field in ("level", "name_ru", "threshold_exp", "badge"):
                with pytest.raises(dataclasses.FrozenInstanceError):
                    setattr(rank, field, "hacked")
                with pytest.raises(dataclasses.FrozenInstanceError):
                    delattr(rank, field)

            with pytest.raises(dataclasses.FrozenInstanceError):
                rank.new_field = 42

    def test_rank_info_hashable(self):
        """Verify RankInfo instances are hashable and valid in sets."""
        ranks_set = set(RANKS)
        assert len(ranks_set) == 19
        for rank in RANKS:
            assert rank in ranks_set
            assert isinstance(hash(rank), int)


class TestRootPackageExports:
    """Verifies all ranks symbols are exported at root cryptogram level without regressions."""

    def test_all_ranks_symbols_present_in_cryptogram(self):
        """Verify symbol presence, __all__ inclusion, and exact identity matching."""
        required_symbols = [
            "ExpBreakdown",
            "RankInfo",
            "RANKS",
            "calculate_mission_exp",
            "calculate_rank_progress",
            "get_next_rank",
            "get_rank",
        ]

        for sym in required_symbols:
            assert hasattr(cryptogram, sym), f"{sym} missing from cryptogram namespace"
            assert sym in cryptogram.__all__, f"{sym} missing from cryptogram.__all__"
            assert getattr(cryptogram, sym) is getattr(ranks_mod, sym)

    def test_legacy_cryptogram_exports_unaffected(self):
        """Verify that cipher engine, catalog, and validation exports are retained."""
        legacy_symbols = [
            "CatalogItem",
            "CipherData",
            "FrequencyItem",
            "GuessResult",
            "GuessStatus",
            "ValidationResult",
            "RUSSIAN_ALPHABET",
            "RUSSIAN_ALPHABET_SET",
            "RUSSIAN_CORPUS_FREQUENCIES",
            "calculate_frequencies",
            "check_victory",
            "generate_cipher",
            "get_unrevealed_codes",
            "normalize_text",
            "render_mask",
            "validate_text",
            "wrap_in_pre",
            "get_catalog",
            "get_catalog_by_difficulty",
            "get_catalog_item",
            "get_catalog_item_by_id",
            "get_random_catalog_item",
        ]

        for sym in legacy_symbols:
            assert hasattr(cryptogram, sym), f"Legacy symbol {sym} missing from cryptogram namespace"
            assert sym in cryptogram.__all__, f"Legacy symbol {sym} missing from cryptogram.__all__"
