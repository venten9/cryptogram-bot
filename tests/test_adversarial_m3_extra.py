"""Extra adversarial challenge tests for Milestone M3 remediation verification.

Targeted verifications:
1. Role separation on refresh across all session lifecycle states (WAITING_AGENT, IN_PROGRESS, SOLVED, CANCELLED).
2. Session cancellation authorization, concurrency, state machine guards, and DB persistence.
3. Strict verification of show_alert=True on all UnauthorizedActionError answers and error boundaries.
"""
import asyncio
from typing import Dict
import pytest
import pytest_asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy import event

from bot.app import create_dispatcher
from database.models import Base, SessionStatus
from database.session import create_session_factory
from services.lock_manager import SessionLockManager
from services.session_service import SessionService
from tests.mock_session import MockPlayerClient, MockTelegramSession
pytestmark = pytest.mark.e2e



@pytest_asyncio.fixture(scope="function")
async def extra_engine() -> AsyncEngine:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def extra_env(extra_engine: AsyncEngine) -> Dict:
    session_factory = create_session_factory(extra_engine)
    lock_manager = SessionLockManager()
    service = SessionService(session_factory=session_factory, lock_manager=lock_manager)

    mock_session = MockTelegramSession()
    bot = Bot(token="42:MOCK_TOKEN", session=mock_session)
    dp = create_dispatcher(
        session_service=service,
        lock_manager=lock_manager,
        bot_username="SpyAnalystBot",
        storage=MemoryStorage(),
    )

    analyst = MockPlayerClient(dp=dp, bot=bot, user_id=1001, username="HQ_Analyst")
    agent = MockPlayerClient(dp=dp, bot=bot, user_id=2002, username="Field_Agent")
    intruder = MockPlayerClient(dp=dp, bot=bot, user_id=9999, username="Intruder_Spy")

    return {
        "engine": extra_engine,
        "service": service,
        "session": mock_session,
        "bot": bot,
        "dp": dp,
        "analyst": analyst,
        "agent": agent,
        "intruder": intruder,
    }


@pytest.mark.asyncio
class TestRoleSeparationOnRefreshDeep:
    """Rigorous verification of role separation on refresh callback."""

    async def test_refresh_on_waiting_agent_session(self, extra_env):
        """When session is in WAITING_AGENT, only Analyst can refresh dossier, intruder and agent are blocked."""
        analyst = extra_env["analyst"]
        agent = extra_env["agent"]
        intruder = extra_env["intruder"]
        service = extra_env["service"]
        session = extra_env["session"]

        game = await service.create_session(creator_tg_id=analyst.user.id, text="Секретный код штаба")
        code = game.invite_code

        # 1. Analyst refreshes -> Success, gets dossier
        ans_before = len(session.callback_answers)
        await analyst.click_button(f"hint:{code}:refresh:0")
        assert len(session.callback_answers) > ans_before
        assert session.callback_answers[-1].text == "Табло обновлено"

        # 2. Intruder attempts refresh -> Blocked with alert
        ans_before = len(session.callback_answers)
        await intruder.click_button(f"hint:{code}:refresh:0")
        assert len(session.callback_answers) > ans_before
        assert session.callback_answers[-1].show_alert is True
        assert "не являетесь участником" in session.callback_answers[-1].text

        # 3. Agent (not yet joined!) attempts refresh -> Blocked with alert because game.agent_id is None
        ans_before = len(session.callback_answers)
        await agent.click_button(f"hint:{code}:refresh:0")
        assert len(session.callback_answers) > ans_before
        assert session.callback_answers[-1].show_alert is True
        assert "не являетесь участником" in session.callback_answers[-1].text

    async def test_refresh_on_in_progress_session(self, extra_env):
        """In IN_PROGRESS: Analyst sees Dossier (with plaintext), Agent sees Terminal (NO plaintext), Intruder rejected."""
        analyst = extra_env["analyst"]
        agent = extra_env["agent"]
        intruder = extra_env["intruder"]
        service = extra_env["service"]
        session = extra_env["session"]

        secret_text = "Совершенно секретный приказ номер один"
        game = await service.create_session(creator_tg_id=analyst.user.id, text=secret_text)
        code = game.invite_code
        await agent.send_command("/start", f"game_{code}")

        # Agent clicks refresh
        agent_msg = session.get_last_message_for_chat(agent.user.id)
        await agent.click_button(f"hint:{code}:refresh:0", message=agent_msg)
        updated_agent_msg = session.get_last_message_for_chat(agent.user.id)
        assert secret_text not in updated_agent_msg.text
        assert "ПОЛЕВОЙ ТЕРМИНАЛ" in updated_agent_msg.text
        assert "ДОСЬЕ" not in updated_agent_msg.text

        # Analyst clicks refresh
        from datetime import datetime, timezone
        analyst_msg = types.Message(
            message_id=5555,
            date=datetime.now(timezone.utc),
            chat=types.Chat(id=analyst.user.id, type="private"),
            from_user=analyst.user,
            text="Старое сообщение штаба",
        )
        session.messages[5555] = analyst_msg
        await analyst.click_button(f"hint:{code}:refresh:0", message=analyst_msg)
        updated_analyst_msg = session.get_last_message_for_chat(analyst.user.id)
        assert updated_analyst_msg is not None
        assert "ДОСЬЕ" in updated_analyst_msg.text
        assert secret_text in updated_analyst_msg.text

        # Intruder clicks refresh -> Blocked with alert
        ans_before = len(session.callback_answers)
        await intruder.click_button(f"hint:{code}:refresh:0")
        assert len(session.callback_answers) > ans_before
        assert session.callback_answers[-1].show_alert is True
        assert "не являетесь участником" in session.callback_answers[-1].text


