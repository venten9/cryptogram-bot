"""Zero-network, in-memory Telegram Bot API mock session and client simulator for Aiogram 3.x."""
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Dict, List, Optional, Type, TypeVar

from aiogram import Bot, Dispatcher, types
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteMessage,
    EditMessageText,
    GetMe,
    SendChatAction,
    SendMessage,
    TelegramMethod,
)

T = TypeVar("T")


class MockTelegramSession(BaseSession):
    """Zero-network in-memory Telegram Bot API session for Aiogram 3.x.

    Intercepts all outgoing requests, records history, and maintains a virtual chat store.
    Correctly updates immutable Pydantic v2 Message models via model_copy.
    """

    def __init__(self, simulate_unmodified_error: bool = False):
        super().__init__()
        self.requests: List[TelegramMethod[Any]] = []
        self.messages: Dict[int, types.Message] = {}  # message_id -> Message
        self.callback_answers: List[AnswerCallbackQuery] = []
        self.simulate_unmodified_error: bool = simulate_unmodified_error
        self._msg_counter: int = 1000

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[T],
        timeout: Optional[int] = None,
    ) -> T:
        self.requests.append(method)
        now = datetime.now(timezone.utc)
        name = type(method).__name__

        if name == "SendMessage":
            assert isinstance(method, SendMessage)
            self._msg_counter += 1
            mid = self._msg_counter
            chat_id = (
                int(method.chat_id)
                if str(method.chat_id).lstrip("-").isdigit()
                else 1000
            )

            msg = types.Message(
                message_id=mid,
                date=now,
                chat=types.Chat(id=chat_id, type="private"),
                from_user=types.User(
                    id=bot.id,
                    is_bot=True,
                    first_name="CryptogramBot",
                    username="SpyAnalystBot",
                ),
                text=method.text,
                reply_markup=method.reply_markup,
            )
            self.messages[mid] = msg
            return msg  # type: ignore

        elif name == "EditMessageText":
            assert isinstance(method, EditMessageText)
            mid = method.message_id or 1000
            existing = self.messages.get(mid)
            chat_id = (
                int(method.chat_id)
                if (method.chat_id and str(method.chat_id).lstrip("-").isdigit())
                else 1000
            )

            if existing:
                # TelegramBadRequest simulation when text & markup are identical
                if (
                    self.simulate_unmodified_error
                    and existing.text == method.text
                    and existing.reply_markup == method.reply_markup
                ):
                    raise TelegramBadRequest(
                        method=method,
                        message="Bad Request: message is not modified: specified new message content and reply markup are exactly the same as a current content and reply markup of the message",
                    )

                # Pydantic v2 immutable update
                updated = existing.model_copy(
                    update={
                        "text": method.text,
                        "reply_markup": (
                            method.reply_markup
                            if method.reply_markup is not None
                            else existing.reply_markup
                        ),
                    }
                )
                self.messages[mid] = updated
                return updated  # type: ignore

            # Fallback if message not yet recorded
            msg = types.Message(
                message_id=mid,
                date=now,
                chat=types.Chat(id=chat_id, type="private"),
                from_user=types.User(
                    id=bot.id,
                    is_bot=True,
                    first_name="CryptogramBot",
                    username="SpyAnalystBot",
                ),
                text=method.text,
                reply_markup=method.reply_markup,
            )
            self.messages[mid] = msg
            return msg  # type: ignore

        elif name == "AnswerCallbackQuery":
            assert isinstance(method, AnswerCallbackQuery)
            self.callback_answers.append(method)
            return True  # type: ignore

        elif name == "SendChatAction":
            return True  # type: ignore

        elif name == "DeleteMessage":
            assert isinstance(method, DeleteMessage)
            self.messages.pop(method.message_id, None)
            return True  # type: ignore

        elif name == "GetMe":
            return types.User(  # type: ignore
                id=bot.id,
                is_bot=True,
                first_name="CryptogramBot",
                username="SpyAnalystBot",
                can_join_groups=False,
                can_read_all_group_messages=False,
                supports_inline_queries=False,
            )

        return True  # type: ignore

    async def stream_content(
        self,
        url: str,
        timeout: Optional[int] = None,
        chunk_size: int = 65536,
    ) -> AsyncGenerator[bytes, None]:
        yield b""

    async def close(self) -> None:
        pass

    # --- Query / Inspection Helpers for Test Assertions ---

    def get_requests(
        self, method_type: Optional[Type[TelegramMethod]] = None
    ) -> List[TelegramMethod]:
        if method_type is None:
            return list(self.requests)
        return [r for r in self.requests if isinstance(r, method_type)]

    def get_last_request(self) -> Optional[TelegramMethod]:
        return self.requests[-1] if self.requests else None

    def get_messages_for_chat(self, chat_id: int) -> List[types.Message]:
        return [m for m in self.messages.values() if m.chat.id == chat_id]

    def get_last_message_for_chat(self, chat_id: int) -> Optional[types.Message]:
        msgs = self.get_messages_for_chat(chat_id)
        return msgs[-1] if msgs else None

    def get_last_message_text(self, chat_id: int) -> Optional[str]:
        msg = self.get_last_message_for_chat(chat_id)
        return msg.text if msg else None

    def clear(self) -> None:
        self.requests.clear()
        self.messages.clear()
        self.callback_answers.clear()


