"""Aiogram 3.x Inline Keyboard Builders for Cooperative Cryptogram UI."""
import urllib.parse
from typing import Any, Iterable, List, Optional, Set, Union

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import (
    CampaignCallback,
    CatalogCallback,
    HintActionCallback,
    LetterGuessCallback,
    MenuCallback,
    SymbolSelectCallback,
)
from cryptogram.catalog import CatalogItem, get_catalog
from cryptogram.engine import RUSSIAN_ALPHABET, get_unrevealed_codes

CATALOG_PAGE_SIZE = 4
DIFFICULTY_ICONS = {
    "EASY": "🟢",
    "MEDIUM": "🟡",
    "HARD": "🔴",
}


def build_main_menu_keyboard() -> InlineKeyboardMarkup:
    """Build initial main menu keyboard with Solo and Coop options."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text="🎯 Одиночная игра (Соло)",
        callback_data=MenuCallback(action="solo"),
    )
    builder.button(
        text="🎖 Сюжетная кампания (1941–1945)",
        callback_data=CampaignCallback(action="menu", chapter=0),
    )
    builder.button(
        text="📜 Выбрать депешу из каталога (на двоих)",
        callback_data=MenuCallback(action="catalog"),
    )
    builder.button(
        text="✍️ Ввести свой текст (для двоих)",
        callback_data=MenuCallback(action="custom"),
    )
    builder.button(
        text="🎖 Мое досье / Профиль",
        callback_data=MenuCallback(action="profile"),
    )
    builder.button(
        text="ℹ️ Правила игры",
        callback_data=MenuCallback(action="help"),
    )
    builder.adjust(1)
    return builder.as_markup()


def build_solo_difficulty_keyboard() -> InlineKeyboardMarkup:
    """Build difficulty selector for Solo Mode."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text="🟢 Легкая (EASY)",
        callback_data=MenuCallback(action="solo_diff", code="EASY"),
    )
    builder.button(
        text="🟡 Средняя (MEDIUM)",
        callback_data=MenuCallback(action="solo_diff", code="MEDIUM"),
    )
    builder.button(
        text="🔴 Сложная (HARD)",
        callback_data=MenuCallback(action="solo_diff", code="HARD"),
    )
    builder.button(
        text="🎲 Случайная депеша",
        callback_data=MenuCallback(action="solo_diff", code="RANDOM"),
    )
    builder.button(
        text="◀️ Главное меню",
        callback_data=MenuCallback(action="main"),
    )
    builder.adjust(1)
    return builder.as_markup()



