"""Sends queued notifications from its own task.

Polls run under a timeout and hold a slot other targets need, and a push service can be slow,
so nothing is sent from inside one. The queue is memory-only: a retry survives a push service
outage, not a crash. A lost notification is a small price against keeping an outbox for a
window of seconds, and the dashboard still shows the problem.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session, sessionmaker

from mediahub.db.session import session_scope
from mediahub.logging import get_logger
from mediahub.push import repository
from mediahub.push.messages import PushMessage
from mediahub.push.webpush import Device, Outcome, Result, Transport

log = get_logger(__name__)

RETRY_DELAYS_S = (30.0, 120.0, 600.0, 1800.0)
CONCURRENCY = 4
DRAIN_S = 5.0


@dataclass(eq=False)
class _Queued:
    message: PushMessage
    device_ids: set[str] | None = None  # None: every device, read when first sent
    attempts: int = 0
    not_before: float = 0.0


class PushDispatcher:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        transport: Transport,
        public_key: str,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = sessions
        self.transport = transport
        self.public_key = public_key
        self._clock = clock
        self._wall = wall
        self._queue: dict[str, _Queued] = {}  # by tag: a newer message replaces a queued one
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    def enqueue(self, message: PushMessage) -> None:
        self._queue[message.tag] = _Queued(message)
        self._wake.set()

    @property
    def queued(self) -> list[PushMessage]:
        return [q.message for q in self._queue.values()]

    async def send_test(self, device: Device, message: PushMessage) -> Result:
        result = await self.transport.send(device, message)
        await asyncio.to_thread(self._record, {device.id: result})
        return result

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._task is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._task, DRAIN_S)
        await self.transport.aclose()

    async def _run(self) -> None:
        while True:
            now = self._clock()
            due = [q for q in self._queue.values() if q.not_before <= now]
            if due:
                for queued in due:
                    await self.deliver(queued)
                continue
            if self._stopping:
                return  # retries still waiting are given up on
            self._wake.clear()
            waits = [q.not_before - now for q in self._queue.values()]
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), min(waits, default=None))

    async def deliver(self, queued: _Queued) -> None:
        try:
            devices = await asyncio.to_thread(self._devices)
            targets = [d for d in devices if queued.device_ids is None or d.id in queued.device_ids]
            results = await self._send_all(targets, queued.message)
            await asyncio.to_thread(self._record, results)
        except Exception as exc:
            # Only the type: httpx and SQLAlchemy errors can quote the endpoint.
            log.error("push_delivery_failed", error=type(exc).__name__)
            self._settle(queued, queued.device_ids, None)
            return
        retry = {i for i, r in results.items() if r.outcome is Outcome.RETRY}
        waits = [r.retry_after_s for r in results.values() if r.retry_after_s is not None]
        self._settle(queued, retry, max(waits, default=None))

    def _settle(self, queued: _Queued, retry: set[str] | None, wait: float | None) -> None:
        """Take a sent message off the queue, unless some devices are owed another try."""
        tag = queued.message.tag
        if self._queue.get(tag) is not queued:
            return  # replaced while sending
        if retry == set() or queued.attempts >= len(RETRY_DELAYS_S):
            if retry:
                log.warning("push_gave_up", devices=len(retry))
            del self._queue[tag]
            return
        delay = RETRY_DELAYS_S[queued.attempts]
        queued.attempts += 1
        queued.device_ids = retry
        queued.not_before = self._clock() + max(delay, wait or 0.0)

    async def _send_all(self, devices: list[Device], message: PushMessage) -> dict[str, Result]:
        limit = asyncio.Semaphore(CONCURRENCY)

        async def one(device: Device) -> tuple[str, Result]:
            async with limit:
                result = await self.transport.send(device, message)
            if result.outcome is not Outcome.SENT:
                log.warning("push_not_delivered", host=device.host, error=result.error)
            return device.id, result

        return dict(await asyncio.gather(*(one(d) for d in devices)))

    def _devices(self) -> list[Device]:
        with session_scope(self._sessions) as db:
            return repository.devices(db)

    def _record(self, results: dict[str, Result]) -> None:
        with session_scope(self._sessions) as db:
            now = self._wall()
            for device_id, result in results.items():
                repository.record_result(db, device_id, result, now)
