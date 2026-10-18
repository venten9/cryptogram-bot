"""Agent handlers: Symbol selection, alphabet substitution picker, guess submission, SOS request."""
from typing import Optional

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery

from bot.callbacks import HintActionCallback, LetterGuessCallback, SymbolSelectCallback
from bot.keyboards import build_agent_alphabet_keyboard, build_agent_symbol_keyboard
from bot.utils import NotifierService, safe_edit_callback
from cryptogram.engine import render_mask
from cryptogram.models import GuessStatus
from database.models import SessionStatus
from services.session_service import SessionService, UnauthorizedActionError

agent_router = Router(name="agent")


@agent_router.callback_query(SymbolSelectCallback.filter())
async def on_symbol_selected(
    callback: CallbackQuery,
    callback_data: SymbolSelectCallback,
    session_service: SessionService,
    bot: Bot,
    notifier: Optional[NotifierService] = None,
) -> None:
    """Agent selects a cipher symbol to decrypt -> Switch to Alphabet Picker or return."""
    game = await session_service.get_session_by_code(callback_data.code)
    if not game or game.status != SessionStatus.IN_PROGRESS:
        await callback.answer("Операция не активна!", show_alert=True)
        return

    if notifier is None:
        notifier = NotifierService(bot=bot, session_service=session_service)

    symbol_code = callback_data.symbol

    # Sentinel symbol=0 means 'Back to symbol grid'
    if symbol_code == 0:
        await callback.answer()
        await notifier.sync_agent_terminal(game)
        return

    if str(symbol_code) in game.guessed_mapping:
        already_revealed = game.guessed_mapping[str(symbol_code)]
        await callback.answer(
            f"Символ [{symbol_code}] уже разгадан как '{already_revealed}'!",
            show_alert=False,
        )
        return

    await callback.answer()

    masked = render_mask(
        game.original_text,
        game.cipher_mapping,
        game.revealed_letters,
        wrap_pre=True,
    )
    text = (
        "🕵️‍♂️ <b>ПОЛЕВОЙ ТЕРМИНАЛ ДЕШИФРОВКИ</b>\n\n"
        f"Текущая депеша:\n{masked}\n\n"
        f"🔑 <b>Подстановка для символа [{symbol_code}]:</b>\n"
        "Выберите предполагаемую букву алфавита:"
    )
    kb = build_agent_alphabet_keyboard(invite_code=game.invite_code, symbol_code=symbol_code)
    await safe_edit_callback(callback, text=text, reply_markup=kb, parse_mode="HTML")


@agent_router.callback_query(LetterGuessCallback.filter())
async def on_letter_guess_submitted(
    callback: CallbackQuery,
    callback_data: LetterGuessCallback,
    session_service: SessionService,
    bot: Bot,
    notifier: Optional[NotifierService] = None,
) -> None:
    """Agent submits a letter substitution guess."""
    code = callback_data.code
    symbol = callback_data.symbol
    letter = callback_data.letter

    game = await session_service.get_session_by_code(code)
    if not game or game.status != SessionStatus.IN_PROGRESS:
        await callback.answer("Операция не активна!", show_alert=True)
        return

    user = callback.from_user
    if not user:
        return

    if notifier is None:
        notifier = NotifierService(bot=bot, session_service=session_service)

    try:
        res = await session_service.submit_guess(
            session_id_or_code=game.id,
            user_tg_id=user.id,
            symbol_code=symbol,
            letter=letter,
        )
    except UnauthorizedActionError as exc:
        await callback.answer(str(exc) or "⛔ Действие недоступно для вашей роли!", show_alert=True)
        return

    reloaded = await session_service.get_session_by_id(game.id)
    if not reloaded:
        return

    if res.status == GuessStatus.VICTORY:
        await callback.answer("🎉 ПОБЕДА! Все символы дешифрованы!", show_alert=True)
        await notifier.notify_victory(reloaded)
        return

    if res.status == GuessStatus.CORRECT:
        await callback.answer(f"✅ Верно! [{symbol}] = '{letter}'")
        await notifier.sync_game_state(
            game=reloaded,
            agent_alert=f"✅ Верно! Символ [{symbol}] — это буква <b>'{letter}'</b>.",
            analyst_alert=f"✅ Агент разгадал символ [{symbol}] = '{letter}'!",
        )
        return

    if res.status == GuessStatus.WRONG:
        await callback.answer(f"❌ Неверно! [{symbol}] — это не '{letter}'", show_alert=True)
        await notifier.sync_agent_terminal(
            game=reloaded,
            alert=f"❌ Ошибка! Символ [{symbol}] — не буква '{letter}'. (Ошибок: {reloaded.mistakes_count})",
        )
        return

    if res.status == GuessStatus.ALREADY_REVEALED:
        await callback.answer("Этот символ уже открыт!")
        await notifier.sync_agent_terminal(reloaded)
        return

    if res.status == GuessStatus.INVALID_CODE:
        await callback.answer("Неверный код символа!", show_alert=True)
        await notifier.sync_agent_terminal(reloaded)
        return


@agent_router.callback_query(F.data.startswith("back:"))
async def on_back_to_symbol_grid(
    callback: CallbackQuery,
    session_service: SessionService,
    bot: Bot,
    notifier: Optional[NotifierService] = None,
) -> None:
    """Return from Alphabet Picker back to Symbol Grid."""
    await callback.answer()
    code = callback.data.split(":")[1]
    game = await session_service.get_session_by_code(code)
    if game:
        if notifier is None:
            notifier = NotifierService(bot=bot, session_service=session_service)
        await notifier.sync_agent_terminal(game)


@agent_router.callback_query(HintActionCallback.filter(F.action == "sos"))
@agent_router.callback_query(F.data.startswith("sos:"))
async def on_agent_sos_request(
    callback: CallbackQuery,
    session_service: SessionService,
    bot: Bot,
    notifier: Optional[NotifierService] = None,
) -> None:
    """Agent transmits emergency SOS help request to Headquarters."""
    code = ""
    if ":" in callback.data:
        parts = callback.data.split(":")
        # hint:<code>:sos:0 or sos:<code>
        if parts[0] == "hint" and len(parts) >= 2:
            code = parts[1]
        elif parts[0] == "sos" and len(parts) >= 2:
            code = parts[1]

    game = await session_service.get_session_by_code(code)
    if not game or game.status != SessionStatus.IN_PROGRESS:
        await callback.answer("Операция не активна!", show_alert=True)
        return

    user = callback.from_user
    if not user:
        return

    if notifier is None:
        notifier = NotifierService(bot=bot, session_service=session_service)

    try:
        await session_service.request_help(game.id, user.id)
    except UnauthorizedActionError as exc:
        await callback.answer(str(exc) or "⛔ Действие недоступно для вашей роли!", show_alert=True)
        return

    await callback.answer(
        "🚨 Сигнал SOS передан в Штаб! Ожидайте радиограмму.", show_alert=True
    )
    await notifier.notify_sos_requested(game)
