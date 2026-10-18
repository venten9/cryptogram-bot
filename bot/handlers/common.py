"""Common handlers: /start, deep-link routing, /help, /cancel."""
import html
from typing import Optional, Union

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.filters.command import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.callbacks import MenuCallback
from bot.keyboards import (
    build_agent_symbol_keyboard,
    build_main_menu_keyboard,
    build_profile_keyboard,
    build_solo_difficulty_keyboard,
)
from bot.utils import NotifierService, safe_edit_callback
from database.models import User
from services.session_service import (
    CreatorCannotJoinAsAgent,
    SelfJoinError,
    SessionAlreadyFull,
    SessionAlreadyStartedError,
    SessionFinishedError,
    SessionNotFoundError,
    SessionService,
)

common_router = Router(name="common")


@common_router.message(CommandStart())
async def handle_start(
    message: Message,
    command: CommandObject,
    session_service: SessionService,
    state: FSMContext,
    bot: Bot,
    notifier: Optional[NotifierService] = None,
) -> None:
    """Handle /start command with or without deep-link join token."""
    await state.clear()
    if notifier is None:
        notifier = NotifierService(bot=bot, session_service=session_service)

    # Deep-link branch: /start game_<invite_code>
    if command.args and command.args.startswith("game_"):
        invite_code = command.args[5:].strip().lower()
        await handle_agent_join(
            message=message,
            invite_code=invite_code,
            session_service=session_service,
            notifier=notifier,
        )
        return

    # Standard branch: Main Menu
    first_name = html.escape(message.from_user.first_name if message.from_user else "Агент")
    welcome_text = (
        f"🕵️‍♂️ <b>Приветствую, {first_name}!</b>\n\n"
        "Добро пожаловать в тактическую криптографическую игру <b>«Шпион и Аналитик»</b>.\n\n"
        "Здесь два игрока действуют в тесной кооперации:\n"
        "• <b>Штабной Аналитик</b>: видит открытый текст депеши, рассчитывает частоты и передает подсказки.\n"
        "• <b>Полевой Агент</b>: получает зашифрованный текст и разгадывает символы через интерактивный терминал.\n\n"
        "Выберите действие для начала:"
    )
    await message.answer(
        text=welcome_text,
        reply_markup=build_main_menu_keyboard(),
        parse_mode="HTML",
    )


async def handle_agent_join(
    message: Message,
    invite_code: str,
    session_service: SessionService,
    notifier: NotifierService,
) -> None:
    """Process Field Agent connection to a waiting session via deep-link."""
    user = message.from_user
    if not user:
        return

    try:
        game = await session_service.join_session(
            invite_code=invite_code,
            agent_tg_id=user.id,
            username=user.username,
            first_name=user.first_name or "",
        )
    except SessionNotFoundError:
        await message.answer(
            "❌ <b>Операция не найдена.</b>\nПроверьте ссылку-приглашение или создайте новую игру.",
            reply_markup=build_main_menu_keyboard(),
            parse_mode="HTML",
        )
        return
    except (SessionAlreadyStartedError, SessionAlreadyFull):
        await message.answer(
            "⚠️ <b>К этой операции уже подключился другой Агент!</b>\nВы можете создать свою игру в главном меню.",
            reply_markup=build_main_menu_keyboard(),
            parse_mode="HTML",
        )
        return
    except (SelfJoinError, CreatorCannotJoinAsAgent):
        await message.answer(
            "⚠️ <b>Вы являетесь Штабным Аналитиком этой операции!</b>\n"
            "Создатель игры не может выступать в роли Агента. Передайте ссылку напарнику.",
            reply_markup=build_main_menu_keyboard(),
            parse_mode="HTML",
        )
        return
    except SessionFinishedError:
        await message.answer(
            "⚠️ <b>Эта операция уже завершена.</b>\nВы можете начать новую игру в главном меню.",
            reply_markup=build_main_menu_keyboard(),
            parse_mode="HTML",
        )
        return

    # Successful join -> Initialize Agent Terminal
    terminal_text = notifier.render_agent_terminal(game)
    kb = build_agent_symbol_keyboard(game)
    terminal_msg = await message.answer(
        text=terminal_text,
        reply_markup=kb,
        parse_mode="HTML",
    )

    # Store terminal coordinates for future in-place edit_message_text sync
    await session_service.update_message_ids(
        session_id_or_code=game.id,
        agent_chat_id=terminal_msg.chat.id,
        agent_message_id=terminal_msg.message_id,
    )

    # Convert User or construct placeholder for notifier
    agent_entity = game.agent or User(
        id=user.id,
        username=user.username,
        first_name=user.first_name or "",
    )
    # Notify Headquarters Analyst
    await notifier.notify_agent_joined(game, agent_entity)


