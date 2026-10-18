"""Aiogram 3.x Router and handlers for Story Campaign (1941–1945).

«Сюжетная кампания: Секретные директивы Ставки (1941–1945)»

Features & Defensive Shields:
- Router `campaign_router` handling:
  1. Campaign chapter selection menu with dynamic status badges (✅, 🔓, 🔒).
  2. Historical pre-mission chapter briefing screen.
  3. Field terminal decryption session launch.
  4. Locked / classified chapter alert notification.
  5. Post-mission historical victory summary and next chapter progression.
- Absolute Button Crash Shield:
  - Every callback query handler calls `await callback.answer()` immediately.
  - Safe message editing via `safe_edit_callback` (suppressing TelegramBadRequest "message is not modified"
    and "message to edit not found").
  - Defensive None guards for `callback.from_user` and `callback.message`.
  - Boundary and out-of-bounds guards for chapter IDs.
  - Strict HTML escaping of all dynamic text using `html.escape`.
"""
import html
import logging
from typing import Optional, Set, Union

from aiogram import Bot, F, Router, types
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.callbacks import CampaignCallback, MenuCallback
from bot.keyboards import (
    build_agent_symbol_keyboard,
    build_campaign_briefing_keyboard,
    build_campaign_menu_keyboard,
    build_campaign_victory_keyboard,
    build_main_menu_keyboard,
)
from bot.utils import NotifierService, safe_edit_callback
from cryptogram.campaign_data import (
    CAMPAIGN_CHAPTERS,
    CAMPAIGN_TITLE,
    TOTAL_CHAPTERS,
    CampaignChapter,
    get_campaign_chapter,
)
from database.models import GameSession, SessionStatus
from services.campaign_service import CampaignService
from services.session_service import SessionService

logger = logging.getLogger(__name__)

campaign_router = Router(name="campaign")


@campaign_router.message(Command("campaign"))
@campaign_router.callback_query(MenuCallback.filter(F.action == "campaign"))
@campaign_router.callback_query(CampaignCallback.filter(F.action == "menu"))
@campaign_router.callback_query(F.data == "menu:campaign")
async def on_campaign_menu(
    event: Union[CallbackQuery, Message],
    session_service: Optional[SessionService] = None,
    state: Optional[FSMContext] = None,
) -> None:
    """Open or refresh the story campaign chapter selection menu."""
    if isinstance(event, CallbackQuery):
        try:
            await event.answer()
        except Exception:
            pass
        if event.from_user is None or event.message is None:
            return
        user_id = event.from_user.id
    else:
        if event.from_user is None:
            return
        user_id = event.from_user.id

    if state is not None:
        await state.clear()

    completed_chapters: Set[int] = set()
    if session_service and getattr(session_service, "session_factory", None):
        try:
            async with session_service.session_factory() as session:
                progress = await CampaignService.get_user_progress(session, user_id)
                completed_chapters = {ch for ch, comp in progress.items() if comp}
        except Exception as exc:
            logger.warning("Error fetching campaign progress for user %s: %s", user_id, exc)
            completed_chapters = set()

    completed_count = len(completed_chapters)
    menu_text = (
        "🎖 <b>СЮЖЕТНАЯ КАМПАНИЯ: СЕКРЕТНЫЕ ДИРЕКТИВЫ СТАВКИ (1941–1945)</b>\n\n"
        "Оперативные приказы и директивы высшего военного командования СССР "
        "с последовательным открытием глав и историческими сводками.\n\n"
        f"📊 Ваш прогресс: <b>{completed_count}/{TOTAL_CHAPTERS} глав</b>\n"
        "✅ — Пройдена (доступна для повтора)\n"
        "🔓 — Доступна к дешифровке\n"
        "🔒 — Засекречено (требуется прохождение предыдущей)\n\n"
        "<i>Выберите доступную главу для ознакомления с боевым заданием:</i>"
    )
    kb = build_campaign_menu_keyboard(completed_chapters)

    if isinstance(event, CallbackQuery):
        await safe_edit_callback(event, text=menu_text, reply_markup=kb, parse_mode="HTML")
    else:
        await event.answer(menu_text, reply_markup=kb, parse_mode="HTML")


@campaign_router.callback_query(CampaignCallback.filter(F.action == "locked"))
async def on_chapter_locked(
    callback: CallbackQuery,
    callback_data: CampaignCallback,
) -> None:
    """Defensive handler for clicks on classified/locked chapters."""
    chapter_num = callback_data.chapter if callback_data.chapter > 0 else "N"
    try:
        await callback.answer(
            f"🔒 Глава {chapter_num} засекречена!\n"
            "Сначала расшифруйте директивы предыдущих глав.",
            show_alert=True,
        )
    except Exception:
        pass


