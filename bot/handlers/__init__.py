"""Bot router tree assembly and Dispatcher integration."""
from aiogram import Router

from bot.handlers.agent import agent_router
from bot.handlers.analyst import analyst_router
from bot.handlers.common import common_router
from bot.handlers.errors import errors_router


def setup_routers() -> Router:
    """Assemble root router tree with error shielding and isolated domain sub-routers."""
    root_router = Router(name="root_handlers")

    for r in (errors_router, common_router, analyst_router, agent_router):
        r._parent_router = None

    # 1. Error shield first
    root_router.include_router(errors_router)

    # 2. General entry points & deep link router
    root_router.include_router(common_router)

    # 3. Role-specific routers
    root_router.include_router(analyst_router)
    root_router.include_router(agent_router)

    return root_router


__all__ = [
    "setup_routers",
    "common_router",
    "analyst_router",
    "agent_router",
    "errors_router",
]
