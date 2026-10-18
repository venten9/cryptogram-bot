"""Aiogram 3.x Middleware for database and session service dependency injection."""
from typing import Any, Awaitable, Callable, Dict, Optional

from aiogram import BaseMiddleware, Bot
from aiogram.types import TelegramObject

from bot.utils import NotifierService
from services.lock_manager import SessionLockManager
from services.session_service import SessionService


class DbSessionMiddleware(BaseMiddleware):
    """Middleware injecting SessionService, SessionLockManager, and NotifierService into handlers."""

    def __init__(
        self,
        session_service: Optional[SessionService] = None,
        lock_manager: Optional[SessionLockManager] = None,
        bot_username: str = "SpyAnalystBot",
    ) -> None:
        super().__init__()
        self.session_service = session_service
        self.lock_manager = lock_manager or (
            session_service.lock_manager if session_service else SessionLockManager()
        )
        self.bot_username = bot_username

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        if "session_service" not in data and self.session_service is not None:
            data["session_service"] = self.session_service

        if "lock_manager" not in data and self.lock_manager is not None:
            data["lock_manager"] = self.lock_manager

        if "bot_username" not in data:
            data["bot_username"] = self.bot_username

        if "notifier" not in data:
            bot = data.get("bot")
            serv = data.get("session_service", self.session_service)
            if bot and serv:
                data["notifier"] = NotifierService(bot=bot, session_service=serv)

        return await handler(event, data)


# Alias
DatabaseMiddleware = DbSessionMiddleware
