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
    },

    # Additional Classified Military & Intelligence Dispatches (40 quotes)
    {
        "id": "granit_perekhvat",
        "title": "Перехват у реки",
        "text": "Приказываю перехватить курьера с шифровальными таблицами до пересечения границы у реки.",
        "difficulty": "EASY",
        "category": "intelligence",
        "source": "Особый отдел штаба фронта"
    },
    {
        "id": "prikaz_buran_7",
        "title": "Директива Буран",
        "text": "Всем постам наблюдения: немедленно заблокировать железнодорожный узел и задержать эшелон номер три.",
        "difficulty": "EASY",
        "category": "military",
        "source": "Секретное распоряжение комендатуры"
    },
    {
        "id": "operaciya_tuman",
        "title": "Операция Туман",
        "text": "Директива особой важности: объект ликвидировать, архив сжечь, агентурную сеть законсервировать.",
        "difficulty": "EASY",
        "category": "intelligence",
        "source": "Управление нелегальной разведки"
    },
    {
        "id": "zagrad_delta",
        "title": "Северный перевал",
        "text": "Установить минные заграждения на северном перевале. Проход закрыть для всех видов транспорта.",
        "difficulty": "EASY",
        "category": "military",
        "source": "Боевой приказ саперному батальону"
    },
    {
        "id": "shifr_sever_12",
        "title": "Шифровка Север",
        "text": "Перехожу на глухой режим связи. Встреча с источником переносится в конспиративный дом на набережной.",
        "difficulty": "EASY",
        "category": "intelligence",
        "source": "Шифровка резидентуры «Север»"
    },
    {
        "id": "kordon_reid",
        "title": "Кордон на шоссе",
        "text": "Срочно перекрыть шоссе на рассвете. Досматривать каждый армейский грузовик с комендантским пропуском.",
        "difficulty": "EASY",
        "category": "military",
        "source": "Циркуляр военной комендатуры"
    },
    {
        "id": "uran_sekret_prikaz",
        "title": "Спецхран",
        "text": "Конвою соблюдать абсолютную скрытность при погрузке контейнеров с маркировкой спецхран.",
        "difficulty": "EASY",
        "category": "military",
        "source": "Гриф «Совершенно секретно», спецотдел"
    },
    {
        "id": "zaton_lodka_3",
        "title": "Квадрат сорок два",
        "text": "Подводной лодке лечь на грунт в квадрате сорок два и ждать условного гидроакустического сигнала.",
        "difficulty": "EASY",
        "category": "military",
        "source": "Боевое распоряжение штаба флота"
    },
    {
        "id": "yavka_berlin_45",
        "title": "Пароль для связного",
        "text": "Пароль для связного: чайка летит на запад. Отзыв: море спокойно лишь до полуночи.",
        "difficulty": "EASY",
        "category": "intelligence",
        "source": "Инструкция связи глубокого прикрытия"
    },
    {
        "id": "bunker_kod_omega",
        "title": "Код Омега",
        "text": "Коменданту бункера: запечатать нижние шлюзы и перевести охрану на режим боевой тревоги.",
        "difficulty": "EASY",
        "category": "military",
        "source": "Директива комендатуре спецобъекта"
    },
    {
        "id": "radist_veter",
        "title": "Позывной Ветер",
        "text": "Выхожу в эфир на запасной волне. Пеленгаторы противника прочесывают лесной массив.",
        "difficulty": "EASY",
        "category": "intelligence",
        "source": "Донесение полевой разведгруппы"
    },
    {
        "id": "diversiya_most",
        "title": "Мост через каньон",
        "text": "Группе подрывников выдвинуться к мосту через каньон. Подрыв назначить ровно на четыре утра.",
        "difficulty": "EASY",
        "category": "military",
        "source": "Оперативная задача диверсионному отряду"
    },
    {
        "id": "kaskad_perehvat",
        "title": "Группа Каскад",
        "text": "Секретные формулы доставлены в бункер связи. Жду дальнейших инструкций от Центра.",
        "difficulty": "EASY",
        "category": "intelligence",
        "source": "Шифротелеграмма оперативной группы «Каскад»"
    },
    {
        "id": "smersh_krot_44",
        "title": "Сигнал СМЕРШ",
        "text": "В штабе армии действует крот противника. Доступ к оперативным картам временно ограничить узким кругом лиц.",
        "difficulty": "MEDIUM",
        "category": "intelligence",
        "source": "Особый отдел контрразведки СМЕРШ"
    },
    {
        "id": "protokol_vympel",
        "title": "Протокол Вымпел",
        "text": "Активировать протокол Вымпел. Всем законсервированным ячейкам проверить тайники и ждать курьера.",
        "difficulty": "MEDIUM",
        "category": "intelligence",
        "source": "Шифрованное предписание управления спецопераций"
    },
    {
        "id": "shtab_proryv",
        "title": "Огневой вал",
        "text": "Противник стягивает тяжелую бронетехнику к переправе. Артиллерии подготовить массированный огневой вал.",
        "difficulty": "MEDIUM",
        "category": "military",
        "source": "Оперативная директива командующего фронтом"
    },
    {
        "id": "glubina_rezident_2",
        "title": "Донесение из столицы",
        "text": "Наш агент в министерстве передал копию секретного протокола переговоров. Документ заложен в тайник.",
        "difficulty": "MEDIUM",
        "category": "intelligence",
        "source": "Шифровка внешней разведки из столицы"
    },
    {
        "id": "arsenal_groza_8",
        "title": "Операция Гроза",
        "text": "Начальнику арсенала: выдать штурмовым батальонам резервные запасы бесшумного оружия и спецсвязи.",
        "difficulty": "MEDIUM",
        "category": "military",
        "source": "Секретный наряд управления вооружений"
    },
    {
        "id": "rubin_evakuaciya",
        "title": "Объект Рубин",
        "text": "В случае угрозы захвата лаборатории приказываю уничтожить все образцы сыворотки и расчетные тетради.",
        "difficulty": "MEDIUM",
        "category": "intelligence",
        "source": "Особая инструкция объекту «Рубин»"
    },
    {
        "id": "vintorez_ohota",
        "title": "Ликвидация связи",
        "text": "Снайперской паре ликвидировать офицера связи противника при выходе из штабного автомобиля.",
        "difficulty": "MEDIUM",
        "category": "military",
        "source": "Боевое задание армейского спецназа"
    },
    {
        "id": "desant_placdarm",
        "title": "Захват аэродрома",
        "text": "Парашютному десанту захватить аэродром противника и удерживать взлетную полосу до подхода главных сил.",
        "difficulty": "MEDIUM",
        "category": "military",
        "source": "Приказ командующего воздушно-десантными войсками"
    },
    {
        "id": "shifr_monolit_19",
        "title": "Сектор девять",
        "text": "Зафиксирован несанкционированный сеанс радиосвязи из сектора девять. Направить группу радиоперехвата.",
        "difficulty": "MEDIUM",
        "category": "intelligence",
        "source": "Доклад службы радиоэлектронной безопасности"
    },
    {
        "id": "zagadka_kaspij",
        "title": "Досмотр в море",
        "text": "Транспортное судно под нейтральным флагом перевозит ящики со спецгрузом. Досмотр провести в открытом море.",
        "difficulty": "MEDIUM",
        "category": "military",
        "source": "Секретное распоряжение пограничной флотилии"
    },
    {
        "id": "argon_svyaz",
        "title": "Резидентура Аргон",
        "text": "Связной провалил явку. Немедленно покинуть конспиративную квартиру через запасной пожарный выход.",
        "difficulty": "MEDIUM",
        "category": "intelligence",
        "source": "Экстренная депеша резидентуры «Аргон»"
    },
    {
        "id": "zasada_pereval",
        "title": "Танковая засада",
        "text": "Бронеколонне прекратить движение и занять круговую оборону в ущелье. Впереди обнаружена танковая засада.",
        "difficulty": "MEDIUM",
        "category": "military",
        "source": "Радиограмма передового дозора"
    },
    {
        "id": "kod_shifr_alfa",
        "title": "Уничтожение блокнота",
        "text": "Использованный шифроблокнот подлежит немедленному сожжению в присутствии дежурного офицера спецчасти.",
        "difficulty": "MEDIUM",
        "category": "intelligence",
        "source": "Регламент восьмого управления Генерального штаба"
    },
    {
        "id": "bastion_centr",
        "title": "Удар по узлу связи",
        "text": "Авиационной эскадрилье нанести точечный удар по узлу связи противника в момент начала артподготовки.",
        "difficulty": "MEDIUM",
        "category": "military",
        "source": "Боевое распоряжение штаба воздушной армии"
    },
    {
        "id": "prikaz_stavka_0042",
        "title": "Приказ номер 0042",
        "text": "Командующим армиями: за самовольный отход с оборонительного рубежа командиров частей предавать суду трибунала.",
        "difficulty": "HARD",
        "category": "historical",
        "source": "Приказ Ставки Верховного Главнокомандования №0042"
    },
    {
        "id": "shifr_glubina_7",
        "title": "Пуск из глубины",
        "text": "Атомной субмарине произвести учебный пуск баллистической ракеты из подводного положения в заданном районе.",
        "difficulty": "HARD",
        "category": "military",
        "source": "Директива Главного штаба Военно-морского флота"
    },
    {
        "id": "smersh_zagran",
        "title": "Захват абвера",
        "text": "Вскрыта диверсионная резидентура абвера. Изъяты портативная рация, фальшивые воинские билеты и яд в ампулах.",
        "difficulty": "HARD",
        "category": "historical",
        "source": "Спецсообщение контрразведки Народному комиссару"
    },
    {
        "id": "uran_obekt_40",
        "title": "Объект сорок",
        "text": "Допуск в экспериментальный сектор разрешен исключительно по спецпропускам первой категории за двумя подписями.",
        "difficulty": "HARD",
        "category": "intelligence",
        "source": "Режимный циркуляр Первого главного управления"
    },
    {
        "id": "perehvat_enigma",
        "title": "Дешифровальный отдел",
        "text": "Аналитикам дешифровального отдела: восстановить разорванную перфоленту телетайпа с донесением командующего армией.",
        "difficulty": "HARD",
        "category": "intelligence",
        "source": "Служебная записка специальной службы криптоанализа"
    },
    {
        "id": "diversant_volk",
        "title": "Десант в тылу",
        "text": "Парашютная группа специального назначения десантирована в глубоком тылу противника без опознавательных знаков.",
        "difficulty": "HARD",
        "category": "military",
        "source": "Оперативная сводка разведывательного управления"
    },
    {
        "id": "fortpost_tayna",
        "title": "Подземный форт",
        "text": "Гарнизону подземного форта подготовить заряды к уничтожению шахт в случае невозможности удержания рубежа.",
        "difficulty": "HARD",
        "category": "military",
        "source": "Секретный приказ коменданту укрепрайона"
    },
    {
        "id": "radist_prizrak",
        "title": "Неизвестный передатчик",
        "text": "Пеленг зафиксировал работу неизвестного передатчика на сверхкоротких волнах в пяти километрах от бункера связи.",
        "difficulty": "HARD",
        "category": "intelligence",
        "source": "Журнал радиоэлектронной разведки дивизии"
    },
    {
        "id": "kremlin_chasy",
        "title": "Эшелон госрезерва",
        "text": "Шифром особой важности: эшелон государственного резерва с золотым запасом отправить по резервной ветке на восток.",
        "difficulty": "HARD",
        "category": "historical",
        "source": "Постановление Государственного комитета обороны"
    },
    {
        "id": "zenit_raketa_30",
        "title": "Огонь на поражение",
        "text": "Ракетным дивизионам противовоздушной обороны открыть огонь по неопознанной высотной цели без предупреждения.",
        "difficulty": "HARD",
        "category": "military",
        "source": "Боевой приказ дежурному КП войск ПВО"
    },
    {
        "id": "agent_orion_vyvod",
        "title": "Эвакуация Ориона",
        "text": "Источник Орион под угрозой разоблачения. Задействовать окно на третьем пограничном участке для срочной эксфильтрации.",
        "difficulty": "HARD",
        "category": "intelligence",
        "source": "План эвакуации центрального аппарата разведки"
    },
    {
        "id": "tuman_spetsnaz",
        "title": "Боевые пловцы",
        "text": "Боевым пловцам проникнуть в гавань военной базы противника и установить магнитные мины на днище флагмана.",
        "difficulty": "HARD",
        "category": "military",
        "source": "Боевое распоряжение отряду подводных диверсантов"
    },
    {
        "id": "posledniy_rubezh",
        "title": "Пакет номер девять",
        "text": "При потере связи с командованием командирам узлов сопротивления действовать автономно по пакету номер девять.",
        "difficulty": "HARD",
        "category": "military",
        "source": "Особый запечатанный пакет Главного штаба войск"
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
