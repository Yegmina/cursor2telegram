"""Cursor Agent Client Protocol (ACP) client over stdio.

This is a minimal but complete async JSON-RPC 2.0 client tailored to talk
to ``agent acp``. It exposes:

* request/response with arbitrary method names (``await client.request("session/new", {...})``).
* notifications (``await client.notify("session/cancel", {...})``).
* an event subscription model that fans out incoming notifications to handlers
  (``client.on("session/update", handler)``).
* graceful start/stop with stderr capture.

Cursor's ACP is JSON-RPC 2.0 over stdio with newline-delimited JSON framing.
See https://agentclientprotocol.com and Cursor's CLI ACP documentation.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import os
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from .logging_setup import get_logger

log = get_logger(__name__)


# Convenient outcome IDs returned by the spec.
PERMISSION_ALLOW_ONCE = "allow-once"
PERMISSION_ALLOW_ALWAYS = "allow-always"
PERMISSION_REJECT_ONCE = "reject-once"


class AcpError(RuntimeError):
    """Raised when the agent reports a JSON-RPC error or the process dies."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"acp error {code}: {message}")
        self.code = code
        self.message = message
        self.data = data


@dataclasses.dataclass(slots=True)
class _Pending:
    future: asyncio.Future[Any]
    method: str


NotificationHandler = Callable[[str, dict[str, Any]], Awaitable[Any] | Any]
RequestHandler = Callable[[str, dict[str, Any]], Awaitable[Any]]


