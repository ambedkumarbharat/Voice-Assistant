"""In-process live call registry and event fan-out."""

from __future__ import annotations

import asyncio
from typing import Any


class CallHub:
    def __init__(self) -> None:
        self._calls: dict[str, Any] = {}
        self._call_queues: dict[str, set[asyncio.Queue]] = {}
        self._global_queues: set[asyncio.Queue] = set()

    def add(self, session: Any) -> None:
        self._calls[session.id] = session

    def remove(self, call_id: str) -> None:
        self._calls.pop(call_id, None)
        self._call_queues.pop(call_id, None)

    def get(self, call_id: str) -> Any | None:
        return self._calls.get(call_id)

    def has(self, call_id: str) -> bool:
        return call_id in self._calls

    def list(self) -> list[dict[str, Any]]:
        return [session.snapshot() for session in list(self._calls.values())]

    def subscribe(self, call_id: str | None, queue: asyncio.Queue) -> None:
        if call_id is None:
            self._global_queues.add(queue)
        else:
            self._call_queues.setdefault(call_id, set()).add(queue)

    def unsubscribe(self, call_id: str | None, queue: asyncio.Queue) -> None:
        if call_id is None:
            self._global_queues.discard(queue)
        else:
            queues = self._call_queues.get(call_id)
            if queues:
                queues.discard(queue)
                if not queues:
                    self._call_queues.pop(call_id, None)

    def publish(self, event: dict[str, Any]) -> None:
        queues: list[asyncio.Queue] = list(self._global_queues)
        queues.extend(self._call_queues.get(event.get("callId", ""), set()))
        for queue in queues:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass
