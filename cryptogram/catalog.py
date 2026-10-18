"""Curated catalog of historical Russian military and intelligence dispatches."""
import random
from typing import List, Optional

from cryptogram.models import CatalogItem

RUSSIAN_ALPHABET_SET = frozenset("АБВГДЕЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ")

RAW_CATALOG = [
    # EASY (7 quotes)
    {
        "id": "gagarin_poekhali",
        "title": "Полет Гагарина",
        "text": "Поехали! Чувствую себя отлично, полет проходит нормально.",
        "difficulty": "EASY",
        "category": "historical",
        "source": "Юрий Гагарин, радиопереговоры 12 апреля 1961 г."
    },
    {
        "id": "suvorov_nauka",
        "title": "Наука побеждать",
        "text": "Тяжело в учении — легко в походе! Сам погибай, а товарища выручай.",
        "difficulty": "EASY",
        "category": "military",
        "source": "Александр Суворов, «Наука побеждать»"
    },
    {
        "id": "nevsky_prizyv",
        "title": "Слово Невского",
        "text": "Кто с мечом к нам придет, от меча и погибнет! На том стояла и стоит русская земля.",
        "difficulty": "EASY",
        "category": "historical",
        "source": "Князь Александр Невский"
    },
    {
        "id": "kutuzov_fili",
        "title": "Совет в Филях",
        "text": "С потерей Москвы не потеряна еще Россия! Приказываю отступить.",
        "difficulty": "EASY",
        "category": "military",
        "source": "Михаил Кутузов, военный совет в Филях, 1812 г."
    },
    {
        "id": "mayak_silence",
        "title": "Позывной Маяк",
        "text": "Внимание всем постам: сохранять полное радиомолчание до особого приказа.",
        "difficulty": "EASY",
        "category": "intelligence",
        "source": "Служебная радиограмма наблюдения"
    },
    {
        "id": "sorge_svodka",
        "title": "Радиограмма Рамзая",
        "text": "Центр, передаю сводку: подготовка наступления завершена. Жду подтверждения.",
        "difficulty": "EASY",
        "category": "intelligence",
        "source": "Группа Рихарда Зорге, позывной Рамзай"
    },
    {
        "id": "sokol_zapros",
        "title": "Запрос Сокола",
        "text": "Центр, ответьте Соколу! Наблюдаю перемещение объекта в квадрате семь.",
        "difficulty": "EASY",
        "category": "intelligence",
        "source": "Позывной оперативного поста"
    },

    # MEDIUM (7 quotes)
    {
        "id": "sorge_tokyo_1941",
        "title": "Донесение из Токио",
        "text": "Япония не вступит в войну против СССР до весны 1942 года. Квантунская армия остается на позициях.",
        "difficulty": "MEDIUM",
        "category": "intelligence",
        "source": "Разведдонесение резидентуры Зорге, октябрь 1941 г."
    },
    {
        "id": "informburo_proryv",
        "title": "Сводка Совинформбюро",
        "text": "В течение 18 января наши войска нанесли противнику сокрушительный удар и овладели узлом обороны.",
        "difficulty": "MEDIUM",
        "category": "military",
        "source": "Совинформбюро, оперативная сводка 1943 г."
    },
    {
        "id": "alex_yustas_order",
        "title": "Юстас — Алексу",
        "text": "Штаб подтверждает получение чертежей. Агенту Клаусу сменить явочную квартиру немедленно.",
        "difficulty": "MEDIUM",
        "category": "intelligence",
        "source": "Радиошифровка Центра резиденту"
    },
    {
        "id": "intercept_nord_convoy",
        "title": "Перехват «Норд»",
        "text": "Шторм усиливается. Транспорт с грузом специального назначения изменил курс на Мурманск.",
        "difficulty": "MEDIUM",
        "category": "intelligence",
        "source": "Северный морской перехват радиограммы"
    },
    {
        "id": "orden_maskirovka",
        "title": "Штабной приказ",
        "text": "Командирам дивизий обеспечить скрытную передислокацию техники в ночное время без включения фар.",
        "difficulty": "MEDIUM",
        "category": "military",
        "source": "Директива штаба фронта по маскировке"
    },
    {
        "id": "recon_pass_height",
        "title": "Высота 1402",
        "text": "Группе разведки занять высоту 1402 до рассвета. Связь держать через запасной радиоканал.",
        "difficulty": "MEDIUM",
        "category": "military",
        "source": "Боевое распоряжение разведроте"
    },
    {
        "id": "radio_molchanie",
        "title": "Приказ о тишине",
        "text": "Приказываю немедленно прекратить радиопередачи и перейти на резервную частоту связи.",
        "difficulty": "MEDIUM",
        "category": "military",
        "source": "Инструкция радиоэлектронной борьбы"
    },

    # HARD (6 quotes)
    {
        "id": "stavka_1914_corps",
        "title": "Депеша Ставки 1914",
        "text": "Верховному главнокомандующему: второй корпус завершил маневр, железнодорожная станция занята.",
        "difficulty": "HARD",
        "category": "historical",
        "source": "Полевая телеграмма Ставки верховного главнокомандующего, 1914 г."
    },
    {
        "id": "cipher_eshelon_429",
        "title": "Шифротелеграмма 429",
        "text": "Шифротелеграмма номер 429: эшелон с боеприпасами прибывает в пункт разгрузки в полночь.",
        "difficulty": "HARD",
        "category": "intelligence",
        "source": "Штаб тылового обеспечения армейской группы"
    },
    {
        "id": "polar_chelyuskin",
        "title": "Мыс Челюскин",
        "text": "Ледокол пробился сквозь торосы к мысу Челюскин. Все научные приборы и радиостанция в строю.",
        "difficulty": "HARD",
        "category": "historical",
        "source": "Полярная экспедиция Главсевморпути"
    },
    {
        "id": "fortress_last_round",
        "title": "Оборона крепости",
        "text": "Связь с правым флангом прервана. Боекомплект на исходе, держим оборону до последнего патрона.",
        "difficulty": "HARD",
        "category": "military",
        "source": "Журнал боевых действий гарнизона"
    },
    {
        "id": "kuznetsov_order_one",
        "title": "Готовность номер один",
        "text": "Флотам немедленно перейти на оперативную готовность номер один. Оружие держать наготове.",
        "difficulty": "HARD",
        "category": "military",
        "source": "Приказ Наркома ВМФ СССР Н.Г. Кузнецова в ночь на 22 июня 1941 г."
    },
    {
        "id": "vostok_terminator",
        "title": "Орбитальный перехват",
        "text": "Бортовой журнал зафиксировал прохождение терминатора. Самочувствие бодрое, приборы работают точно.",
        "difficulty": "HARD",
        "category": "historical",
        "source": "Бортовой журнал первого пилотируемого космического корабля"
    }
]