class AcpClient:
    """Async wrapper around an ``agent acp`` subprocess."""

    def __init__(
        self,
        command: list[str],
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> None:
        self.command = list(command)
        self.env = {**os.environ, **(env or {})}
        self.cwd = cwd
        self._process: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._pending: dict[int, _Pending] = {}
        self._notif_handlers: dict[str, list[NotificationHandler]] = {}
        self._req_handlers: dict[str, RequestHandler] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self._closed = asyncio.Event()
        self._exit_code: int | None = None
        self._stderr_recent: deque[str] = deque(maxlen=48)

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    @property
    def exit_code(self) -> int | None:
        return self._exit_code

    def recent_stderr_tail(self, max_chars: int = 900) -> str:
        """Last lines from the agent subprocess (for user-facing diagnostics)."""

        lines = list(self._stderr_recent)
        if not lines:
            return ""
        text = "\n".join(lines).strip()
        if len(text) <= max_chars:
            return text
        return text[-max_chars:].lstrip()

    async def start(self) -> None:
        if self._process:
            return
        log.info("acp.start", command=self.command, cwd=self.cwd)
        self._process = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.env,
            cwd=self.cwd,
        )
        self._reader_task = asyncio.create_task(self._read_loop(), name="acp-reader")
        self._stderr_task = asyncio.create_task(self._stderr_loop(), name="acp-stderr")

    async def stop(self) -> None:
        if not self._process:
            return
        proc = self._process
        log.info("acp.stop")
        try:
            if proc.stdin and not proc.stdin.is_closing():
                proc.stdin.close()
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
        self._exit_code = proc.returncode
        self._process = None
        self._closed.set()
        for task in (self._reader_task, self._stderr_task):
            if task and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    async def __aenter__(self) -> AcpClient:
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.stop()

    def on(self, method: str, handler: NotificationHandler) -> None:
        """Register a notification handler. Multiple handlers per method are allowed."""
        self._notif_handlers.setdefault(method, []).append(handler)

    def on_request(self, method: str, handler: RequestHandler) -> None:
        """Register a server-to-client request handler. Only one per method."""
        self._req_handlers[method] = handler

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        if not self.running:
            raise AcpError(-32000, "acp client is not running")
        msg_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[Any] = asyncio.get_event_loop().create_future()
        self._pending[msg_id] = _Pending(future=future, method=method)
        try:
            await self._send(
                {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}}
            )
            if timeout is not None:
                return await asyncio.wait_for(future, timeout=timeout)
            return await future
        finally:
            self._pending.pop(msg_id, None)

    async def respond(self, msg_id: Any, result: Any) -> None:
        await self._send({"jsonrpc": "2.0", "id": msg_id, "result": result})

    async def respond_error(self, msg_id: Any, code: int, message: str, data: Any = None) -> None:
        err: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        await self._send({"jsonrpc": "2.0", "id": msg_id, "error": err})

    async def _send(self, payload: dict[str, Any]) -> None:
        proc = self._process
        if not proc or not proc.stdin:
            raise AcpError(-32000, "acp client has no stdin")
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        async with self._write_lock:
            proc.stdin.write(line.encode("utf-8"))
            await proc.stdin.drain()

    async def _read_loop(self) -> None:
        assert self._process and self._process.stdout
        stdout = self._process.stdout
        try:
            while True:
                raw = await stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("acp.read.bad_json", line=line[:200])
                    continue
                await self._dispatch(msg)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("acp.read.error")
        finally:
            for pending in list(self._pending.values()):
                if not pending.future.done():
                    pending.future.set_exception(AcpError(-32000, "acp connection closed"))
            self._pending.clear()

    async def _stderr_loop(self) -> None:
        assert self._process and self._process.stderr
        stderr = self._process.stderr
        try:
            while True:
                raw = await stderr.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip()
                if line:
                    self._stderr_recent.append(line)
                    log.debug("acp.stderr", line=line)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("acp.stderr.error")

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        if "id" in msg and ("result" in msg or "error" in msg):
            pending = self._pending.pop(msg["id"], None)
            if not pending:
                log.warning("acp.dispatch.unknown_id", id=msg.get("id"))
                return
            if "error" in msg:
                err = msg["error"] or {}
                pending.future.set_exception(
                    AcpError(int(err.get("code", -1)), str(err.get("message", "")), err.get("data"))
                )
            else:
                pending.future.set_result(msg.get("result"))
            return

        method = msg.get("method")
        params = msg.get("params") or {}

        if "id" in msg and method:
            await self._handle_server_request(msg["id"], str(method), params)
            return

        if method:
            handlers = self._notif_handlers.get(str(method), [])
            wildcard = self._notif_handlers.get("*", [])
            for handler in (*handlers, *wildcard):
                try:
                    res = handler(str(method), params)
                    if asyncio.iscoroutine(res):
                        await res
                except Exception:  # noqa: BLE001
                    log.exception("acp.notif.handler_error", method=method)

    async def _handle_server_request(self, msg_id: Any, method: str, params: dict[str, Any]) -> None:
        handler = self._req_handlers.get(method)
        if handler is None:
            log.info("acp.request.no_handler", method=method)
            await self.respond_error(msg_id, -32601, f"no handler for {method}")
            return
        try:
            result = await handler(method, params)
            await self.respond(msg_id, result)
        except AcpError as exc:
            await self.respond_error(msg_id, exc.code, exc.message, exc.data)
        except Exception as exc:  # noqa: BLE001
            log.exception("acp.request.handler_error", method=method)
            await self.respond_error(msg_id, -32000, str(exc))


# ---------------------------------------------------------------------------
# Convenience helpers built on top of AcpClient


async def initialize(client: AcpClient, *, client_name: str, client_version: str) -> dict[str, Any]:
    return await client.request(
        "initialize",
        {
            "protocolVersion": 1,
            "clientCapabilities": {
                "fs": {"readTextFile": False, "writeTextFile": False},
                "terminal": False,
            },
            "clientInfo": {"name": client_name, "version": client_version},
        },
    )


async def authenticate_cursor_login(client: AcpClient) -> dict[str, Any]:
    return await client.request("authenticate", {"methodId": "cursor_login"})


async def session_new(
    client: AcpClient,
    *,
    cwd: str,
    mcp_servers: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {"cwd": cwd}
    if mcp_servers is not None:
        params["mcpServers"] = mcp_servers
    return await client.request("session/new", params)


async def session_load(client: AcpClient, *, session_id: str, cwd: str) -> dict[str, Any]:
    return await client.request("session/load", {"sessionId": session_id, "cwd": cwd})


async def session_prompt(
    client: AcpClient,
    *,
    session_id: str,
    text: str,
) -> dict[str, Any]:
    return await client.request(
        "session/prompt",
        {"sessionId": session_id, "prompt": [{"type": "text", "text": text}]},
    )


async def session_cancel(client: AcpClient, *, session_id: str) -> None:
    await client.notify("session/cancel", {"sessionId": session_id})