class MockPlayerClient:
    """Simulates an asynchronous Telegram player sending updates to the bot."""

    def __init__(
        self,
        dp: Dispatcher,
        bot: Bot,
        user_id: int,
        username: str = "player",
        first_name: str = "Player",
    ):
        self.dp = dp
        self.bot = bot
        self.user = types.User(
            id=user_id, is_bot=False, first_name=first_name, username=username
        )
        self.chat = types.Chat(id=user_id, type="private")
        self._update_counter: int = user_id * 1000
        self._message_counter: int = user_id * 1000

    def _next_update_id(self) -> int:
        self._update_counter += 1
        return self._update_counter

    def _next_message_id(self) -> int:
        self._message_counter += 1
        return self._message_counter

    async def send_command(self, command: str, args: str = "", **kwargs) -> None:
        """Send a slash command (e.g. /start or /start game_abc12345)."""
        text = f"{command} {args}".strip()
        await self.send_message(text, **kwargs)

    async def send_message(self, text: str, **kwargs) -> None:
        """Send plain text from player to bot."""
        msg = types.Message(
            message_id=self._next_message_id(),
            date=datetime.now(timezone.utc),
            chat=self.chat,
            from_user=self.user,
            text=text,
        )
        update = types.Update(update_id=self._next_update_id(), message=msg)
        await self.dp.feed_update(self.bot, update, **kwargs)

    async def click_button(
        self,
        callback_data: str,
        message: Optional[types.Message] = None,
        **kwargs,
    ) -> None:
        """Simulate clicking an inline button with exact callback_data."""
        call = types.CallbackQuery(
            id=str(self._next_update_id()),
            from_user=self.user,
            chat_instance=f"chat_{self.user.id}",
            data=callback_data,
            message=message,
        )
        update = types.Update(update_id=self._next_update_id(), callback_query=call)
        await self.dp.feed_update(self.bot, update, **kwargs)

    async def click_button_by_text(
        self,
        button_text_substr: str,
        message: types.Message,
        **kwargs,
    ) -> str:
        """Find an inline button by matching text substring and simulate clicking it."""
        assert message.reply_markup and message.reply_markup.inline_keyboard, (
            f"Message {message.message_id} has no inline keyboard attached!"
        )
        target_data: Optional[str] = None
        for row in message.reply_markup.inline_keyboard:
            for btn in row:
                if button_text_substr.lower() in btn.text.lower():
                    target_data = btn.callback_data
                    break
            if target_data:
                break

        assert target_data is not None, (
            f"Button with text '{button_text_substr}' not found in keyboard of message {message.message_id}!"
        )
        await self.click_button(target_data, message=message, **kwargs)
        return target_data