def build_catalog_keyboard(
    items: Union[List[CatalogItem], int, None] = None,
    page: int = 0,
    total_pages: Optional[int] = None,
    page_size: int = CATALOG_PAGE_SIZE,
) -> InlineKeyboardMarkup:
    """Build paginated quote catalog keyboard with prev/next navigation."""
    catalog = get_catalog()
    calculated_total = max(1, (len(catalog) + page_size - 1) // page_size)

    if isinstance(items, int):
        page = max(0, min(items, calculated_total - 1))
        items_list = catalog[page * page_size : (page + 1) * page_size]
        total_pages = total_pages if total_pages is not None else calculated_total
    elif isinstance(items, list):
        items_list = items
        page = max(0, min(page, (total_pages or calculated_total) - 1))
        total_pages = total_pages if total_pages is not None else calculated_total
    else:
        page = max(0, min(page, calculated_total - 1))
        items_list = catalog[page * page_size : (page + 1) * page_size]
        total_pages = total_pages if total_pages is not None else calculated_total

    builder = InlineKeyboardBuilder()
    for item in items_list:
        icon = DIFFICULTY_ICONS.get(item.difficulty, "⚪")
        builder.button(
            text=f"{icon} {item.title}",
            callback_data=CatalogCallback(action="pick", item_id=item.id, page=page),
        )
    builder.adjust(1)

    # Navigation pagination bar
    nav_buttons: List[InlineKeyboardButton] = []
    if page > 0:
        nav_buttons.append(
            InlineKeyboardButton(
                text="◀️ Назад",
                callback_data=CatalogCallback(action="page", page=page - 1).pack(),
            )
        )
    else:
        nav_buttons.append(
            InlineKeyboardButton(text="·", callback_data=CatalogCallback(action="noop").pack())
        )

    nav_buttons.append(
        InlineKeyboardButton(
            text=f"📄 {page + 1}/{total_pages}",
            callback_data=CatalogCallback(action="noop").pack(),
        )
    )

    if page < total_pages - 1:
        nav_buttons.append(
            InlineKeyboardButton(
                text="Вперед ▶️",
                callback_data=CatalogCallback(action="page", page=page + 1).pack(),
            )
        )
    else:
        nav_buttons.append(
            InlineKeyboardButton(text="·", callback_data=CatalogCallback(action="noop").pack())
        )

    builder.row(*nav_buttons)
    builder.row(
        InlineKeyboardButton(
            text="✍️ Свой текст", callback_data=MenuCallback(action="custom").pack()
        ),
        InlineKeyboardButton(
            text="🏠 Главное меню", callback_data=MenuCallback(action="main").pack()
        ),
    )
    return builder.as_markup()


def build_invite_keyboard(invite_code: str, bot_username: str) -> InlineKeyboardMarkup:
    """Build invite sharing keyboard with native Telegram share URL and status controls."""
    deep_link = f"https://t.me/{bot_username}?start=game_{invite_code}"
    share_text = (
        f"Присоединяйся к расшифровке секретной радиограммы!\n"
        f"Твоя роль: Полевой Агент.\n"
        f"Код операции: {invite_code}"
    )
    share_url = f"https://t.me/share/url?url={urllib.parse.quote(deep_link)}&text={urllib.parse.quote(share_text)}"

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📨 Отправить приглашение Агенту", url=share_url))
    builder.row(
        InlineKeyboardButton(
            text="🔄 Проверить подключение",
            callback_data=HintActionCallback(code=invite_code, action="refresh").pack(),
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="❌ Отменить миссию",
            callback_data=HintActionCallback(code=invite_code, action="cancel").pack(),
        )
    )
    return builder.as_markup()


def build_analyst_dossier_keyboard(
    session_or_code: Any,
    unrevealed_symbols: Optional[Iterable[int]] = None,
    invite_link: Optional[str] = None,
    bot_username: Optional[str] = None,
) -> InlineKeyboardMarkup:
    """Build tactical controls keyboard for Headquarters Analyst."""
    if hasattr(session_or_code, "invite_code"):
        code = session_or_code.invite_code
        status = getattr(session_or_code, "status", None)
        status_val = getattr(status, "value", str(status))
    else:
        code = str(session_or_code)
        status_val = "IN_PROGRESS"

    builder = InlineKeyboardBuilder()

    if status_val == "WAITING_AGENT":
        if invite_link or bot_username:
            url = invite_link or f"https://t.me/{bot_username}?start=game_{code}"
            share_text = f"Присоединяйся к секретной операции! Код: {code}"
            share_url = f"https://t.me/share/url?url={urllib.parse.quote(url)}&text={urllib.parse.quote(share_text)}"
            builder.row(InlineKeyboardButton(text="📨 Отправить приглашение Агенту", url=share_url))

        builder.row(
            InlineKeyboardButton(
                text="🔄 Проверить подключение",
                callback_data=HintActionCallback(code=code, action="refresh", symbol=0).pack(),
            )
        )
        builder.row(
            InlineKeyboardButton(
                text="❌ Отменить миссию",
                callback_data=HintActionCallback(code=code, action="cancel", symbol=0).pack(),
            )
        )
        return builder.as_markup()

    # Active game dossier buttons
    builder.row(
        InlineKeyboardButton(
            text="💡 Раскрыть символ",
            callback_data=HintActionCallback(code=code, action="reveal", symbol=0).pack(),
        ),
        InlineKeyboardButton(
            text="📊 Частоты букв",
            callback_data=HintActionCallback(code=code, action="freq", symbol=0).pack(),
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text="🔄 Обновить табло",
            callback_data=HintActionCallback(code=code, action="refresh", symbol=0).pack(),
        )
    )
    return builder.as_markup()


def build_agent_symbol_keyboard(
    session_or_code: Any,
    char_to_code: Optional[Any] = None,
    revealed_letters: Optional[Set[str]] = None,
) -> InlineKeyboardMarkup:
    """Build Field Terminal Phase 1 keyboard: 5-column grid of unrevealed symbols + SOS."""
    if hasattr(session_or_code, "invite_code"):
        code = session_or_code.invite_code
        c_map = session_or_code.cipher_mapping
        rev = session_or_code.revealed_letters
        unrevealed = get_unrevealed_codes(c_map, rev)
    elif isinstance(char_to_code, dict):
        code = str(session_or_code)
        rev = revealed_letters or set()
        unrevealed = get_unrevealed_codes(char_to_code, rev)
    elif isinstance(char_to_code, (list, set, tuple)):
        code = str(session_or_code)
        unrevealed = sorted(list(char_to_code))
    else:
        code = str(session_or_code)
        unrevealed = []

    builder = InlineKeyboardBuilder()
    for symbol_id in sorted(unrevealed):
        builder.button(
            text=f"[{symbol_id}]",
            callback_data=SymbolSelectCallback(code=code, symbol=symbol_id),
        )
    builder.adjust(5)

    is_solo = getattr(session_or_code, "is_solo", False)
    if is_solo:
        builder.row(
            InlineKeyboardButton(
                text="💡 Подсказка Штаба (открыть букву)",
                callback_data=HintActionCallback(code=code, action="solo_hint", symbol=0).pack(),
            ),
        )
        builder.row(
            InlineKeyboardButton(
                text="📊 Частотный анализ",
                callback_data=HintActionCallback(code=code, action="solo_freq", symbol=0).pack(),
            ),
            InlineKeyboardButton(
                text="🏳️ Сдаться",
                callback_data=HintActionCallback(code=code, action="solo_surrender", symbol=0).pack(),
            ),
        )
    else:
        builder.row(
            InlineKeyboardButton(
                text="📡 Запросить помощь Штаба",
                callback_data=HintActionCallback(code=code, action="sos", symbol=0).pack(),
            )
        )
    builder.row(
        InlineKeyboardButton(
            text="🔄 Обновить табло",
            callback_data=HintActionCallback(code=code, action="refresh", symbol=0).pack(),
        )
    )
    return builder.as_markup()


def build_agent_alphabet_keyboard(invite_code: str, symbol_code: int) -> InlineKeyboardMarkup:
    """Build Field Terminal Phase 2 letter substitution keyboard: 32 Cyrillic letters in 4 rows of 8."""
    builder = InlineKeyboardBuilder()
    for letter in RUSSIAN_ALPHABET:
        builder.button(
            text=letter,
            callback_data=LetterGuessCallback(
                code=invite_code, symbol=symbol_code, letter=letter
            ),
        )
    # 4 rows of 8 letters = 32 letters
    builder.adjust(8, 8, 8, 8)

    builder.row(
        InlineKeyboardButton(
            text="⬅️ Назад к символам",
            callback_data=SymbolSelectCallback(code=invite_code, symbol=0).pack(),
        )
    )
    return builder.as_markup()


def build_victory_keyboard() -> InlineKeyboardMarkup:
    """Build end-of-mission victory celebration keyboard."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text="🚀 Сыграть новую игру",
        callback_data=MenuCallback(action="catalog"),
    )
    builder.button(
        text="🏠 Главное меню",
        callback_data=MenuCallback(action="main"),
    )
    builder.adjust(1)
    return builder.as_markup()


def build_cancel_keyboard() -> InlineKeyboardMarkup:
    """Build simple cancellation keyboard."""
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data=MenuCallback(action="cancel"))
    return builder.as_markup()


def build_profile_keyboard() -> InlineKeyboardMarkup:
    """Build navigation keyboard for player military dossier profile."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text="🔄 Обновить данные",
        callback_data=MenuCallback(action="profile"),
    )
    builder.button(
        text="◀️ Главное меню",
        callback_data=MenuCallback(action="main"),
    )
    builder.adjust(1)
    return builder.as_markup()



