"""Authentic historical campaign chapters and cryptogram data (1941–1945).

Defines the 5 historical story campaign chapters based on authentic declassified
Soviet High Command directives, orders, and debriefings from the Great Patriotic War.
"""
from dataclasses import dataclass
from typing import Dict, List, Optional

from cryptogram.engine import (
    RUSSIAN_ALPHABET_SET,
    generate_cipher,
    normalize_text,
    validate_text,
)

TOTAL_CHAPTERS: int = 5
CAMPAIGN_TITLE: str = "Сюжетная кампания: Секретные директивы Ставки (1941–1945)"


@dataclass(frozen=True)
class CampaignChapter:
    """Historical story campaign chapter model.
    
    Contains operational background, authentic directive text, metadata,
    and post-decryption victory debriefing.
    """
    id: int  # 1..5
    year: int  # 1941..1945
    date_str: str  # e.g. "22 июня 1941 г."
    title: str  # e.g. "Внезапный удар"
    issuing_body: str  # e.g. "Наркомат обороны СССР"
    document_name: str  # e.g. "Директива № 1 Наркомата обороны"
    operational_context: str  # Historical background / briefing text
    directive_text: str  # Authentic order text (30-200 chars, Russian)
    victory_summary: str  # Historical debriefing after decryption

    @property
    def clean_text(self) -> str:
        """Authentic directive text stripped of outer quotation marks and extra whitespace."""
        return self.directive_text.strip("«» \t\r\n")

    @property
    def cipher_text(self) -> str:
        """Alias for clean_text for use in cipher generation and game sessions."""
        return self.clean_text

    @property
    def full_title(self) -> str:
        """Formatted title e.g. 'Глава 1: Внезапный удар (1941)'."""
        return f"Глава {self.id}: {self.title} ({self.year})"

    @property
    def catalog_id(self) -> str:
        """Catalog identifier for game session integration."""
        return f"campaign_ch{self.id}"

    @property
    def char_count(self) -> int:
        """Total character length of the clean decryptable directive."""
        return len(self.clean_text)

    @property
    def unique_letters_count(self) -> int:
        """Count of unique Cyrillic letters to decrypt."""
        norm = normalize_text(self.clean_text)
        return len(set(c for c in norm if c in RUSSIAN_ALPHABET_SET))

    @property
    def text(self) -> str:
        """Alias for clean_text for catalog/engine compatibility."""
        return self.clean_text

    @property
    def source(self) -> str:
        """Issuing body and document description."""
        return f"{self.issuing_body}, {self.document_name}"

    @property
    def difficulty(self) -> str:
        """Difficulty level for EXP calculation and UI badges."""
        return "HARD"


CHAPTER_1 = CampaignChapter(
    id=1,
    year=1941,
    date_str="22 июня 1941 г.",
    title="Внезапный удар",
    issuing_body="Наркомат обороны СССР",
    document_name="Директива № 1 Наркомата обороны",
    operational_context=(
        "В ночь на 22 июня 1941 года пограничные военные округа получили экстренную "
        "директиву из Москвы за подписями Наркома обороны С.К. Тимошенко и Начальника "
        "Генерального штаба Г.К. Жукова. Немецко-фашистские войска сосредоточены на "
        "советской границе, разведка фиксирует неминуемое вторжение. Войскам приказано "
        "привести части в полную боевую готовность и встретить удар врага во всеоружии."
    ),
    directive_text=(
        "«В течение 22–23 июня возможно внезапное нападение. Задача наших войск — "
        "быть в полной боевой готовности встретить удар.»"
    ),
    victory_summary=(
        "Директива № 1 была передана в войска всего за несколько часов до вероломного "
        "вторжения врага. Несмотря на тяжелейшие условия первых приграничных боев лета "
        "1941 года, беспримерный героизм и самоотверженность советских воинов сорвали "
        "гитлеровский план молниеносной войны «Барбаросса»."
    ),
)