@campaign_router.callback_query(
    CampaignCallback.filter(F.action.in_({"chapter", "briefing", "select", "next"}))
)
async def on_chapter_briefing(
    callback: CallbackQuery,
    callback_data: CampaignCallback,
    session_service: Optional[SessionService] = None,
) -> None:
    """Display historical pre-mission briefing for a specific chapter."""
    if callback.from_user is None or callback.message is None:
        try:
            await callback.answer()
        except Exception:
            pass
        return

    chapter_id = callback_data.chapter
    if chapter_id < 1 or chapter_id > TOTAL_CHAPTERS:
        try:
            await callback.answer(
                f"Недопустимый номер главы: {chapter_id}.",
                show_alert=True,
            )
        except Exception:
            pass
        return

    # Progression check
    if session_service and getattr(session_service, "session_factory", None):
        try:
            async with session_service.session_factory() as session:
                can_play, reason = await CampaignService.can_play_chapter(
                    session, callback.from_user.id, chapter_id
                )
                if not can_play:
                    try:
                        await callback.answer(
                            reason or f"🔒 Глава {chapter_id} засекречена!",
                            show_alert=True,
                        )
                    except Exception:
                        pass
                    return
        except Exception as exc:
            logger.warning("Error verifying chapter %s access: %s", chapter_id, exc)

    ch = get_campaign_chapter(chapter_id)
    if not ch:
        try:
            await callback.answer("Глава не найдена!", show_alert=True)
        except Exception:
            pass
        return

    try:
        await callback.answer()
    except Exception:
        pass

    briefing_text = (
        f"🎖 <b>СЮЖЕТНАЯ КАМПАНИЯ: ГЛАВА {ch.id}</b>\n"
        f"«<b>{html.escape(ch.title)}</b>» ({ch.year} г.)\n"
        "──────────────────────────────\n"
        f"📅 <b>Дата:</b> {html.escape(ch.date_str)}\n"
        f"🏛 <b>Орган:</b> {html.escape(ch.issuing_body)}\n"
        f"📄 <b>Документ:</b> {html.escape(ch.document_name)}\n\n"
        "📜 <b>Оперативная обстановка:</b>\n"
        f"{html.escape(ch.operational_context)}\n\n"
        "<i>Штаб перехватил зашифрованную директиву. Ваша задача — провести дешифровку "
        "и восстановить подлинный текст приказа высшего командования.</i>\n"
        "──────────────────────────────"
    )
    kb = build_campaign_briefing_keyboard(chapter_id=ch.id)
    await safe_edit_callback(callback, text=briefing_text, reply_markup=kb, parse_mode="HTML")


