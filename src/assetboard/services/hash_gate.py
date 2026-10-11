"""Bounded concurrency for password hashing.

Argon2id with the library defaults needs about 64 MiB and noticeable CPU per call. The gate
runs at most ``max_concurrent`` hashes at once, each in a worker thread so the event loop (and
``/healthz``) stays responsive. Callers beyond that wait in a bounded queue; a full queue or a
long wait fails fast with ``HashGateBusy`` so the web layer can answer 503 with ``Retry-After``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import anyio.to_thread

WAITERS_PER_SLOT = 8
WAIT_TIMEOUT_SECONDS = 10.0
RETRY_AFTER_SECONDS = 5


class HashGateBusy(Exception):
    code = "auth_busy"

    def __init__(self) -> None:
        super().__init__("Too many sign-in requests are being processed; try again shortly")


class HashGate:
    def __init__(
        self,
        max_concurrent: int,
        *,
        max_waiting: int | None = None,
        wait_timeout: float = WAIT_TIMEOUT_SECONDS,
    ) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be at least 1")
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._max_waiting = (
            WAITERS_PER_SLOT * max_concurrent if max_waiting is None else max_waiting
        )
        self._wait_timeout = wait_timeout
        self._waiting = 0

    @property
    def waiting(self) -> int:
        return self._waiting

    async def run[T](self, operation: Callable[[], T]) -> T:
        """Run ``operation`` in a worker thread once a hashing slot is free."""
        if self._semaphore.locked() and self._waiting >= self._max_waiting:
            raise HashGateBusy
        self._waiting += 1
        try:
            async with asyncio.timeout(self._wait_timeout):
                await self._semaphore.acquire()
        except TimeoutError as exc:
            raise HashGateBusy from exc
        finally:
            self._waiting -= 1
        try:
            return await anyio.to_thread.run_sync(operation)
        finally:
            self._semaphore.release()
