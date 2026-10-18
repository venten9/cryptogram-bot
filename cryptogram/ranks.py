"""Player Military Rank Hierarchy, Experience Points (EXP) Calculation, and Progress Bar Engine.

Implements the official 19-tier military rank system (from Private to Marshal),
dynamic EXP reward calculations with 'Clean Work' and 'Autonomy' bonuses,
and Unicode progress bar formatting for player military dossiers.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class RankInfo:
    """Immutable representation of a military rank tier."""

    level: int               # 1-indexed rank level (1..19)
    name_ru: str             # Official Russian military title
    threshold_exp: int       # Minimum cumulative EXP required for this rank
    badge: str               # Visual emoji badge / insignia

    @property
    def title(self) -> str:
        """Alias for name_ru to guarantee backward and cross-module compatibility."""
        return self.name_ru


@dataclass(frozen=True)
class ExpBreakdown:
    """Immutable detailed breakdown of experience points earned in a mission."""

    base_exp: int            # Base reward according to cipher difficulty
    clean_work_bonus: int    # Bonus for completing mission with zero mistakes (+50)
    autonomy_bonus: int      # Bonus for completing mission without hints (+50)
    total_exp: int           # Sum total of base EXP and all applicable bonuses


# 19 Military Ranks defined by the authoritative military hierarchy
RANKS: List[RankInfo] = [
    RankInfo(level=1, name_ru="Рядовой", threshold_exp=0, badge="🪖"),
    RankInfo(level=2, name_ru="Ефрейтор", threshold_exp=100, badge="🎗️"),
    RankInfo(level=3, name_ru="Младший сержант", threshold_exp=250, badge="🥉"),
    RankInfo(level=4, name_ru="Сержант", threshold_exp=450, badge="🥈"),
    RankInfo(level=5, name_ru="Старший сержант", threshold_exp=700, badge="🥇"),
    RankInfo(level=6, name_ru="Старшина", threshold_exp=1000, badge="🎖️"),
    RankInfo(level=7, name_ru="Прапорщик", threshold_exp=1400, badge="🔷"),
    RankInfo(level=8, name_ru="Младший лейтенант", threshold_exp=1900, badge="⭐"),
    RankInfo(level=9, name_ru="Лейтенант", threshold_exp=2500, badge="⭐⭐"),
    RankInfo(level=10, name_ru="Старший лейтенант", threshold_exp=3200, badge="⭐⭐⭐"),
    RankInfo(level=11, name_ru="Капитан", threshold_exp=4000, badge="⭐⭐⭐⭐"),
    RankInfo(level=12, name_ru="Майор", threshold_exp=5000, badge="🌟"),
    RankInfo(level=13, name_ru="Подполковник", threshold_exp=6500, badge="🌟🌟"),
    RankInfo(level=14, name_ru="Полковник", threshold_exp=8500, badge="🌟🌟🌟"),
    RankInfo(level=15, name_ru="Генерал-майор", threshold_exp=11000, badge="⚔️⭐"),
    RankInfo(level=16, name_ru="Генерал-лейтенант", threshold_exp=14500, badge="⚔️⭐⭐"),
    RankInfo(level=17, name_ru="Генерал-полковник", threshold_exp=19000, badge="⚔️⭐⭐⭐"),
    RankInfo(level=18, name_ru="Генерал армии", threshold_exp=25000, badge="🦅⭐"),
    RankInfo(level=19, name_ru="Маршал", threshold_exp=35000, badge="👑⭐"),
]

# Quick level-to-rank lookup map
_LEVEL_TO_RANK: Dict[int, RankInfo] = {rank.level: rank for rank in RANKS}

# Base EXP matrix for catalog difficulties
DIFFICULTY_BASE_EXP: Dict[str, int] = {
    "EASY": 100,
    "MEDIUM": 200,
    "HARD": 300,
}

CLEAN_WORK_BONUS_EXP: int = 50
AUTONOMY_BONUS_EXP: int = 50
MAX_RANK_THRESHOLD: int = 35000


def get_rank(exp: int) -> RankInfo:
    """Determine the player's military rank based on cumulative EXP.

    Args:
        exp: Player's current experience points. Negative values are clamped to 0.

    Returns:
        The highest RankInfo whose threshold_exp is <= exp.
    """
    if exp <= 0:
        return RANKS[0]

    # Iterate in descending threshold order
    for rank in reversed(RANKS):
        if exp >= rank.threshold_exp:
            return rank

    return RANKS[0]


def get_next_rank(exp: int) -> Optional[RankInfo]:
    """Retrieve the next rank tier the player is working towards.

    Args:
        exp: Player's current experience points.

    Returns:
        RankInfo for the next tier, or None if the player has achieved Marshal (rank 19).
    """
    current_rank = get_rank(exp)
    if current_rank.level >= len(RANKS):
        return None
    return _LEVEL_TO_RANK.get(current_rank.level + 1)


def calculate_rank_progress(exp: int) -> Tuple[float, Optional[int], str]:
    """Calculate progress percentage towards next rank, next threshold, and visual progress bar.

    Args:
        exp: Player's current experience points.

    Returns:
        Tuple of:
          - progress_pct (float): Percentage of progress in current tier (0.0 to 100.0)
          - next_rank_exp (Optional[int]): EXP threshold of next rank, or None for Marshal
          - progress_bar_str (str): 10-segment Unicode progress bar e.g. '[██████░░░░] 60%'
    """
    current_rank = get_rank(exp)
    next_rank = get_next_rank(exp)

    if next_rank is None:
        # Highest rank achieved (Маршал)
        return 100.0, None, "[██████████] 100%"

    tier_start = current_rank.threshold_exp
    tier_end = next_rank.threshold_exp
    tier_span = tier_end - tier_start

    safe_exp = max(0, exp)
    progress_within_tier = max(0, safe_exp - tier_start)

    if tier_span <= 0:
        pct = 0.0
    else:
        raw_pct = (progress_within_tier / tier_span) * 100.0
        pct = round(max(0.0, min(100.0, raw_pct)), 1)

    filled_blocks = min(10, max(0, int(pct // 10)))
    empty_blocks = 10 - filled_blocks
    bar_str = f"[{'█' * filled_blocks}{'░' * empty_blocks}] {int(pct)}%"

    return pct, tier_end, bar_str


def calculate_mission_exp(
    difficulty: str,
    mistakes_count: int,
    hints_used: int,
    is_custom: bool = False,
) -> ExpBreakdown:
    """Calculate experience points earned for completing a mission.

    Args:
        difficulty: Quote difficulty string ("EASY", "MEDIUM", "HARD"). Case-insensitive.
        mistakes_count: Number of wrong guesses made during the session.
        hints_used: Number of hints consumed during the session.
        is_custom: True if quote was user-authored (custom text defaults to MEDIUM base EXP).

    Returns:
        ExpBreakdown containing base EXP, bonuses, and grand total.
    """
    diff_key = (difficulty or "").strip().upper()

    if is_custom or diff_key not in DIFFICULTY_BASE_EXP:
        base_exp = DIFFICULTY_BASE_EXP["MEDIUM"]
    else:
        base_exp = DIFFICULTY_BASE_EXP[diff_key]

    clean_bonus = CLEAN_WORK_BONUS_EXP if mistakes_count <= 0 else 0
    autonomy_bonus = AUTONOMY_BONUS_EXP if hints_used <= 0 else 0

    total = base_exp + clean_bonus + autonomy_bonus

    return ExpBreakdown(
        base_exp=base_exp,
        clean_work_bonus=clean_bonus,
        autonomy_bonus=autonomy_bonus,
        total_exp=total,
    )
