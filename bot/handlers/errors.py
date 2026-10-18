"""Global error handler catching TelegramBadRequest and unexpected exceptions."""
import logging

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import ErrorEvent

logger = logging.getLogger(__name__)

errors_router = Router(name="errors")


@errors_router.error()
async def global_error_handler(event: ErrorEvent) -> bool:
    """Intercept and gracefully handle unhandled exceptions across the bot."""
    exc = event.exception

    if isinstance(exc, TelegramBadRequest):
        msg = str(exc).lower()
        if "message is not modified" in msg:
            # Idempotent edit attempt, safely ignore
            return True
        if "message to edit not found" in msg:
            logger.warning("Message to edit not found in update %s", event.update)
            return True

    if isinstance(exc, TelegramForbiddenError):
        logger.warning(
            "TelegramForbiddenError: Bot was blocked by user in update %s",
            event.update,
        )
        return True

    logger.exception("Unhandled error processing update %s: %s", event.update, exc)

    if event.update and event.update.callback_query:
        try:
            await event.update.callback_query.answer(
                "⚠️ Ошибка обработки действия", show_alert=True
            )
        except Exception:
            pass

    return False