@common_router.message(Command("help"))
@common_router.callback_query(F.data == "menu:help")
@common_router.callback_query(MenuCallback.filter(F.action == "help"))
async def handle_help(event: Union[Message, CallbackQuery]) -> None:
    """Display rules, mechanics, and operational instructions."""
    help_text = (
        "📖 <b>ПРАВИЛА ИГРЫ «ШПИОН И АНАЛИТИК»</b>\n\n"
        "<b>1. Цель игры:</b>\n"
        "Совместными усилиями полностью восстановить исходный текст секретной депеши.\n\n"
        "<b>2. Шифр простой подстановки:</b>\n"
        "Каждой уникальной русской букве сопоставлен числовой код: <code>[1]</code>, <code>[2]</code>...\n"
        "Знаки препинания, пробелы и регистр сохраняются без изменений.\n\n"
        "<b>3. Роль Штабного Аналитика:</b>\n"
        "• Выбирает текст из каталога или вводит свою фразу.\n"
        "• Видит открытый текст и частотный анализ букв.\n"
        "• Передает подсказки: раскрывает неизвестные буквы или отправляет частоты.\n\n"
        "<b>4. Роль Полевого Агента:</b>\n"
        "• Получает табло с маской текста.\n"
        "• Выбирает зашифрованный символ и подставляет предполагаемую букву.\n"
        "• При затруднении отправляет сигнал <code>SOS</code> в Штаб.\n\n"
        "<b>5. Команды:</b>\n"
        "• /start — Главное меню\n"
        "• /help — Справка и правила\n"
        "• /cancel — Отмена текущего действия"
    )
    kb = build_main_menu_keyboard()
    if isinstance(event, CallbackQuery):
        await event.answer()
        if event.message:
            await event.message.answer(help_text, reply_markup=kb, parse_mode="HTML")
    else:
        await event.answer(help_text, reply_markup=kb, parse_mode="HTML")


@common_router.message(Command("cancel"))
@common_router.callback_query(F.data == "action:cancel")
@common_router.callback_query(MenuCallback.filter(F.action == "cancel"))
async def handle_cancel(event: Union[Message, CallbackQuery], state: FSMContext) -> None:
    """Cancel current FSM operation and return to main menu."""
    await state.clear()
    text = "Действие отменено. Возврат в главное меню."
    kb = build_main_menu_keyboard()
    if isinstance(event, CallbackQuery):
        await event.answer("Отменено")
        if event.message:
            await event.message.answer(text, reply_markup=kb, parse_mode="HTML")
    else:
        await event.answer(text, reply_markup=kb, parse_mode="HTML")


@common_router.callback_query(MenuCallback.filter(F.action == "main"))
@common_router.callback_query(F.data == "menu:main")
async def handle_main_menu(callback: CallbackQuery, state: FSMContext) -> None:
    """Return to main menu via callback."""
    await state.clear()
    await callback.answer()
    first_name = html.escape(
        callback.from_user.first_name if callback.from_user else "Аналитик"
    )
    welcome_text = (
        f"🕵️‍♂️ <b>Главное меню Штаба ({first_name}):</b>\n\n"
        "Выберите действие для начала криптографической операции:"
    )
    if callback.message:
        await callback.message.answer(
            welcome_text, reply_markup=build_main_menu_keyboard(), parse_mode="HTML"
        )


@common_router.callback_query(MenuCallback.filter(F.action == "solo"))
@common_router.callback_query(F.data == "menu:solo")
async def handle_solo_menu(callback: CallbackQuery, state: FSMContext) -> None:
    """Prompt player to choose difficulty for Solo Game mode."""
    await state.clear()
    await callback.answer()
    text = (
        "🎯 <b>ОДИНОЧНЫЙ РЕЖИМ (СОЛО)</b>\n\n"
        "Штаб подготовит для вас зашифрованную депешу. "
        "Ваша задача — расшифровать её в роли Полевого Агента.\n\n"
        "Выберите сложность операции:"
    )
    kb = build_solo_difficulty_keyboard()
    await safe_edit_callback(callback, text=text, reply_markup=kb, parse_mode="HTML")


