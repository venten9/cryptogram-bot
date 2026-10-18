"""Analyst handlers: Custom text FSM, catalog selection, hint dispatch, dossier refresh."""
import html
from typing import Optional

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from bot.callbacks import CatalogCallback, HintActionCallback, MenuCallback
from bot.keyboards import (
    build_agent_symbol_keyboard,
    build_analyst_dossier_keyboard,
    build_cancel_keyboard,
    build_catalog_keyboard,
    build_main_menu_keyboard,
)
from bot.utils import NotifierService, safe_edit_callback
from cryptogram.catalog import get_catalog, get_catalog_item
from cryptogram.engine import validate_text
from database.models import HintType, SessionStatus
from services.session_service import (
    InvalidHintError,
    SessionError,
    SessionInvalidStateError,
    SessionNotFoundError,
    SessionService,
    UnauthorizedActionError,
)

analyst_router = Router(name="analyst")


class AnalystFSM(StatesGroup):
    """FSM states for Headquarters Analyst custom phrase input."""

    waiting_for_custom_text = State()


# ------------------------------------------------------------------
# Custom Text Creation Flow
# ------------------------------------------------------------------


@analyst_router.callback_query(MenuCallback.filter(F.action == "custom"))
@analyst_router.callback_query(F.data == "menu:custom")
async def on_menu_custom_text(callback: CallbackQuery, state: FSMContext) -> None:
    """Prompt Analyst to enter custom secret dispatch text."""
    await callback.answer()
    await state.set_state(AnalystFSM.waiting_for_custom_text)
    prompt = (
        "📝 <b>Ввод депеши для шифрования:</b>\n\n"
        "Отправьте текстовое сообщение с фразой на русском языке.\n\n"
        "<b>Требования к тексту:</b>\n"
        "• Длина от 10 до 250 символов\n"
        "• Только русский алфавит (буквы 'Е' и 'Ё' эквивалентны)\n"
        "• Минимум 3 уникальные русские буквы\n"
        "• Знаки препинания и пробелы сохраняются\n\n"
        "<i>Для отмены отправьте /cancel</i>"
    )
    if callback.message:
        await callback.message.answer(
            prompt, reply_markup=build_cancel_keyboard(), parse_mode="HTML"
        )


@analyst_router.message(AnalystFSM.waiting_for_custom_text, F.text)
async def on_custom_text_input(
    message: Message,
    state: FSMContext,
    session_service: SessionService,
    bot: Bot,
    notifier: Optional[NotifierService] = None,
    bot_username: Optional[str] = None,
) -> None:
    """Validate custom text and initialize game session."""
    raw_text = (message.text or "").strip()

    validation = validate_text(
        raw_text,
        min_length=10,
        max_length=250,
        min_unique_letters=3,
        disallow_latin=True,
    )
    if not validation.is_valid:
        await message.answer(
            f"❌ <b>Ошибка валидации:</b> {validation.error_message}\n\nПопробуйте снова или отправьте /cancel.",
            reply_markup=build_cancel_keyboard(),
            parse_mode="HTML",
        )
        return

    await state.clear()
    user = message.from_user
    if not user:
        return

    if notifier is None:
        notifier = NotifierService(bot=bot, session_service=session_service)

    game = await session_service.create_session(
        creator_tg_id=user.id,
        text=raw_text,
        is_custom=True,
        username=user.username,
        first_name=user.first_name or "",
    )

    if not bot_username:
        try:
            bot_user = await bot.get_me()
            bot_username = bot_user.username or "SpyAnalystBot"
        except Exception:
            bot_username = "SpyAnalystBot"

    invite_link = f"https://t.me/{bot_username}?start=game_{game.invite_code}"

    dossier_text = notifier.render_analyst_dossier(game, invite_link=invite_link)
    kb = build_analyst_dossier_keyboard(game, invite_link=invite_link, bot_username=bot_username)

    dossier_msg = await message.answer(dossier_text, reply_markup=kb, parse_mode="HTML")

    await session_service.update_message_ids(
        session_id_or_code=game.id,
        analyst_chat_id=dossier_msg.chat.id,
        analyst_message_id=dossier_msg.message_id,
    )


# ------------------------------------------------------------------
# Catalog Selection Flow
# ------------------------------------------------------------------