CHAPTER_2 = CampaignChapter(
    id=2,
    year=1942,
    date_str="28 июля 1942 г.",
    title="Сталинград",
    issuing_body="Народный комиссариат обороны СССР",
    document_name="Приказ НКО СССР № 227",
    operational_context=(
        "Лето 1942 года. Прорвав оборону на юго-западном направлении, немецкие "
        "полчища рвутся к Волге и кавказской нефти, создавая смертельную угрозу "
        "существованию Родины. В этот критический момент Народный комиссар обороны "
        "И.В. Сталин издает исторический приказ № 227, требующий железной стойкости "
        "и категорически запрещающий несанкционированный отход."
    ),
    directive_text=(
        "«Отступать дальше — значит загубить себя и нашу Родину. Каждый командир и "
        "боец обязан понять: ни шагу назад!»"
    ),
    victory_summary=(
        "Приказ № 227 «Ни шагу назад!» стал стальным стержнем обороны Сталинграда. "
        "Защитники волжской твердыни стояли насмерть за каждую улицу и дом, превратив "
        "руины города в неприступную крепость и перемолов отборные армии врага."
    ),
)

CHAPTER_3 = CampaignChapter(
    id=3,
    year=1942,
    date_str="Ноябрь 1942 г.",
    title="Операция «Уран»",
    issuing_body="Ставка Верховного Главнокомандования",
    document_name="Директива Ставки ВГК",
    operational_context=(
        "Осень 1942 года. В глубочайшей тайне Генеральный штаб и Ставка ВГК под "
        "руководством Г.К. Жукова и А.М. Василевского разработали план грандиозного "
        "контрнаступления — операцию «Уран». Войскам Юго-Западного, Донского и "
        "Сталинградского фронтов предстоит прорвать фланги противника и замкнуть "
        "кольцо вокруг вражеской группировки."
    ),
    directive_text=(
        "«Войскам фронтов перейти в решительное наступление с целью окружения и "
        "полного разгрома сталинградской группировки врага.»"
    ),
    victory_summary=(
        "19–23 ноября 1942 года стальные клинья советских фронтов сокрушили оборону "
        "противника и соединились в районе Калача-на-Дону, замкнув в кольцо 330-тысячную "
        "6-ю армию Паулюса. Разгром врага на Волге положил начало коренному перелому "
        "во всей Второй мировой войне."
    ),
)

CHAPTER_4 = CampaignChapter(
    id=4,
    year=1943,
    date_str="Июль 1943 г.",
    title="Курская дуга",
    issuing_body="Ставка Верховного Главнокомандования",
    document_name="План контрнаступления Ставки",
    operational_context=(
        "Лето 1943 года. Командование вермахта планирует операцию «Цитадель», рассчитывая "
        "срезать Курский выступ и вернуть стратегическую инициативу с помощью новых танков "
        "«Тигр» и «Пантера». Советская разведка вскрыла планы врага, позволив Ставке создать "
        "мощнейшую эшелонированную оборону и подготовить сокрушительный контрудар."
    ),
    directive_text=(
        "«Главная задача наших войск — измотать ударные бронетанковые клинья противника "
        "и перейти в генеральное контрнаступление.»"
    ),
    victory_summary=(
        "Грандиозная битва на Курской дуге и легендарное танковое сражение под Прохоровкой "
        "сокрушили бронетанковую мощь нацистской Германии. Красная Армия перешла в "
        "победоносное наступление, освободив Орел, Белгород и Харьков. Инициатива навсегда "
        "перешла к СССР."
    ),
)

CHAPTER_5 = CampaignChapter(
    id=5,
    year=1945,
    date_str="Апрель 1945 г.",
    title="Штурм Берлина",
    issuing_body="Командование 1-го Белорусского фронта",
    document_name="Боевой приказ командующего фронтом",
    operational_context=(
        "Апрель 1945 года. Советские войска вышли на ближние подступы к столице Третьего "
        "рейха. Впереди — решающий штурм мощнейшего укрепленного района врага. Войскам "
        "1-го Белорусского фронта под командованием Маршала Г.К. Жукова отдается исторический "
        "боевой приказ на сокрушение обороны противника и взятие Берлина."
    ),
    directive_text=(
        "«Войскам фронта сломить сопротивление на подступах к городу, овладеть Берлином "
        "и водрузить Знамя Победы над рейхстагом.»"
    ),
    victory_summary=(
        "В ходе яростного штурма советские воины преодолели ожесточенное сопротивление "
        "гарнизона Берлина. 30 апреля над поверженным Рейхстагом взвилось священное "
        "Знамя Победы. 8 мая 1945 года была подписана безоговорочная капитуляция "
        "нацистской Германии."
    ),
)