@common_router.callback_query(MenuCallback.filter(F.action == "solo_diff"))
async def handle_solo_start(
    callback: CallbackQuery,
    callback_data: MenuCallback,
    session_service: SessionService,
    bot: Bot,
    notifier: Optional[NotifierService] = None,
) -> None:
    """Start solo game session and open Field Agent Terminal."""
    user = callback.from_user
    if not user:
        return

    await callback.answer()
    difficulty = callback_data.code or "MEDIUM"

    if notifier is None:
        notifier = NotifierService(bot=bot, session_service=session_service)

    game = await session_service.create_solo_session(
        player_tg_id=user.id,
        difficulty=difficulty,
        username=user.username,
        first_name=user.first_name or "",
        last_name=user.last_name,
    )

    terminal_text = notifier.render_agent_terminal(game)
    kb = build_agent_symbol_keyboard(game)

    if callback.message:
        await safe_edit_callback(callback, text=terminal_text, reply_markup=kb, parse_mode="HTML")
        await session_service.update_message_ids(
            session_id_or_code=game.id,
            agent_chat_id=callback.message.chat.id,
            agent_message_id=callback.message.message_id,
        )



def render_player_profile(user: User) -> str:
    """Render stylized military dossier card with rank, EXP progress, and operational record."""
    name = html.escape(user.display_name)
    badge = user.current_rank_badge
    rank = html.escape(user.rank_title)
    exp = user.exp
    next_exp = user.next_rank_exp
    bar = user.rank_progress_bar

    if next_exp is not None:
        exp_line = f"• Опыт: <b>{exp}</b> / <b>{next_exp}</b> EXP"
    else:
        exp_line = f"• Опыт: <b>{exp}</b> EXP (Максимальное звание)"

    lines = [
        "🎖 <b>ЛИЧНОЕ ДЕЛО ВОЕННОСЛУЖАЩЕГО</b>",
        "──────────────────────────────",
        f"👤 <b>Позывной:</b> {name}",
        f"🎖 <b>Звание:</b> {badge} {rank}",
        exp_line,
        f"📈 <b>Прогресс:</b> <code>{bar}</code>",
        "",
        "📊 <b>Боевой послужной список:</b>",
        f"• Всего игр: <b>{user.games_played}</b>",
        f"• Побед: <b>{user.games_won}</b> (Винрейт: <b>{user.win_rate}%</b>)",
        f"• В роли Аналитика: <b>{user.games_as_analyst}</b>",
        f"• В роли Агента: <b>{user.games_as_agent}</b>",
        "",
        "🔍 <b>Качество дешифровки:</b>",
        f"• Ошибок допущено: <b>{user.total_mistakes}</b>",
        f"• Подсказок использовано: <b>{user.total_hints_used}</b>",
        "──────────────────────────────",
    ]
    return "\n".join(lines)


format_player_profile = render_player_profile


@common_router.message(Command("profile"))
async def handle_profile_command(
    message: Message,
    session_service: SessionService,
) -> None:
    """Render player profile dossier via /profile command."""
    if not message.from_user:
        return
    user = await session_service.get_or_create_user(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name or "",
        last_name=message.from_user.last_name,
    )
    text = render_player_profile(user)
    kb = build_profile_keyboard()
    await message.answer(text=text, reply_markup=kb, parse_mode="HTML")


@common_router.callback_query(MenuCallback.filter(F.action == "profile"))
@common_router.callback_query(F.data == "menu:profile")
async def handle_profile_callback(
    callback: CallbackQuery,
    session_service: SessionService,
) -> None:
    """Render or refresh player profile dossier via inline callback."""
    if not callback.from_user:
        return
    user = await session_service.get_or_create_user(
        telegram_id=callback.from_user.id,
        username=callback.from_user.username,
        first_name=callback.from_user.first_name or "",
        last_name=callback.from_user.last_name,
    )
    text = render_player_profile(user)
    kb = build_profile_keyboard()

    await callback.answer()
    await safe_edit_callback(
        callback=callback,
        text=text,
        reply_markup=kb,
        parse_mode="HTML",
    )