@campaign_router.callback_query(CampaignCallback.filter(F.action == "start"))
async def on_start_campaign_chapter(
    callback: CallbackQuery,
    callback_data: CampaignCallback,
    session_service: Optional[SessionService] = None,
    bot: Optional[Bot] = None,
    notifier: Optional[NotifierService] = None,
) -> None:
    """Launch solo field terminal decryption for the selected campaign chapter."""
    if callback.from_user is None or callback.message is None:
        try:
            await callback.answer()
        except Exception:
            pass
        return

    chapter_id = callback_data.chapter
    if chapter_id < 1 or chapter_id > TOTAL_CHAPTERS:
        try:
            await callback.answer(
                f"Недопустимый номер главы: {chapter_id}.",
                show_alert=True,
            )
        except Exception:
            pass
        return

    ch = get_campaign_chapter(chapter_id)
    if not ch:
        try:
            await callback.answer("Глава не найдена!", show_alert=True)
        except Exception:
            pass
        return

    user = callback.from_user
    if bot and session_service and notifier is None:
        notifier = NotifierService(bot=bot, session_service=session_service)

    try:
        if not session_service or not getattr(session_service, "session_factory", None):
            raise ValueError("session_service has no session_factory")

        async with session_service.session_factory() as session:
            can_play, reason = await CampaignService.can_play_chapter(session, user.id, chapter_id)
            if not can_play:
                try:
                    await callback.answer(
                        reason or f"🔒 Глава {chapter_id} засекречена!",
                        show_alert=True,
                    )
                except Exception:
                    pass
                return

            game = await CampaignService.create_campaign_session(
                session,
                user_tg_id=user.id,
                chapter_id=chapter_id,
                username=user.username,
                first_name=user.first_name or "",
                last_name=user.last_name,
            )
    except Exception as exc:
        logger.error(
            "Failed to create campaign session for user %s chapter %s: %s",
            user.id,
            chapter_id,
            exc,
        )
        try:
            await callback.answer("Ошибка запуска главы! Попробуйте позже.", show_alert=True)
        except Exception:
            pass
        return

    try:
        await callback.answer()
    except Exception:
        pass

    if notifier is not None:
        terminal_text = notifier.render_agent_terminal(game)
    else:
        from cryptogram.engine import render_mask
        masked = render_mask(game.original_text, game.cipher_mapping, game.revealed_letters, wrap_pre=True)
        terminal_text = (
            "🕵️‍♂️ <b>ПОЛЕВОЙ ТЕРМИНАЛ ДЕШИФРОВКИ</b>\n"
            f"Операция: <code>{game.invite_code}</code>\n\n"
            "<b>Текущая депеша:</b>\n"
            f"{masked}\n\n"
            f"📊 Прогресс: <b>{game.revealed_count} / {game.total_unique_letters}</b> ({game.progress_percentage}%)\n"
            f"⚠️ Ошибок: <b>{game.mistakes_count}</b> | 💡 Подсказок Штаба: <b>{game.hints_used}</b>\n\n"
            "<i>Выберите зашифрованный символ для подстановки буквы:</i>"
        )

    header = (
        f"🎖 <b>ШТАБ ВГК: СЮЖЕТНАЯ КАМПАНИЯ</b>\n"
        f"<b>Глава {ch.id}: {html.escape(ch.title)}</b> (Операция: <code>{game.invite_code}</code>)\n\n"
    )
    full_terminal = header + terminal_text
    kb = build_agent_symbol_keyboard(game)

    await safe_edit_callback(callback, text=full_terminal, reply_markup=kb, parse_mode="HTML")
    if session_service and callback.message:
        await session_service.update_message_ids(
            session_id_or_code=game.id,
            agent_chat_id=callback.message.chat.id,
            agent_message_id=callback.message.message_id,
        )


@campaign_router.callback_query(
    CampaignCallback.filter(F.action.in_({"victory", "debriefing"}))
)
async def on_campaign_victory_debriefing(
    callback: CallbackQuery,
    callback_data: CampaignCallback,
    session_service: Optional[SessionService] = None,
) -> None:
    """Display post-mission historical victory summary and next chapter progression."""
    try:
        await callback.answer()
    except Exception:
        pass

    if callback.from_user is None or callback.message is None:
        return

    chapter_id = max(1, min(TOTAL_CHAPTERS, callback_data.chapter))
    ch = get_campaign_chapter(chapter_id)
    if not ch:
        return

    next_unlocked = False
    if session_service and getattr(session_service, "session_factory", None) and chapter_id < TOTAL_CHAPTERS:
        try:
            async with session_service.session_factory() as session:
                can_play, _ = await CampaignService.can_play_chapter(
                    session, callback.from_user.id, chapter_id + 1
                )
                next_unlocked = can_play
        except Exception:
            next_unlocked = True
    else:
        next_unlocked = (chapter_id < TOTAL_CHAPTERS)

    victory_text = (
        f"🏆 <b>ДЕПЕША УСПЕШНО РАСШИФРОВАНА!</b>\n"
        f"🎖 <b>Глава {ch.id}: {html.escape(ch.title)}</b> ({ch.year} г.)\n"
        "──────────────────────────────\n"
        "<b>Подлинный текст директивы:</b>\n"
        f"<pre>{html.escape(ch.clean_text)}</pre>\n\n"
        "📜 <b>Историческая сводка:</b>\n"
        f"{html.escape(ch.victory_summary)}\n"
        "──────────────────────────────\n"
        "<i>Штаб выражает благодарность за восстановление секретной директивы!</i>"
    )
    kb = build_campaign_victory_keyboard(current_chapter_id=ch.id, next_unlocked=next_unlocked)
    await safe_edit_callback(callback, text=victory_text, reply_markup=kb, parse_mode="HTML")


@campaign_router.callback_query(CampaignCallback.filter())
async def on_campaign_fallback(callback: CallbackQuery) -> None:
    """Defensive catch-all for any unhandled campaign callback queries."""
    try:
        await callback.answer()
    except Exception:
        pass


__all__ = [
    "campaign_router",
    "on_campaign_menu",
    "on_chapter_locked",
    "on_chapter_briefing",
    "on_start_campaign_chapter",
    "on_campaign_victory_debriefing",
    "on_campaign_fallback",
]