def build_campaign_menu_keyboard(
    completed_chapters: Union[Set[int], Iterable[int], int, None] = None,
) -> InlineKeyboardMarkup:
    """Build interactive chapter selection keyboard with sequential status badges.

    Status badges:
    - ✅: Chapter is completed (replayable at any time)
    - 🔓: Chapter is currently unlocked and ready for decryption
    - 🔒: Chapter is classified/locked until previous chapter is solved
    """
    if isinstance(completed_chapters, int):
        completed_set = set(range(1, completed_chapters + 1))
    elif isinstance(completed_chapters, (set, list, tuple)):
        completed_set = set(completed_chapters)
    else:
        completed_set = set()

    from cryptogram.campaign_data import CAMPAIGN_CHAPTERS

    builder = InlineKeyboardBuilder()
    for ch_id in range(1, 6):
        ch = CAMPAIGN_CHAPTERS.get(ch_id)
        title = ch.title if ch else f"Глава {ch_id}"
        if ch_id in completed_set:
            text = f"✅ Глава {ch_id}: {title}"
            cb = CampaignCallback(action="chapter", chapter=ch_id)
        elif ch_id == 1 or (ch_id - 1) in completed_set:
            text = f"🔓 Глава {ch_id}: {title}"
            cb = CampaignCallback(action="chapter", chapter=ch_id)
        else:
            text = f"🔒 Глава {ch_id}: [Засекречено]"
            cb = CampaignCallback(action="locked", chapter=ch_id)
        builder.button(text=text, callback_data=cb)

    builder.button(
        text="◀️ Главное меню",
        callback_data=MenuCallback(action="main"),
    )
    builder.adjust(1)
    return builder.as_markup()


