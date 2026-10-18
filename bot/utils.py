"""Safe Telegram messaging utilities and cross-player synchronization service."""
import html
import logging
from typing import Optional, Union

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import CallbackQuery, InlineKeyboardMarkup

from cryptogram.engine import render_mask
from database.models import GameSession, SessionStatus, User
from services.session_service import SessionService

logger = logging.getLogger(__name__)


async def safe_edit_message(
    bot: Bot,
    chat_id: int,
    message_id: int,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    parse_mode: str = "HTML",
) -> bool:
    """Safely edit a message, suppressing 'message is not modified' and 'message to edit not found'."""
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
        return True
    except TelegramBadRequest as exc:
        msg = str(exc).lower()
        if "message is not modified" in msg:
            return False
        if "message to edit not found" in msg:
            logger.warning("Message %s in chat %s not found for editing", message_id, chat_id)
            return False
        logger.error(
            "TelegramBadRequest editing message %s in chat %s: %s",
            message_id,
            chat_id,
            exc,
        )
        return False
    except TelegramForbiddenError:
        logger.warning("Bot blocked by user in chat %s", chat_id)
        return False
    except Exception as exc:
        logger.error(
            "Unexpected error editing message %s in chat %s: %s",
            message_id,
            chat_id,
            exc,
        )
        return False


async def safe_edit_callback(
    callback: CallbackQuery,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    parse_mode: str = "HTML",
) -> bool:
    """Safely edit the message associated with an incoming CallbackQuery."""
    if not callback.message:
        return False
    try:
        await callback.message.edit_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
        return True
    except TelegramBadRequest as exc:
        msg = str(exc).lower()
        if "message is not modified" in msg:
            return False
        if "message to edit not found" in msg:
            return False
        logger.error("TelegramBadRequest editing callback message: %s", exc)
        return False
    except Exception as exc:
        logger.error("Unexpected error editing callback message: %s", exc)
        return False