# Build immutable CatalogItem instances
CATALOG_QUOTES: List[CatalogItem] = [
    CatalogItem(
        id=item["id"],
        title=item["title"],
        text=item["text"],
        difficulty=item["difficulty"],
        category=item["category"],
        source=item["source"],
        char_count=len(item["text"]),
        unique_letters_count=len(
            set(c for c in item["text"].upper().replace("Ё", "Е") if c in RUSSIAN_ALPHABET_SET)
        ),
    )
    for item in RAW_CATALOG
]


def get_catalog() -> List[CatalogItem]:
    """Return complete list of catalog items."""
    return CATALOG_QUOTES


def get_catalog_item(item_id: str) -> Optional[CatalogItem]:
    """Retrieve catalog item by unique ID (case-insensitive)."""
    item_id_clean = item_id.strip().lower()
    for item in CATALOG_QUOTES:
        if item.id.lower() == item_id_clean:
            return item
    return None


def get_catalog_item_by_id(item_id: str) -> Optional[CatalogItem]:
    """Alias for get_catalog_item adhering to interface specifications."""
    return get_catalog_item(item_id)


def get_catalog_by_difficulty(difficulty: str) -> List[CatalogItem]:
    """Filter catalog items by difficulty ('EASY', 'MEDIUM', 'HARD')."""
    diff_lower = difficulty.strip().lower()
    return [item for item in CATALOG_QUOTES if item.difficulty.lower() == diff_lower]


def get_random_catalog_item(
    difficulty: Optional[str] = None,
    seed: Optional[int] = None
) -> CatalogItem:
    """Select a random quote optionally filtered by difficulty and seeded for repeatability."""
    pool = get_catalog_by_difficulty(difficulty) if difficulty else CATALOG_QUOTES
    if not pool:
        raise ValueError(f"Нет доступных цитат для уровня сложности: {difficulty}")
    rng = random.Random(seed)
    return rng.choice(pool)
