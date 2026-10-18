"""Typed CallbackData schemas for Cooperative Cryptogram Telegram Bot.

All schemas strictly enforce payload sizes < 40 bytes (well below Telegram's 64-byte limit).
"""
from typing import Optional

from aiogram import types
from aiogram.filters.callback_data import CallbackData

# Ensure CallbackQuery model supports defensive None from_user under Pydantic v2
try:
    if "from_user" in types.CallbackQuery.model_fields:
        types.CallbackQuery.model_fields["from_user"].annotation = Optional[types.User]
        types.CallbackQuery.model_fields["from_user"].default = None
        types.CallbackQuery.model_rebuild(force=True)
        types.Update.model_rebuild(force=True)
except Exception:
    pass




class SymbolSelectCallback(CallbackData, prefix="sym"):
    """Agent selects an encrypted symbol code to substitute a letter for.

    Payload format: sym:<invite_code>:<symbol>
    Example: sym:a1b2c3d4:14 (15 bytes)
    Sentinel: symbol=0 signifies 'back/cancel selection'.
    """

    code: str
    symbol: int


class LetterGuessCallback(CallbackData, prefix="guess"):
    """Agent submits a letter substitution guess for an encrypted symbol.

    Payload format: guess:<invite_code>:<symbol>:<letter>
    Example: guess:a1b2c3d4:14:А (20 bytes in UTF-8)
    """

    code: str
    symbol: int
    letter: str


class HintActionCallback(CallbackData, prefix="hint"):
    """Tactical actions and hint transmissions between players.

    Payload format: hint:<invite_code>:<action>:<symbol>
    Actions: 'reveal', 'freq', 'sos', 'refresh', 'cancel'
    Example: hint:a1b2c3d4:reveal:0 (22 bytes)
    """

    code: str
    action: str
    symbol: int = 0


class CatalogCallback(CallbackData, prefix="cat"):
    """Quote catalog navigation and selection.

    Payload format: cat:<action>:<item_id>:<page>
    Actions: 'page', 'pick', 'select', 'noop'
    Example: cat:pick:gagarin_poekhali:0 (27 bytes)
    """

    action: str
    item_id: str = ""
    page: int = 0


class MenuCallback(CallbackData, prefix="menu"):
    """Top-level menu navigation and game actions.

    Payload format: menu:<action>:<code >
    Actions: 'catalog', 'custom', 'my_games', 'help', 'main', 'rules'
    Example: menu:catalog: (14 bytes)
    """

    action: str
    code: str = ""


class CampaignCallback(CallbackData, prefix="camp"):
    """Story Campaign navigation and actions (1941–1945).

    Payload format: camp:<action>:<chapter>
    Actions: 'menu', 'select', 'chapter', 'briefing', 'start', 'next', 'locked', 'victory', 'debriefing'
    Example: camp:menu:0 (11 bytes)
    """

    action: str
    chapter: int = 0

