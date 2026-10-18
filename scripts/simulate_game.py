#!/usr/bin/env python3
"""Standalone offline headless simulation runner for Cooperative Cryptogram Bot.

Supports automated end-to-end playthroughs and interactive CLI play
without real Telegram bot tokens or network connectivity.
"""
import argparse
import asyncio
import os
import sys
import textwrap
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# 1. Force UTF-8 on Windows consoles to eliminate charmap UnicodeEncodeError
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from aiogram import Bot, types
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy.pool import StaticPool

from bot.app import create_dispatcher
from bot.callbacks import HintActionCallback, LetterGuessCallback
from cryptogram.catalog import get_catalog, CATALOG_QUOTES
from cryptogram.engine import (
    calculate_frequencies,
    check_victory,
    get_unrevealed_codes,
    validate_text,
    render_mask,
    generate_cipher,
    RUSSIAN_CORPUS_FREQUENCIES,
)
from database.models import SessionStatus, HintType
from database.session import close_db, create_sqlite_async_engine, create_session_factory, init_db
from services.lock_manager import SessionLockManager
from services.session_service import SessionService
from tests.mock_session import MockPlayerClient, MockTelegramSession


# ==============================================================================
# UI Themes & Visual Styling
# ==============================================================================


@dataclass(frozen=True)
class BoxTheme:
    tl: str
    tr: str
    bl: str
    br: str
    h: str
    v: str
    sep_l: str
    sep_r: str
    sep_h: str


ASCII_THEME = BoxTheme(
    tl="+", tr="+", bl="+", br="+",
    h="-", v="|", sep_l="+", sep_r="+", sep_h="-"
)
UNICODE_THEME = BoxTheme(
    tl="┌", tr="┐", bl="└", br="┘",
    h="─", v="│", sep_l="├", sep_r="┤", sep_h="─"
)
DOUBLE_THEME = BoxTheme(
    tl="╔", tr="╗", bl="╚", br="╝",
    h="═", v="║", sep_l="╠", sep_r="╣", sep_h="═"
)


class Colorizer:
    """Provides ANSI color styling with automatic non-TTY and NO_COLOR fallback."""

    def __init__(self, enabled: bool = True):
        self.enabled = (
            enabled
            and sys.stdout.isatty()
            and "NO_COLOR" not in os.environ
            and os.environ.get("TERM") != "dumb"
        )

    def green(self, text: str) -> str:
        return f"\033[92m{text}\033[0m" if self.enabled else text

    def red(self, text: str) -> str:
        return f"\033[91m{text}\033[0m" if self.enabled else text

    def yellow(self, text: str) -> str:
        return f"\033[93m{text}\033[0m" if self.enabled else text

    def cyan(self, text: str) -> str:
        return f"\033[96m{text}\033[0m" if self.enabled else text

    def bold(self, text: str) -> str:
        return f"\033[1m{text}\033[0m" if self.enabled else text

    def gray(self, text: str) -> str:
        return f"\033[90m{text}\033[0m" if self.enabled else text


# ==============================================================================
# Box Rendering Functions
# ==============================================================================