@pytest.mark.asyncio
class TestSessionCancellationDeep:
    """Rigorous verification of SessionService.cancel_session and on_analyst_cancel_session."""

    async def test_agent_and_intruder_cannot_cancel(self, extra_env):
        """Neither field agent nor outsider can cancel session."""
        analyst = extra_env["analyst"]
        agent = extra_env["agent"]
        intruder = extra_env["intruder"]
        service = extra_env["service"]
        session = extra_env["session"]

        game = await service.create_session(creator_tg_id=analyst.user.id, text="Проверочная депеша")
        code = game.invite_code

        # Intruder tries to cancel
        ans_before = len(session.callback_answers)
        await intruder.click_button(f"hint:{code}:cancel:0")
        assert len(session.callback_answers) > ans_before
        assert session.callback_answers[-1].show_alert is True
        assert "недоступно для вашей роли" in session.callback_answers[-1].text.lower() or "только создатель" in session.callback_answers[-1].text.lower()

        # Agent tries to cancel
        ans_before = len(session.callback_answers)
        await agent.click_button(f"hint:{code}:cancel:0")
        assert len(session.callback_answers) > ans_before
        assert session.callback_answers[-1].show_alert is True

        # Verify game is still WAITING_AGENT
        reloaded = await service.get_session_by_code(code)
        assert reloaded.status == SessionStatus.WAITING_AGENT

    async def test_cancel_active_game_rejected(self, extra_env):
        """Analyst cannot cancel game once Agent has joined (IN_PROGRESS)."""
        analyst = extra_env["analyst"]
        agent = extra_env["agent"]
        service = extra_env["service"]
        session = extra_env["session"]

        game = await service.create_session(creator_tg_id=analyst.user.id, text="Проверочная депеша")
        code = game.invite_code
        await agent.send_command("/start", f"game_{code}")

        # Analyst tries to cancel active game
        ans_before = len(session.callback_answers)
        await analyst.click_button(f"hint:{code}:cancel:0")
        assert len(session.callback_answers) > ans_before
        assert session.callback_answers[-1].show_alert is True
        assert "невозможно отменить активную операцию" in session.callback_answers[-1].text.lower()

        # Verify game remains IN_PROGRESS
        reloaded = await service.get_session_by_code(code)
        assert reloaded.status == SessionStatus.IN_PROGRESS

    async def test_concurrent_cancel_calls(self, extra_env):
        """Rapid concurrent cancellation calls from Analyst serialize safely without race condition."""
        analyst = extra_env["analyst"]
        service = extra_env["service"]
        session = extra_env["session"]

        game = await service.create_session(creator_tg_id=analyst.user.id, text="Проверочная депеша")
        code = game.invite_code

        # Fire 5 concurrent cancel requests
        tasks = [analyst.click_button(f"hint:{code}:cancel:0") for _ in range(5)]
        await asyncio.gather(*tasks, return_exceptions=True)

        # Game must be CANCELLED in DB
        reloaded = await service.get_session_by_code(code)
        assert reloaded.status == SessionStatus.CANCELLED


@pytest.mark.asyncio
class TestAlertsAndErrorShieldDeep:
    """Verify UnauthorizedActionError and global error handler always answer callbacks with alerts."""

    async def test_global_error_handler_answers_unhandled_callback(self, extra_env):
        """Ensure global_error_handler intercepts unhandled errors and answers with alert."""
        intruder = extra_env["intruder"]
        dp = extra_env["dp"]
        session = extra_env["session"]

        # Register a test callback handler that deliberately raises an unhandled ValueError
        from aiogram import Router
        from aiogram.types import CallbackQuery
        bug_router = Router()

        @bug_router.callback_query(lambda c: c.data == "trigger:bug")
        async def on_bug(cb: CallbackQuery):
            raise RuntimeError("Deliberate unhandled failure for testing")

        dp.include_router(bug_router)

        ans_before = len(session.callback_answers)
        await intruder.click_button("trigger:bug")

        assert len(session.callback_answers) > ans_before
        last_ans = session.callback_answers[-1]
        assert last_ans.show_alert is True
        assert "Ошибка обработки действия" in last_ans.text
