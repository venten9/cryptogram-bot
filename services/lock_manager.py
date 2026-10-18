"""In-memory asynchronous mutex registry for per-session concurrency control."""
import asyncio
from contextlib import asynccontextmanager
from typing import Dict, Optional, Union


class _LockEntry:
    """Internal container for an asyncio.Lock and reference counter."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.ref_count: int = 0


class SessionLockManager:
    """Registry of per-session asyncio mutex locks.

    Prevents race conditions between concurrent player actions on the same game session.
    Features reference counting to automatically prune lock entries when all waiting
    coroutines complete, preventing memory leaks in long-running processes.
    """

    def __init__(self) -> None:
        self._locks: Dict[str, _LockEntry] = {}

    @asynccontextmanager
    async def lock(self, session_id: Union[str, int], timeout: Optional[float] = None):
        """Acquire per-session async mutex as an asynchronous context manager.

        Args:
            session_id: Unique identifier for the game session.
            timeout: Optional timeout in seconds to acquire the lock.

        Raises:
            TimeoutError: If timeout is exceeded before lock acquisition.
        """
        key = str(session_id)
        if key not in self._locks:
            self._locks[key] = _LockEntry()
        entry = self._locks[key]
        entry.ref_count += 1
        try:
            if timeout is not None:
                async with asyncio.timeout(timeout):
                    async with entry.lock:
                        yield
            else:
                async with entry.lock:
                    yield
        finally:
            entry.ref_count -= 1
            if entry.ref_count <= 0 and key in self._locks:
                if self._locks[key] is entry:
                    del self._locks[key]

    acquire = lock  # Ergonomic alias

    def is_locked(self, session_id: Union[str, int]) -> bool:
        """Check if a session lock is currently held by any coroutine."""
        key = str(session_id)
        entry = self._locks.get(key)
        return entry is not None and entry.lock.locked()

    def get_active_count(self) -> int:
        """Return the count of currently allocated session locks."""
        return len(self._locks)

    def reset(self) -> None:
        """Clear all active locks. Intended for test suite teardown."""
        self._locks.clear()