def render_analyst_dossier_terminal(
    invite_code: str,
    status_text: str,
    original_text: str,
    title: str = "Спецоперация",
    source: str = "",
    difficulty: str = "NORMAL",
    category: str = "military",
    total_letters: int = 0,
    unique_letters: int = 0,
    revealed_count: int = 0,
    mistakes_count: int = 0,
    hints_used: int = 0,
    theme: BoxTheme = ASCII_THEME,
) -> str:
    w = 72
    inner_w = w - 4
    pct = round((revealed_count / unique_letters) * 100, 1) if unique_letters else 0.0

    lines = []
    lines.append(f"{theme.tl}{theme.h * (w - 2)}{theme.tr}")
    lines.append(f"{theme.v} [HQ] ШТАБНОЙ АНАЛИТИК // ОПЕРАТИВНОЕ ДОСЬЕ{' ' * (inner_w - 41)} {theme.v}")
    lines.append(f"{theme.sep_l}{theme.sep_h * (w - 2)}{theme.sep_r}")

    lines.append(f"{theme.v} Операция: {invite_code:<18} | Статус: {status_text:<31} {theme.v}")
    lines.append(f"{theme.sep_l}{theme.sep_h * (w - 2)}{theme.sep_r}")

    lines.append(f"{theme.v} ИСТОЧНИК ДЕПЕШИ:{' ' * (inner_w - 17)} {theme.v}")
    title_line = f"«{title}» (Сложность: {difficulty} | Категория: {category})"
    lines.append(f"{theme.v} {title_line[:inner_w]:<{inner_w}} {theme.v}")
    if source:
        src_line = f"Источник: {source}"
        for sl in textwrap.wrap(src_line, width=inner_w):
            lines.append(f"{theme.v} {sl:<{inner_w}} {theme.v}")
    lines.append(f"{theme.sep_l}{theme.sep_h * (w - 2)}{theme.sep_r}")

    lines.append(f"{theme.v} ОТКРЫТЫЙ ТЕКСТ (СЕКРЕТНО ДЛЯ ШТАБА):{' ' * (inner_w - 35)} {theme.v}")
    for tl in textwrap.wrap(f'"{original_text}"', width=inner_w):
        lines.append(f"{theme.v} {tl:<{inner_w}} {theme.v}")
    lines.append(f"{theme.sep_l}{theme.sep_h * (w - 2)}{theme.sep_r}")

    lines.append(f"{theme.v} СТАТИСТИКА ОПЕРАЦИИ:{' ' * (inner_w - 20)} {theme.v}")
    lines.append(f"{theme.v} - Всего букв: {total_letters:<16} - Уникальных символов: {unique_letters:<10} {theme.v}")
    lines.append(f"{theme.v} - Раскрыто: {revealed_count} ({pct}%) {' ' * max(0, 14 - len(str(revealed_count)) - len(str(pct)))} - Осталось закрыто:   {unique_letters - revealed_count:<10} {theme.v}")
    lines.append(f"{theme.v} - Ошибок Агента: {mistakes_count:<13} - Передано подсказок:  {hints_used:<10} {theme.v}")
    lines.append(f"{theme.bl}{theme.h * (w - 2)}{theme.br}")

    return "\n".join(lines)


def render_agent_terminal_screen(
    invite_code: str,
    masked_text: str,
    frequencies: List[Tuple[int, str, int, float, float]],
    unrevealed_codes: List[int],
    total_unique: int,
    revealed_count: int,
    mistakes_count: int,
    hints_used: int,
    last_alert: str = "",
    theme: BoxTheme = ASCII_THEME,
) -> str:
    w = 72
    inner_w = w - 4
    pct = round((revealed_count / total_unique) * 100, 1) if total_unique else 0.0

    lines = []
    lines.append(f"{theme.tl}{theme.h * (w - 2)}{theme.tr}")
    hdr = f"[AGENT] ПОЛЕВОЙ ТЕРМИНАЛ ДЕШИФРОВКИ // МИССИЯ: {invite_code}"
    lines.append(f"{theme.v} {hdr:<{inner_w}} {theme.v}")
    lines.append(f"{theme.sep_l}{theme.sep_h * (w - 2)}{theme.sep_r}")

    stat1 = f"Прогресс: {revealed_count}/{total_unique} ({pct}%)"
    stat2 = f"Ошибок: {mistakes_count} | Подсказок: {hints_used}"
    lines.append(f"{theme.v} {stat1:<32} | {stat2:<33} {theme.v}")
    lines.append(f"{theme.sep_l}{theme.sep_h * (w - 2)}{theme.sep_r}")

    lines.append(f"{theme.v} ШИФРОГРАММА (ТЕКУЩЕЕ СОСТОЯНИЕ):{' ' * (inner_w - 32)} {theme.v}")
    lines.append(f"{theme.v}{' ' * (inner_w + 2)}{theme.v}")
    for ml in textwrap.wrap(masked_text, width=inner_w - 4):
        lines.append(f"{theme.v}   {ml:<{inner_w - 2}} {theme.v}")
    lines.append(f"{theme.v}{' ' * (inner_w + 2)}{theme.v}")
    lines.append(f"{theme.sep_l}{theme.sep_h * (w - 2)}{theme.sep_r}")

    lines.append(f"{theme.v} ЧАСТОТНЫЙ АНАЛИЗ (ТОП БУКВ ДЕПЕШИ):{' ' * (inner_w - 35)} {theme.v}")
    top_freqs = frequencies[:10]
    half = (len(top_freqs) + 1) // 2
    for i in range(half):
        c1, ch1, cnt1, tp1, bp1 = top_freqs[i]
        col1 = f"[{c1:2d}: {ch1}] {cnt1:2d} ({tp1:4.1f}%) корп:{bp1:5.2f}%"
        if i + half < len(top_freqs):
            c2, ch2, cnt2, tp2, bp2 = top_freqs[i + half]
            col2 = f"[{c2:2d}: {ch2}] {cnt2:2d} ({tp2:4.1f}%) корп:{bp2:5.2f}%"
        else:
            col2 = ""
        lines.append(f"{theme.v} {col1:<32} | {col2:<33} {theme.v}")
    lines.append(f"{theme.sep_l}{theme.sep_h * (w - 2)}{theme.sep_r}")

    cnt_unrev = len(unrevealed_codes)
    lines.append(f"{theme.v} НЕРАСКРЫТЫЕ СИМВОЛЫ ({cnt_unrev} шт.):{' ' * max(0, inner_w - 29 - len(str(cnt_unrev)))} {theme.v}")
    symbols_str = " ".join(f"[{code}]" for code in unrevealed_codes)
    for sl in textwrap.wrap(symbols_str, width=inner_w - 2):
        lines.append(f"{theme.v}  {sl:<{inner_w - 1}} {theme.v}")
    lines.append(f"{theme.sep_l}{theme.sep_h * (w - 2)}{theme.sep_r}")

    alert_text = last_alert or "Ожидание выбора действия..."
    lines.append(f"{theme.v} СВОДКА: {alert_text[:inner_w - 9]:<{inner_w - 9}} {theme.v}")
    lines.append(f"{theme.bl}{theme.h * (w - 2)}{theme.br}")

    return "\n".join(lines)