def build_campaign_briefing_keyboard(
    chapter_id: int = 1,
    chapter: Optional[int] = None,
) -> InlineKeyboardMarkup:
    """Build historical pre-mission briefing keyboard."""
    ch = chapter if chapter is not None else chapter_id
    builder = InlineKeyboardBuilder()
    builder.button(
        text="🎯 Приступить к дешифровке",
        callback_data=CampaignCallback(action="start", chapter=ch),
    )
    builder.button(
        text="◀️ К выбору глав",
        callback_data=CampaignCallback(action="menu", chapter=0),
    )
    builder.button(
        text="🏠 Главное меню",
        callback_data=MenuCallback(action="main"),
    )
    builder.adjust(1, 2)
    return builder.as_markup()


def build_campaign_victory_keyboard(
    current_chapter_id: int = 1,
    next_unlocked: bool = True,
    chapter: Optional[int] = None,
) -> InlineKeyboardMarkup:
    """Build victory debriefing keyboard with next chapter transition."""
    ch = chapter if chapter is not None else current_chapter_id
    builder = InlineKeyboardBuilder()
    if ch < 5 and next_unlocked:
        builder.button(
            text="Перейти к следующей главе ➡️",
            callback_data=CampaignCallback(action="chapter", chapter=ch + 1),
        )
    builder.button(
        text="📜 Выбор глав",
        callback_data=CampaignCallback(action="menu", chapter=0),
    )
    builder.button(
        text="🏠 Главное меню",
        callback_data=MenuCallback(action="main"),
    )
    builder.adjust(1)
    return builder.as_markup()


# Aliases for cross-report compatibility
get_main_menu_keyboard = build_main_menu_keyboard
get_catalog_keyboard = build_catalog_keyboard
get_invite_share_keyboard = build_invite_keyboard
get_analyst_dossier_keyboard = build_analyst_dossier_keyboard
get_agent_terminal_keyboard = build_agent_symbol_keyboard
build_alphabet_keyboard = build_agent_alphabet_keyboard
get_alphabet_picker_keyboard = build_agent_alphabet_keyboard
build_play_again_keyboard = build_victory_keyboard
get_victory_keyboard = build_victory_keyboard
get_profile_keyboard = build_profile_keyboard
build_campaign_chapters_keyboard = build_campaign_menu_keyboard
get_campaign_menu_keyboard = build_campaign_menu_keyboard
build_chapter_briefing_keyboard = build_campaign_briefing_keyboard
get_campaign_briefing_keyboard = build_campaign_briefing_keyboard
get_campaign_victory_keyboard = build_campaign_victory_keyboard