@analyst_router.callback_query(MenuCallback.filter(F.action == "catalog"))
@analyst_router.callback_query(F.data == "menu:catalog")
async def on_menu_catalog(callback: CallbackQuery) -> None:
    """Display first page of predefined dispatches from catalog."""
    await callback.answer()
    catalog = get_catalog()
    page = 0
    page_size = 4
    total_pages = max(1, (len(catalog) + page_size - 1) // page_size)
    items = catalog[page * page_size : (page + 1) * page_size]

    text = f"📖 <b>КАТАЛОГ СЕКРЕТНЫХ ДЕПЕШ (Стр. {page + 1}/{total_pages})</b>\n\nВыберите депешу для операции:"
    kb = build_catalog_keyboard(items=items, page=page, total_pages=total_pages)

    if callback.message:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")


@analyst_router.callback_query(CatalogCallback.filter(F.action == "page"))
async def on_catalog_page_navigation(
    callback: CallbackQuery, callback_data: CatalogCallback
) -> None:
    """Paginate catalog items."""
    await callback.answer()
    catalog = get_catalog()
    page_size = 4
    total_pages = max(1, (len(catalog) + page_size - 1) // page_size)
    page = max(0, min(callback_data.page, total_pages - 1))
    items = catalog[page * page_size : (page + 1) * page_size]

    text = f"📖 <b>КАТАЛОГ СЕКРЕТНЫХ ДЕПЕШ (Стр. {page + 1}/{total_pages})</b>\n\nВыберите депешу для операции:"
    kb = build_catalog_keyboard(items=items, page=page, total_pages=total_pages)
    await safe_edit_callback(callback, text=text, reply_markup=kb, parse_mode="HTML")


@analyst_router.callback_query(CatalogCallback.filter(F.action.in_({"pick", "select"})))
async def on_catalog_quote_selected(
    callback: CallbackQuery,
    callback_data: CatalogCallback,
    session_service: SessionService,
    bot: Bot,
    notifier: Optional[NotifierService] = None,
    bot_username: Optional[str] = None,
) -> None:
    """Create game session from selected catalog quote."""
    await callback.answer()
    quote = get_catalog_item(callback_data.item_id)
    if not quote:
        await callback.answer("Депеша не найдена!", show_alert=True)
        return

    user = callback.from_user
    if not user:
        return

    if notifier is None:
        notifier = NotifierService(bot=bot, session_service=session_service)

    game = await session_service.create_session(
        creator_tg_id=user.id,
        text=quote.text,
        is_custom=False,
        catalog_id=quote.id,
        username=user.username,
        first_name=user.first_name or "",
    )

    if not bot_username:
        try:
            bot_user = await bot.get_me()
            bot_username = bot_user.username or "SpyAnalystBot"
        except Exception:
            bot_username = "SpyAnalystBot"

    invite_link = f"https://t.me/{bot_username}?start=game_{game.invite_code}"

    dossier_text = notifier.render_analyst_dossier(game, invite_link=invite_link)
    kb = build_analyst_dossier_keyboard(game, invite_link=invite_link, bot_username=bot_username)

    if callback.message:
        await safe_edit_callback(callback, text=dossier_text, reply_markup=kb, parse_mode="HTML")
        await session_service.update_message_ids(
            session_id_or_code=game.id,
            analyst_chat_id=callback.message.chat.id,
            analyst_message_id=callback.message.message_id,
        )


# ------------------------------------------------------------------
# Analyst Tactical Controls (Hints & Refresh)
# ------------------------------------------------------------------


@analyst_router.callback_query(HintActionCallback.filter(F.action == "reveal"))
async def on_analyst_reveal_letter(
    callback: CallbackQuery,
    callback_data: HintActionCallback,
    session_service: SessionService,
    bot: Bot,
    notifier: Optional[NotifierService] = None,
) -> None:
    """Analyst transmits a letter reveal hint."""
    game = await session_service.get_session_by_code(callback_data.code)
    if not game or game.status != SessionStatus.IN_PROGRESS:
        await callback.answer("Операция не активна или ожидает Агента!", show_alert=True)
        return

    if notifier is None:
        notifier = NotifierService(bot=bot, session_service=session_service)

    try:
        hint_res = await session_service.give_hint(
            session_id_or_code=game.id,
            user_tg_id=callback.from_user.id,
            hint_type=HintType.REVEAL_LETTER,
            symbol_code=callback_data.symbol if callback_data.symbol > 0 else None,
        )
    except UnauthorizedActionError as exc:
        await callback.answer(str(exc) or "⛔ Действие недоступно для вашей роли!", show_alert=True)
        return
    except InvalidHintError as exc:
        await callback.answer(str(exc), show_alert=True)
        return

    await callback.answer(f"Раскрыт символ [{hint_res.symbol_code}] = '{hint_res.letter}'!")

    reloaded = await session_service.get_session_by_id(game.id)
    if not reloaded:
        return

    if reloaded.status == SessionStatus.SOLVED:
        await notifier.notify_victory(reloaded)
    else:
        await notifier.sync_game_state(
            game=reloaded,
            agent_alert=f"📡 <b>Штаб передал разведданные:</b>\nСимвол [{hint_res.symbol_code}] — это буква <b>'{hint_res.letter}'</b>!",
            analyst_alert=f"💡 Подсказка передана: символ [{hint_res.symbol_code}] = '{hint_res.letter}'",
        )


@analyst_router.callback_query(HintActionCallback.filter(F.action == "freq"))
async def on_analyst_send_frequencies(
    callback: CallbackQuery,
    callback_data: HintActionCallback,
    session_service: SessionService,
    bot: Bot,
    notifier: Optional[NotifierService] = None,
) -> None:
    """Analyst transmits letter frequency distribution."""
    game = await session_service.get_session_by_code(callback_data.code)
    if not game or game.status != SessionStatus.IN_PROGRESS:
        await callback.answer("Операция не активна или ожидает Агента!", show_alert=True)
        return

    if notifier is None:
        notifier = NotifierService(bot=bot, session_service=session_service)

    try:
        hint_res = await session_service.give_hint(
            session_id_or_code=game.id,
            user_tg_id=callback.from_user.id,
            hint_type=HintType.FREQUENCY_REPORT,
        )
    except UnauthorizedActionError as exc:
        await callback.answer(str(exc) or "⛔ Действие недоступно для вашей роли!", show_alert=True)
        return
    except InvalidHintError as exc:
        await callback.answer(str(exc), show_alert=True)
        return

    await callback.answer("Частотный анализ передан Агенту!")
    await notifier.send_frequency_report(game, hint_res.content)


@analyst_router.callback_query(HintActionCallback.filter(F.action == "refresh"))
async def on_analyst_refresh_dossier(
    callback: CallbackQuery,
    callback_data: HintActionCallback,
    session_service: SessionService,
    bot: Bot,
    notifier: Optional[NotifierService] = None,
    bot_username: Optional[str] = None,
) -> None:
    """Refresh Analyst HQ Dossier or Agent Field Terminal display based on caller role."""
    game = await session_service.get_session_by_code(callback_data.code)
    if not game:
        await callback.answer("Операция не активна!", show_alert=True)
        return

    user = callback.from_user
    user_id = user.id if user else None

    if user_id != game.analyst_id and user_id != game.agent_id:
        await callback.answer("⛔ Вы не являетесь участником этой операции!", show_alert=True)
        return

    if notifier is None:
        notifier = NotifierService(bot=bot, session_service=session_service)

    await callback.answer("Табло обновлено")

    if user_id == game.analyst_id:
        invite_link = None
        if game.status == SessionStatus.WAITING_AGENT:
            b_user = bot_username or "SpyAnalystBot"
            invite_link = f"https://t.me/{b_user}?start=game_{game.invite_code}"

        text = notifier.render_analyst_dossier(game, invite_link=invite_link)
        kb = build_analyst_dossier_keyboard(
            game, invite_link=invite_link, bot_username=bot_username
        )
        await safe_edit_callback(callback, text=text, reply_markup=kb, parse_mode="HTML")
    elif user_id == game.agent_id:
        text = notifier.render_agent_terminal(game)
        kb = build_agent_symbol_keyboard(game)
        await safe_edit_callback(callback, text=text, reply_markup=kb, parse_mode="HTML")


@analyst_router.callback_query(HintActionCallback.filter(F.action == "cancel"))
async def on_analyst_cancel_session(
    callback: CallbackQuery,
    callback_data: HintActionCallback,
    session_service: SessionService,
) -> None:
    """Analyst cancels a waiting session."""
    if not callback.from_user:
        return

    try:
        await session_service.cancel_session(
            session_identifier=callback_data.code,
            user_tg_id=callback.from_user.id,
        )
    except UnauthorizedActionError as exc:
        await callback.answer(str(exc) or "⛔ Действие недоступно для вашей роли!", show_alert=True)
        return
    except SessionInvalidStateError:
        await callback.answer("Невозможно отменить активную операцию.", show_alert=True)
        return
    except SessionNotFoundError:
        await callback.answer("Операция не найдена!", show_alert=True)
        return

    await callback.answer("Операция отменена")
    if callback.message:
        await callback.message.answer(
            "Миссия отменена. Возврат в главное меню.",
            reply_markup=build_main_menu_keyboard(),
        )
