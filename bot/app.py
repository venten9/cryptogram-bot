"""Application entrypoint and Dispatcher factory for Cooperative Cryptogram Bot."""
import asyncio
import logging
from typing import Optional

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage

from bot.handlers import setup_routers
from bot.middlewares.db_middleware import DbSessionMiddleware
from config import get_settings
from database.session import close_db, create_app_engine, create_session_factory, init_db
from services.lock_manager import SessionLockManager
from services.session_service import SessionService

logger = logging.getLogger(__name__)


def create_dispatcher(
    session_service: Optional[SessionService] = None,
    lock_manager: Optional[SessionLockManager] = None,
    bot_username: Optional[str] = None,
    storage: Optional[BaseStorage] = None,
) -> Dispatcher:
    """Build root Aiogram 3.x Dispatcher with mounted routers and registered middlewares."""
    if bot_username is None:
        try:
            bot_username = get_settings().effective_bot_username
        except Exception:
            bot_username = "SpyAnalystBot"

    dp = Dispatcher(storage=storage or MemoryStorage())

    # Mount assembled router tree
    dp.include_router(setup_routers())

    # Register session and lock middlewares
    middleware = DbSessionMiddleware(
        session_service=session_service,
        lock_manager=lock_manager,
        bot_username=bot_username,
    )
    dp.message.middleware(middleware)
    dp.callback_query.middleware(middleware)

    # Set workflow data accessible across handlers
    dp["session_service"] = session_service
    dp["lock_manager"] = lock_manager or (
        session_service.lock_manager if session_service else SessionLockManager()
    )
    dp["bot_username"] = bot_username

    return dp


async def main() -> None:
    """Production entrypoint to launch Telegram bot polling."""
    settings = get_settings()
    settings.validate_for_bot_polling()

    logging.basicConfig(
        level=logging.DEBUG if settings.DEBUG else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger.info("Initializing bot with username @%s...", settings.effective_bot_username)

    engine = create_app_engine(db_path=settings.DB_PATH, echo=settings.DEBUG)
    await init_db(engine)

    session_factory = create_session_factory(engine)
    lock_manager = SessionLockManager()
    session_service = SessionService(session_factory=session_factory, lock_manager=lock_manager)

    bot = Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = create_dispatcher(
        session_service=session_service,
        lock_manager=lock_manager,
        bot_username=settings.effective_bot_username,
    )

    try:
        logger.info("Bot polling started. Press Ctrl+C to terminate.")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
        await close_db(engine)
        logger.info("Bot shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())