class NotifierService:
    """Coordinates tactical UI updates and notifications across Analyst HQ and Field Agent."""

    def __init__(self, bot: Bot, session_service: SessionService) -> None:
        self.bot = bot
        self.session_service = session_service

    def render_agent_terminal(self, game: GameSession, alert: Optional[str] = None) -> str:
        """Render the Field Agent decryption terminal screen."""
        masked = render_mask(
            game.original_text,
            game.cipher_mapping,
            game.revealed_letters,
            wrap_pre=True,
        )
        lines = [
            "🕵️‍♂️ <b>ПОЛЕВОЙ ТЕРМИНАЛ ДЕШИФРОВКИ</b>",
            f"Операция: <code>{game.invite_code}</code>",
            "",
            "<b>Текущая депеша:</b>",
            masked,
            "",
            f"📊 Прогресс: <b>{game.revealed_count} / {game.total_unique_letters}</b> ({game.progress_percentage}%)",
            f"⚠️ Ошибок: <b>{game.mistakes_count}</b> | 💡 Подсказок Штаба: <b>{game.hints_used}</b>",
        ]
        if alert:
            lines.append(f"\n{alert}")
        else:
            lines.append("\n<i>Выберите зашифрованный символ для подстановки буквы:</i>")
        return "\n".join(lines)

    def render_analyst_dossier(
        self,
        game: GameSession,
        invite_link: Optional[str] = None,
        alert: Optional[str] = None,
    ) -> str:
        """Render Headquarters Analyst operation dossier screen."""
        status_map = {
            SessionStatus.WAITING_AGENT: "⏳ Ожидание подключения Агента",
            SessionStatus.IN_PROGRESS: "🟢 Операция в процессе",
            SessionStatus.SOLVED: "🏆 Депеша полностью расшифрована",
            SessionStatus.CANCELLED: "❌ Операция отменена",
        }
        status_text = status_map.get(game.status, str(game.status.value))
        agent_name = (
            f"@{game.agent.username}"
            if (game.agent and game.agent.username)
            else (game.agent.first_name if game.agent else "Не назначен")
        )

        lines = [
            "📋 <b>ДОСЬЕ СПЕЦОПЕРАЦИИ (ШТАБ)</b>",
            f"Код операции: <code>{game.invite_code}</code>",
            f"Статус: <b>{status_text}</b>",
            f"Полевой Агент: <b>{html.escape(agent_name)}</b>",
            "",
            "<b>Исходный текст депеши:</b>",
            f"<pre>{html.escape(game.original_text)}</pre>",
            "",
            f"📊 Раскрыто символов: <b>{game.revealed_count} / {game.total_unique_letters}</b> ({game.progress_percentage}%)",
            f"⚠️ Ошибок Агента: <b>{game.mistakes_count}</b> | 💡 Передано подсказок: <b>{game.hints_used}</b>",
        ]

        if game.status == SessionStatus.WAITING_AGENT and invite_link:
            lines.extend(
                [
                    "",
                    "🔗 <b>Ссылка для подключения напарника:</b>",
                    f"<code>{invite_link}</code>",
                    "<i>Отправьте эту ссылку второму игроку для начала операции.</i>",
                ]
            )

        if alert:
            lines.append(f"\n{alert}")

        return "\n".join(lines)

    async def notify_agent_joined(self, game: GameSession, agent: User) -> None:
        """Notify Analyst when Field Agent connects via deep-link."""
        from bot.keyboards import build_analyst_dossier_keyboard

        if game.analyst_chat_id and game.analyst_message_id:
            text = self.render_analyst_dossier(
                game=game,
                alert="🚨 <b>Агент подключился к операции! Терминал активирован.</b>",
            )
            kb = build_analyst_dossier_keyboard(game)
            await safe_edit_message(
                bot=self.bot,
                chat_id=game.analyst_chat_id,
                message_id=game.analyst_message_id,
                text=text,
                reply_markup=kb,
            )

        agent_display = agent.first_name or (
            f"@{agent.username}" if agent.username else f"Agent_{agent.id}"
        )
        if game.analyst_chat_id:
            await self.bot.send_message(
                chat_id=game.analyst_chat_id,
                text=f"🚨 <b>Агент подключился к операции!</b>\nПозывной: <b>{html.escape(agent_display)}</b>\nДешифровка началась.",
                parse_mode="HTML",
            )

    async def sync_game_state(
        self,
        game: GameSession,
        agent_alert: Optional[str] = None,
        analyst_alert: Optional[str] = None,
    ) -> None:
        """Synchronize both players' screens after an accurate guess or hint."""
        from bot.keyboards import build_agent_symbol_keyboard, build_analyst_dossier_keyboard

        if game.agent_chat_id and game.agent_message_id:
            await safe_edit_message(
                bot=self.bot,
                chat_id=game.agent_chat_id,
                message_id=game.agent_message_id,
                text=self.render_agent_terminal(game, alert=agent_alert),
                reply_markup=build_agent_symbol_keyboard(game),
            )

        if game.analyst_chat_id and game.analyst_message_id:
            await safe_edit_message(
                bot=self.bot,
                chat_id=game.analyst_chat_id,
                message_id=game.analyst_message_id,
                text=self.render_analyst_dossier(game, alert=analyst_alert),
                reply_markup=build_analyst_dossier_keyboard(game),
            )

    async def sync_agent_terminal(self, game: GameSession, alert: Optional[str] = None) -> None:
        """Update only the Field Agent's terminal screen."""
        from bot.keyboards import build_agent_symbol_keyboard

        if game.agent_chat_id and game.agent_message_id:
            await safe_edit_message(
                bot=self.bot,
                chat_id=game.agent_chat_id,
                message_id=game.agent_message_id,
                text=self.render_agent_terminal(game, alert=alert),
                reply_markup=build_agent_symbol_keyboard(game),
            )

    async def notify_sos_requested(self, game: GameSession) -> None:
        """Notify Analyst of Agent SOS request."""
        from bot.keyboards import build_analyst_dossier_keyboard

        if game.analyst_chat_id:
            await self.bot.send_message(
                chat_id=game.analyst_chat_id,
                text=(
                    "🚨 <b>ВНИМАНИЕ ШТАБ! СИГНАЛ SOS!</b>\n\n"
                    "Полевой Агент запрашивает срочную помощь по расшифровке!\n"
                    "Воспользуйтесь кнопкой <b>[💡 Раскрыть символ]</b> в досье операции."
                ),
                parse_mode="HTML",
            )

        if game.analyst_chat_id and game.analyst_message_id:
            await safe_edit_message(
                bot=self.bot,
                chat_id=game.analyst_chat_id,
                message_id=game.analyst_message_id,
                text=self.render_analyst_dossier(
                    game, alert="🚨 <b>АГЕНТ ЗАПРОСИЛ ПОМОЩЬ ШТАБА!</b>"
                ),
                reply_markup=build_analyst_dossier_keyboard(game),
            )

    async def send_frequency_report(self, game: GameSession, content: str) -> None:
        """Send frequency distribution report to Agent and update Analyst."""
        from bot.keyboards import build_analyst_dossier_keyboard

        if game.agent_chat_id:
            await self.bot.send_message(
                chat_id=game.agent_chat_id,
                text=f"📡 <b>РАДИОГРАММА ИЗ ШТАБА:</b>\n\n{content}",
                parse_mode="HTML",
            )

        if game.analyst_chat_id and game.analyst_message_id:
            await safe_edit_message(
                bot=self.bot,
                chat_id=game.analyst_chat_id,
                message_id=game.analyst_message_id,
                text=self.render_analyst_dossier(
                    game, alert="📊 Частотный отчет передан на терминал Агента."
                ),
                reply_markup=build_analyst_dossier_keyboard(game),
            )

    async def notify_victory(self, game: GameSession) -> None:
        """Broadcast victory screens and play-again options to both players."""
        from bot.keyboards import build_victory_keyboard

        agent_text = (
            "🎉 <b>ПОБЕДА! ОПЕРАЦИЯ УСПЕШНО ЗАВЕРШЕНА!</b>\n\n"
            f"Депеша полностью расшифрована:\n<pre>{html.escape(game.original_text)}</pre>\n\n"
            f"📊 <b>Итоги миссии:</b>\n"
            f"• Уникальных символов: {game.total_unique_letters}\n"
            f"• Подсказок Штаба: {game.hints_used}\n"
            f"• Ошибок Агента: {game.mistakes_count}\n\n"
            "Полевой Агент и Штабной Аналитик сработали как единое целое!"
        )

        analyst_text = (
            "🏆 <b>ОПЕРАЦИЯ УСПЕШНО ЗАВЕРШЕНА!</b>\n\n"
            f"Напарник успешно разгадал все шифры:\n<pre>{html.escape(game.original_text)}</pre>\n\n"
            f"📊 <b>Итоги миссии:</b>\n"
            f"• Уникальных символов: {game.total_unique_letters}\n"
            f"• Передано подсказок: {game.hints_used}\n"
            f"• Ошибок Агента: {game.mistakes_count}\n\n"
            "Штаб выражает благодарность за безупречную операцию!"
        )

        kb = build_victory_keyboard()

        if game.agent_chat_id and game.agent_message_id:
            await safe_edit_message(
                bot=self.bot,
                chat_id=game.agent_chat_id,
                message_id=game.agent_message_id,
                text=agent_text,
                reply_markup=kb,
            )

        if game.analyst_chat_id and game.analyst_message_id:
            await safe_edit_message(
                bot=self.bot,
                chat_id=game.analyst_chat_id,
                message_id=game.analyst_message_id,
                text=analyst_text,
                reply_markup=kb,
            )
