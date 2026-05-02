"""Convert streaming ACP ``session/update`` notifications into Telegram-friendly events.

Cursor sends many small chunks while a model is generating. Telegram has rate
limits and a 4096-character message cap, so we buffer text deltas, throttle
edits, and emit higher-level events for tool calls / plans / questions.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal

EventKind = Literal[
    "text_chunk",
    "text_block_end",
    "tool_call_start",
    "tool_call_update",
    "tool_call_end",
    "todos_update",
    "plan",
    "question",
    "permission",
    "task",
    "image",
    "stop",
    "raw",
]


@dataclass(slots=True)
class StreamEvent:
    kind: EventKind
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return str(self.data.get("text", ""))


class StreamAggregator:
    """Receives ACP ``session/update`` payloads and yields high-level events.

    Use :meth:`feed` from your notification handler and iterate via :meth:`events`.
    The aggregator is single-producer, single-consumer per session.
    """

    def __init__(self, *, throttle_ms: int = 600) -> None:
        self.throttle_s = throttle_ms / 1000.0
        self._queue: asyncio.Queue[StreamEvent | None] = asyncio.Queue()
        self._closed = False

    async def feed(self, params: dict[str, Any]) -> None:
        update = params.get("update") or {}
        kind = update.get("sessionUpdate")
        if not kind:
            return
        events = list(self._translate(kind, update))
        for ev in events:
            await self._queue.put(ev)

    async def feed_event(self, event: StreamEvent) -> None:
        await self._queue.put(event)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._queue.put(None)

    async def events(self) -> AsyncIterator[StreamEvent]:
        while True:
            ev = await self._queue.get()
            if ev is None:
                return
            yield ev

    def _translate(self, kind: str, update: dict[str, Any]):
        # ACP session updates we know about.
        if kind == "agent_message_chunk":
            content = update.get("content") or {}
            text = content.get("text", "")
            if text:
                yield StreamEvent("text_chunk", {"text": text})
        elif kind == "agent_message_end":
            yield StreamEvent("text_block_end", {})
        elif kind == "tool_call":
            yield StreamEvent(
                "tool_call_start",
                {
                    "id": update.get("toolCallId"),
                    "name": update.get("name")
                    or update.get("toolName")
                    or update.get("title")
                    or update.get("kind"),
                    "args": update.get("args") or update.get("rawInput") or {},
                    "status": update.get("status"),
                    "raw": update,
                },
            )
        elif kind == "tool_call_update":
            if update.get("rawOutput") or update.get("status") in {"completed", "failed", "error"}:
                yield StreamEvent(
                    "tool_call_end",
                    {
                        "id": update.get("toolCallId"),
                        "result": update.get("rawOutput") or update.get("result"),
                        "error": update.get("error"),
                        "status": update.get("status"),
                        "raw": update,
                    },
                )
                return
            yield StreamEvent(
                "tool_call_update",
                {
                    "id": update.get("toolCallId"),
                    "delta": update.get("delta") or {},
                    "raw": update,
                },
            )
        elif kind == "tool_call_end":
            yield StreamEvent(
                "tool_call_end",
                {
                    "id": update.get("toolCallId"),
                    "result": update.get("result"),
                    "error": update.get("error"),
                    "raw": update,
                },
            )
        elif kind in ("plan", "create_plan"):
            yield StreamEvent("plan", {"raw": update})
        elif kind in ("todos", "update_todos"):
            yield StreamEvent("todos_update", {"raw": update})
        elif kind == "stop":
            yield StreamEvent(
                "stop",
                {"reason": update.get("stopReason") or update.get("reason"), "raw": update},
            )
        else:
            yield StreamEvent("raw", {"kind": kind, "raw": update})


@dataclass(slots=True)
class TextBuffer:
    """Accumulates text chunks and reports when it's time to flush."""

    throttle_s: float
    last_flush: float = 0.0
    buffer: str = ""

    def append(self, chunk: str) -> None:
        self.buffer += chunk

    def should_flush(self) -> bool:
        return bool(self.buffer) and (time.monotonic() - self.last_flush) >= self.throttle_s

    def flush(self) -> str:
        out = self.buffer
        self.buffer = ""
        self.last_flush = time.monotonic()
        return out

    def force_flush(self) -> str:
        return self.flush()