def render_victory_banner(
    original_text: str,
    total_unique: int,
    mistakes_count: int,
    hints_used: int,
    elapsed_seconds: float,
    theme: BoxTheme = DOUBLE_THEME,
) -> str:
    w = 72
    inner_w = w - 4

    mins = int(elapsed_seconds // 60)
    secs = int(elapsed_seconds % 60)
    time_str = f"{mins:02d}:{secs:02d} ({int(elapsed_seconds)} сек.)"

    total_attempts = total_unique + mistakes_count
    accuracy = round((total_unique / total_attempts) * 100, 1) if total_attempts else 100.0

    if mistakes_count == 0 and hints_used == 0:
        rating = "ЭЛИТНЫЙ КРИПТОАНАЛИТИК (S-КЛАСС)"
    elif mistakes_count <= 2 and hints_used <= 1:
        rating = "МАСТЕР-ДЕШИФРОВЩИК (A-КЛАСС)"
    elif mistakes_count <= 5 and hints_used <= 3:
        rating = "ОПЫТНЫЙ ПОЛЕВОЙ АГЕНТ (B-КЛАСС)"
    else:
        rating = "СПЕЦИАЛИСТ ПО РАДИОПЕРЕХВАТУ (C-КЛАСС)"

    lines = []
    lines.append(f"{theme.tl}{theme.h * (w - 2)}{theme.tr}")
    lines.append(f"{theme.v}{' ' * ((inner_w - 27) // 2)}*** МИССИЯ ВЫПОЛНЕНА! ***{' ' * ((inner_w - 27 + 1) // 2)}{theme.v}")
    lines.append(f"{theme.v}{' ' * ((inner_w - 30) // 2)}ШИФРОГРАММА УСПЕШНО РАСКРЫТА{' ' * ((inner_w - 30 + 1) // 2)}{theme.v}")
    lines.append(f"{theme.sep_l}{theme.sep_h * (w - 2)}{theme.sep_r}")

    lines.append(f"{theme.v} РАСШИФРОВАННЫЙ ТЕКСТ ДЕПЕШИ:{' ' * (inner_w - 28)} {theme.v}")
    for tl in textwrap.wrap(f'"{original_text}"', width=inner_w - 2):
        lines.append(f"{theme.v}  {tl:<{inner_w - 1}} {theme.v}")
    lines.append(f"{theme.sep_l}{theme.sep_h * (w - 2)}{theme.sep_r}")

    lines.append(f"{theme.v} ИТОГОВАЯ СТАТИСТИКА СПЕЦОПЕРАЦИИ:{' ' * (inner_w - 34)} {theme.v}")
    lines.append(f"{theme.v} - Затраченное время:        {time_str:<39} {theme.v}")
    lines.append(f"{theme.v} - Всего уникальных букв:    {total_unique:<39} {theme.v}")
    lines.append(f"{theme.v} - Допущено ошибок:          {mistakes_count:<39} {theme.v}")
    lines.append(f"{theme.v} - Запрошено подсказок:      {hints_used:<39} {theme.v}")
    lines.append(f"{theme.v} - Точность дешифровки:      {accuracy}%{' ' * max(0, 38 - len(str(accuracy)))} {theme.v}")
    lines.append(f"{theme.v} - Оценка эффективности:     {rating:<39} {theme.v}")
    lines.append(f"{theme.bl}{theme.h * (w - 2)}{theme.br}")

    return "\n".join(lines)


# ==============================================================================
# Input & Homoglyph Helpers
# ==============================================================================


RUSSIAN_ALPHABET_SET = frozenset("АБВГДЕЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ")

LATIN_HOMOGLYPHS: Dict[str, str] = {
    "A": "А", "B": "В", "E": "Е", "K": "К", "M": "М", "H": "Н",
    "O": "О", "P": "Р", "C": "С", "T": "Т", "X": "Х", "Y": "У"
}


def safe_input(prompt: str = "") -> str:
    """Protected input prompt handling KeyboardInterrupt and EOFError gracefully."""
    try:
        return input(prompt)
    except KeyboardInterrupt:
        print("\n\n[!] Операция прервана пользователем (Ctrl+C). До связи!")
        sys.exit(0)
    except EOFError:
        print("\n[!] Обнаружен конец потока ввода (EOF). Завершение сессии.")
        sys.exit(0)


def prompt_menu_choice(prompt_text: str, valid_choices: Set[str], default: Optional[str] = None) -> str:
    while True:
        raw = safe_input(prompt_text)
        choice = raw.strip().upper()
        if not choice and default is not None:
            return default
        if choice in valid_choices:
            return choice
        allowed = ", ".join(sorted(valid_choices))
        print(f"[!] Неверный выбор '{choice}'. Пожалуйста, выберите один из вариантов: [{allowed}]")


def prompt_symbol_choice(unrevealed_codes: List[int]) -> Optional[int]:
    while True:
        raw = safe_input("Введите номер символа [или 'C' для отмены]: ")
        clean = raw.strip().upper()
        if not clean or clean == "C":
            return None
        if not clean.isdigit():
            print("[!] Ошибка: номер символа должен быть целым положительным числом.")
            continue
        code = int(clean)
        if code not in unrevealed_codes:
            print(f"[!] Ошибка: символ [{code}] не найден среди нераскрытых символов шифрограммы.")
            continue
        return code


def prompt_letter_choice(symbol_code: int) -> Optional[str]:
    while True:
        raw = safe_input(f"Введите русскую букву для символа [{symbol_code}] [или 'C' для отмены]: ")
        clean = raw.strip().upper()
        if not clean or clean == "C":
            return None
        char = clean[0].replace("Ё", "Е")

        if char in LATIN_HOMOGLYPHS:
            cyr = LATIN_HOMOGLYPHS[char]
            print(f"[i] Латинская буква '{char}' автоматически заменена на русскую '{cyr}'.")
            char = cyr

        if char not in RUSSIAN_ALPHABET_SET:
            print(f"[!] Ошибка: '{char}' не является буквой русского алфавита (А-Я).")
            continue
        return char


# ==============================================================================
# Simulation Configuration & Harness
# ==============================================================================


@dataclass
class SimulationConfig:
    mode: str
    quote_idx: Optional[int]
    custom_text: Optional[str]
    seed: Optional[int]
    db_path: str
    no_color: bool


class GameHarness:
    """Manages async SQLite engine, mock Telegram session, Dispatcher, and simulated players."""

    def __init__(self, db_path: str = ":memory:"):
        self.db_path = db_path
        self.engine = None
        self.session_service = None
        self.lock_manager = None
        self.mock_session = None
        self.bot = None
        self.dp = None
        self.analyst = None
        self.agent = None

    async def setup(self) -> None:
        poolclass = StaticPool if self.db_path == ":memory:" else None
        self.engine = create_sqlite_async_engine(
            db_path=self.db_path, poolclass=poolclass, echo=False
        )
        await init_db(self.engine)

        session_factory = create_session_factory(self.engine)
        self.lock_manager = SessionLockManager()
        self.session_service = SessionService(
            session_factory=session_factory, lock_manager=self.lock_manager
        )

        self.mock_session = MockTelegramSession()
        self.bot = Bot(token="42:MOCK_SIMULATION_TOKEN", session=self.mock_session)
        self.dp = create_dispatcher(
            session_service=self.session_service,
            lock_manager=self.lock_manager,
            bot_username="SpyAnalystBot",
            storage=MemoryStorage(),
        )

        self.analyst = MockPlayerClient(
            self.dp, self.bot, user_id=1001, username="analyst_hq", first_name="Штабной"
        )
        self.agent = MockPlayerClient(
            self.dp, self.bot, user_id=1002, username="field_agent", first_name="Агент 007"
        )

    async def teardown(self) -> None:
        if self.bot and self.bot.session:
            await self.bot.session.close()
        if self.engine:
            await close_db(self.engine)


# ==============================================================================
# Automated Simulator
# ==============================================================================


class AutoSimulator:
    """Executes a full automated game playthrough from creation to victory verification."""

    def __init__(self, harness: GameHarness, config: SimulationConfig, colorizer: Colorizer):
        self.harness = harness
        self.config = config
        self.c = colorizer

    async def run(self) -> int:
        catalog = get_catalog()
        start_time = datetime.now()

        # 1. Determine phrase
        if self.config.custom_text:
            phrase = self.config.custom_text
            is_custom = True
            catalog_id = None
        else:
            q_idx = self.config.quote_idx if self.config.quote_idx is not None else 0
            item = catalog[q_idx]
            phrase = item.text
            is_custom = False
            catalog_id = item.id

        print(self.c.bold("=" * 80))
        print(self.c.cyan("   🕵️‍♂️  ШПИОН И АНАЛИТИК — АВТОМАТИЧЕСКАЯ СИМУЛЯЦИЯ ИГРЫ"))
        print(self.c.bold("=" * 80))

        # 2. Analyst creates game
        print(f"\n{self.c.bold('[1/6] Создание игровой сессии Штабным Аналитиком...')}")
        service = self.harness.session_service
        game = await service.create_session(
            creator_tg_id=self.harness.analyst.user.id,
            text=phrase,
            is_custom=is_custom,
            catalog_id=catalog_id,
            seed=self.config.seed,
            username=self.harness.analyst.user.username,
            first_name=self.harness.analyst.user.first_name,
        )
        code = game.invite_code
        print(f"      • Код приглашения: {self.c.green('game_' + code)}")
        print(f"      • Исходный текст: \"{phrase}\"")
        print(f"      • Уникальных букв: {game.total_unique_letters}")

        # Seed Analyst dossier message in mock session
        analyst_msg = types.Message(
            message_id=500,
            date=datetime.now(timezone.utc),
            chat=self.harness.analyst.chat,
            from_user=self.harness.analyst.user,
            text=f"ДОСЬЕ СПЕЦОПЕРАЦИИ (game_{code})",
        )
        self.harness.mock_session.messages[500] = analyst_msg
        await service.update_message_ids(
            game.id, analyst_chat_id=self.harness.analyst.chat.id, analyst_message_id=500
        )

        # 3. Agent joins via deep-link
        print(f"\n{self.c.bold('[2/6] Полевой Агент подключается по deep-link...')}")
        await self.harness.agent.send_command("/start", f"game_{code}")
        game = await service.get_session_by_code(code)
        assert game.status == SessionStatus.IN_PROGRESS, "Game must be IN_PROGRESS after agent joins"
        agent_msg = self.harness.mock_session.get_last_message_for_chat(self.harness.agent.user.id)
        assert agent_msg is not None, "Agent terminal message must be present"
        print(f"      • Статус сессии: {self.c.green(game.status.value)}")
        print(f"      • Агент: @{self.harness.agent.user.username} успешно получил Полевой Терминал")

        # 4. Analyst sends frequency report hint
        print(f"\n{self.c.bold('[3/6] Штабной Аналитик передает частотный анализ депеши...')}")
        freq_cb = HintActionCallback(code=code, action="freq", symbol=0).pack()
        await self.harness.analyst.click_button(freq_cb, message=self.harness.mock_session.messages[500])
        print(f"      • Радиограмма с частотами отправлена на терминал Агента")

        # 5. Agent guesses top high-frequency letters
        print(f"\n{self.c.bold('[4/6] Полевой Агент анализирует частоты и разгадывает ключевые символы...')}")
        freqs = calculate_frequencies(phrase)
        code_to_char = game.code_to_char_map
        char_to_code = game.cipher_mapping

        # Guess top 2 most frequent letters
        for item in freqs[:2]:
            let = item.letter
            sym = char_to_code[let]
            guess_cb = LetterGuessCallback(code=code, symbol=sym, letter=let).pack()
            await self.harness.agent.click_button(guess_cb, message=agent_msg)
            print(f"      • Догадка: символ [{sym}] = '{let}' -> {self.c.green('ВЕРНО')}")

        # 6. Analyst provides letter reveal hint if stuck
        print(f"\n{self.c.bold('[5/6] Штаб передает подсказку (раскрытие символа)...')}")
        hint_cb = HintActionCallback(code=code, action="reveal", symbol=0).pack()
        await self.harness.analyst.click_button(hint_cb, message=self.harness.mock_session.messages[500])
        game = await service.get_session_by_code(code)
        print(f"      • Использовано подсказок: {game.hints_used}")

        # 7. Agent solves remaining letters to completion
        print(f"\n{self.c.bold('[6/6] Полевой Агент завершает дешифровку остальных символов...')}")
        all_codes = sorted(code_to_char.keys())
        for sym in all_codes:
            game = await service.get_session_by_code(code)
            if str(sym) in game.guessed_mapping:
                continue
            let = code_to_char[sym]
            guess_cb = LetterGuessCallback(code=code, symbol=sym, letter=let).pack()
            await self.harness.agent.click_button(guess_cb, message=agent_msg)
            print(f"      • Догадка: символ [{sym}] = '{let}' -> {self.c.green('ВЕРНО')}")

        # 8. Assert victory
        final_game = await service.get_session_by_code(code)
        assert final_game.status == SessionStatus.SOLVED, "Game status must be SOLVED"
        elapsed = (datetime.now() - start_time).total_seconds()

        print(self.c.bold("\n" + "=" * 80))
        print(self.c.green("🎉 ПОБЕДА! ШИФРОГРАММА УСПЕШНО ДЕШИФРОВАНА!"))
        print(self.c.bold("=" * 80))
        print(f"Текст депеши: \"{phrase}\"")
        print(f"Статистика: Статус={final_game.status.value} | Ошибок={final_game.mistakes_count} | Подсказок={final_game.hints_used} | Время={elapsed:.2f}с")
        print(self.c.green("Симуляция завершена успешно. Код выхода: 0\n"))
        return 0


# ==============================================================================
# Interactive CLI Simulator
# ==============================================================================


class InteractiveSimulator:
    """Interactive CLI terminal game supporting human play."""

    def __init__(self, harness: GameHarness, config: SimulationConfig, colorizer: Colorizer):
        self.harness = harness
        self.config = config
        self.c = colorizer

    async def run(self) -> int:
        catalog = get_catalog()
        service = self.harness.session_service
        start_time = datetime.now()

        # Step 1: Select or create phrase
        catalog_item = None
        if self.config.custom_text:
            phrase = self.config.custom_text
            is_custom = True
            catalog_id = None
        elif self.config.quote_idx is not None:
            catalog_item = catalog[self.config.quote_idx]
            phrase = catalog_item.text
            is_custom = False
            catalog_id = catalog_item.id
        else:
            print(self.c.bold("\n=== ВЫБОР ДЕПЕШИ ДЛЯ СПЕЦОПЕРАЦИИ ==="))
            print("[1] Выбрать из каталога исторических депеш")
            print("[2] Ввести собственный секретный текст")
            print("[3] Быстрый старт (первая депеша каталога)")
            choice = prompt_menu_choice("Ваш выбор [1-3, по умолч. 3]: ", {"1", "2", "3"}, default="3")

            if choice == "1":
                print("\nКАТАЛОГ ДЕПЕШ:")
                for idx, it in enumerate(catalog):
                    print(f"[{idx:2d}] «{it.title}» ({it.difficulty}, {it.category})")
                q_idx_str = safe_input("Введите номер депеши [0-19, по умолч. 0]: ").strip()
                q_idx = int(q_idx_str) if q_idx_str.isdigit() and 0 <= int(q_idx_str) < len(catalog) else 0
                catalog_item = catalog[q_idx]
                phrase = catalog_item.text
                is_custom = False
                catalog_id = catalog_item.id
            elif choice == "2":
                while True:
                    custom_in = safe_input("Введите текст на русском языке: ").strip()
                    val = validate_text(custom_in)
                    if val.is_valid:
                        phrase = custom_in
                        is_custom = True
                        catalog_id = None
                        break
                    print(f"[!] Ошибка: {val.error_message}")
            else:
                catalog_item = catalog[0]
                phrase = catalog_item.text
                is_custom = False
                catalog_id = catalog_item.id

        # Step 2: Create session
        game = await service.create_session(
            creator_tg_id=self.harness.analyst.user.id,
            text=phrase,
            is_custom=is_custom,
            catalog_id=catalog_id,
            seed=self.config.seed,
            username=self.harness.analyst.user.username,
            first_name=self.harness.analyst.user.first_name,
        )
        code = game.invite_code

        # Seed Analyst message
        analyst_msg = types.Message(
            message_id=500,
            date=datetime.now(timezone.utc),
            chat=self.harness.analyst.chat,
            from_user=self.harness.analyst.user,
            text=f"ДОСЬЕ СПЕЦОПЕРАЦИИ (game_{code})",
        )
        self.harness.mock_session.messages[500] = analyst_msg
        await service.update_message_ids(
            game.id, analyst_chat_id=self.harness.analyst.chat.id, analyst_message_id=500
        )

        # Agent joins
        await self.harness.agent.send_command("/start", f"game_{code}")
        agent_msg = self.harness.mock_session.get_last_message_for_chat(self.harness.agent.user.id)
        assert agent_msg is not None

        # Build frequencies table: (code, char, count, text_pct, corpus_pct)
        freq_items = calculate_frequencies(phrase)
        code_map = game.cipher_mapping
        freq_tuples = []
        for fi in freq_items:
            c_code = code_map[fi.letter]
            corp_pct = RUSSIAN_CORPUS_FREQUENCIES.get(fi.letter, 0.0)
            freq_tuples.append((c_code, fi.letter, fi.count, fi.percentage, corp_pct))

        last_alert = "Операция начата. Полевой терминал на связи со Штабом."

        # Game loop
        while True:
            game = await service.get_session_by_code(code)
            if game.status == SessionStatus.SOLVED:
                break

            revealed_set = set(game.guessed_mapping.values())
            unrevealed_codes = get_unrevealed_codes(game.cipher_mapping, revealed_set)
            masked_str = render_mask(phrase, game.cipher_mapping, revealed_set, wrap_pre=False)

            # Render terminal screen
            terminal_screen = render_agent_terminal_screen(
                invite_code=f"game_{code}",
                masked_text=masked_str,
                frequencies=freq_tuples,
                unrevealed_codes=unrevealed_codes,
                total_unique=game.total_unique_letters,
                revealed_count=len(revealed_set),
                mistakes_count=game.mistakes_count,
                hints_used=game.hints_used,
                last_alert=last_alert,
                theme=UNICODE_THEME if not self.config.no_color else ASCII_THEME,
            )
            print("\n" + terminal_screen)

            # Action Menu
            print("\nДЕЙСТВИЯ АГЕНТА:")
            print("  [1] Подставить букву (выбор символа и буквы)")
            print("  [2] Запросить подсказку у Штаба (раскрыть случайный символ)")
            print("  [3] Запросить частотную радиограмму у Штаба")
            print("  [4] Передать сигнал тревоги SOS")
            print("  [5] Просмотреть Досье Штаба (HQ)")
            print("  [Q] Завершить сеанс")

            act = prompt_menu_choice("Выберите действие [1-5, Q]: ", {"1", "2", "3", "4", "5", "Q"})

            if act == "Q":
                print("\n[!] Сеанс дешифровки прерван пользователем. До связи!")
                return 0

            elif act == "1":
                sym = prompt_symbol_choice(unrevealed_codes)
                if sym is None:
                    last_alert = "Выбор символа отменен."
                    continue
                let = prompt_letter_choice(sym)
                if let is None:
                    last_alert = "Ввод буквы отменен."
                    continue

                guess_res = await service.submit_guess(code, self.harness.agent.user.id, sym, let)
                if guess_res.is_correct:
                    last_alert = f"Символ [{sym}] = '{let}' подтвержден Штабом!"
                else:
                    last_alert = f"ОШИБКА: символ [{sym}] != '{let}'! Ошибка зафиксирована."

            elif act == "2":
                hint_res = await service.give_hint(code, self.harness.analyst.user.id, HintType.REVEAL_LETTER)
                if hint_res.revealed_letter:
                    last_alert = f"Штаб раскрыл символ [{hint_res.revealed_code}] = '{hint_res.revealed_letter}'!"
                else:
                    last_alert = "Штаб: все символы уже раскрыты."

            elif act == "3":
                await service.give_hint(code, self.harness.analyst.user.id, HintType.FREQUENCY_REPORT)
                last_alert = "Штаб передал свежую радиограмму частотного распределения."

            elif act == "4":
                await service.request_help(code, self.harness.agent.user.id)
                last_alert = "Сигнал SOS передан в Штаб! Аналитик уведомлен."

            elif act == "5":
                title_val = catalog_item.title if catalog_item else "Собственная депеша"
                source_val = catalog_item.source if catalog_item else ""
                diff_val = catalog_item.difficulty if catalog_item else "NORMAL"
                cat_val = catalog_item.category if catalog_item else "custom"
                dossier = render_analyst_dossier_terminal(
                    invite_code=f"game_{code}",
                    status_text=game.status.value,
                    original_text=phrase,
                    title=title_val,
                    source=source_val,
                    difficulty=diff_val,
                    category=cat_val,
                    total_letters=game.total_letters_count,
                    unique_letters=game.total_unique_letters,
                    revealed_count=len(revealed_set),
                    mistakes_count=game.mistakes_count,
                    hints_used=game.hints_used,
                    theme=UNICODE_THEME if not self.config.no_color else ASCII_THEME,
                )
                print("\n" + dossier)
                safe_input("\nНажмите Enter для возврата к терминалу дешифровки...")
                last_alert = "Возврат из оперативного досье Штаба."

        # Final Victory
        elapsed = (datetime.now() - start_time).total_seconds()
        final_game = await service.get_session_by_code(code)
        banner = render_victory_banner(
            original_text=phrase,
            total_unique=final_game.total_unique_letters,
            mistakes_count=final_game.mistakes_count,
            hints_used=final_game.hints_used,
            elapsed_seconds=elapsed,
            theme=DOUBLE_THEME if not self.config.no_color else ASCII_THEME,
        )
        print("\n" + banner)
        return 0


# ==============================================================================
# CLI Argument Parser & Entrypoint
# ==============================================================================


def parse_args(argv: Optional[list] = None) -> SimulationConfig:
    """Parse and validate command line arguments."""
    parser = argparse.ArgumentParser(
        description="Cooperative Cryptogram Headless CLI Simulation Runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--auto", action="store_true", help="Run automated simulation to victory"
    )
    mode_group.add_argument(
        "--interactive", action="store_true", help="Run interactive CLI terminal game"
    )

    text_group = parser.add_mutually_exclusive_group()
    text_group.add_argument(
        "--quote", type=int, default=None, help="Quote index from catalog (0-19)"
    )
    text_group.add_argument(
        "--custom", type=str, default=None, help="Custom Russian text for cryptogram"
    )

    parser.add_argument(
        "--seed", type=int, default=None, help="Deterministic random seed for cipher generation"
    )
    parser.add_argument(
        "--db", type=str, default=":memory:", help="SQLite database path (default: :memory:)"
    )
    parser.add_argument(
        "--no-color", action="store_true", help="Disable ANSI color codes in console output"
    )

    args = parser.parse_args(argv)

    mode = "interactive" if args.interactive else "auto"

    catalog = get_catalog()
    if args.quote is not None:
        if args.quote < 0 or args.quote >= len(catalog):
            parser.error(
                f"--quote index out of range: must be between 0 and {len(catalog) - 1} (got {args.quote})"
            )

    if args.custom is not None:
        val = validate_text(args.custom)
        if not val.is_valid:
            parser.error(f"Invalid custom text: {val.error_message}")

    return SimulationConfig(
        mode=mode,
        quote_idx=args.quote,
        custom_text=args.custom,
        seed=args.seed,
        db_path=args.db,
        no_color=args.no_color,
    )


async def run_simulation(config: SimulationConfig) -> int:
    colorizer = Colorizer(enabled=not config.no_color)
    harness = GameHarness(db_path=config.db_path)
    await harness.setup()
    try:
        if config.mode == "interactive":
            sim = InteractiveSimulator(harness, config, colorizer)
        else:
            sim = AutoSimulator(harness, config, colorizer)
        return await sim.run()
    finally:
        await harness.teardown()


def main(argv: Optional[list] = None) -> int:
    try:
        config = parse_args(argv)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 2

    try:
        return asyncio.run(run_simulation(config))
    except KeyboardInterrupt:
        print("\n\n[!] Сеанс прерван пользователем.")
        return 0
    except EOFError:
        print("\n[!] Конец потока ввода (EOF). Сеанс завершен.")
        return 0
    except Exception as exc:
        print(f"\n[!] Критическая ошибка симулятора: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