CAMPAIGN_CHAPTERS: Dict[int, CampaignChapter] = {
    1: CHAPTER_1,
    2: CHAPTER_2,
    3: CHAPTER_3,
    4: CHAPTER_4,
    5: CHAPTER_5,
}


def get_campaign_chapter(chapter_id: int) -> Optional[CampaignChapter]:
    """Retrieve a campaign chapter by its integer ID (1..5).
    
    Returns None if chapter_id is not found or is invalid.
    """
    try:
        cid = int(chapter_id)
    except (TypeError, ValueError):
        return None
    return CAMPAIGN_CHAPTERS.get(cid)


def get_all_campaign_chapters() -> List[CampaignChapter]:
    """Return all campaign chapters sorted by sequential ID (1..5)."""
    return sorted(CAMPAIGN_CHAPTERS.values(), key=lambda ch: ch.id)


def validate_chapter(chapter: CampaignChapter, raise_on_error: bool = False) -> bool:
    """Validate a single campaign chapter against cryptographic engine requirements.
    
    Checks:
    1. Object is a CampaignChapter instance.
    2. Chapter ID is between 1 and 5.
    3. clean_text satisfies length bounds (30..200 characters).
    4. clean_text contains only Cyrillic letters (no Latin).
    5. clean_text contains at least 3 unique Cyrillic letters.
    6. generate_cipher successfully produces a bijective cipher mapping.
    
    Returns True if valid. If invalid, raises ValueError when raise_on_error is True,
    or returns False.
    """
    if not isinstance(chapter, CampaignChapter):
        if raise_on_error:
            raise TypeError(f"Expected CampaignChapter, got {type(chapter).__name__}")
        return False

    if chapter.id < 1 or chapter.id > TOTAL_CHAPTERS:
        if raise_on_error:
            raise ValueError(f"Invalid chapter id: {chapter.id} (expected 1..{TOTAL_CHAPTERS})")
        return False

    val = validate_text(
        chapter.clean_text,
        min_length=30,
        max_length=200,
        min_unique_letters=3,
        disallow_latin=True,
    )
    if not val.is_valid:
        if raise_on_error:
            raise ValueError(f"Chapter {chapter.id} validation failed: {val.error_message}")
        return False

    try:
        cipher = generate_cipher(chapter.clean_text, seed=chapter.id * 100)
        if not cipher or not cipher.char_to_code:
            if raise_on_error:
                raise ValueError(f"Chapter {chapter.id} failed to generate cipher")
            return False
    except Exception as exc:
        if raise_on_error:
            raise ValueError(f"Chapter {chapter.id} cipher generation exception: {exc}") from exc
        return False

    return True


def validate_all_chapters(raise_on_error: bool = False) -> bool:
    """Validate all registered campaign chapters against engine rules.
    
    Verifies that:
    1. Exactly TOTAL_CHAPTERS (5) chapters are defined with IDs 1..5.
    2. Every chapter passes validate_chapter checks.
    
    Returns True if all chapters are valid. If any chapter fails, returns False
    or raises ValueError if raise_on_error is True.
    """
    if len(CAMPAIGN_CHAPTERS) != TOTAL_CHAPTERS:
        if raise_on_error:
            raise ValueError(
                f"Expected {TOTAL_CHAPTERS} chapters, found {len(CAMPAIGN_CHAPTERS)}"
            )
        return False

    for ch_id in range(1, TOTAL_CHAPTERS + 1):
        chapter = CAMPAIGN_CHAPTERS.get(ch_id)
        if not chapter:
            if raise_on_error:
                raise ValueError(f"Missing chapter with id {ch_id}")
            return False
        if not validate_chapter(chapter, raise_on_error=raise_on_error):
            return False

    return True


__all__ = [
    "CampaignChapter",
    "CAMPAIGN_CHAPTERS",
    "TOTAL_CHAPTERS",
    "CAMPAIGN_TITLE",
    "CHAPTER_1",
    "CHAPTER_2",
    "CHAPTER_3",
    "CHAPTER_4",
    "CHAPTER_5",
    "get_campaign_chapter",
    "get_all_campaign_chapters",
    "validate_chapter",
    "validate_all_chapters",
]
