"""Per-Telegram-chat Cursor session state."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .acp import AcpClient
from .config import Config
from .logging_setup import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class ChatSession:
    chat_id: int
    workspace: Path
    mode: str = "agent"
    model: str = ""
    sandbox: str = ""
    yolo: bool = False
    acp: AcpClient | None = None
    session_id: str | None = None
    created_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)
    pending_prompt: asyncio.Task | None = None
    extra_state: dict[str, Any] = field(default_factory=dict)

    def touch(self) -> None:
        self.last_used_at = time.time()


class SessionManager:
    """Owns one ACP child process per chat (lazy)."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.sessions: dict[int, ChatSession] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    @staticmethod
    def _resolve_workspace_path(path: Path) -> Path:
        try:
            return path.expanduser().resolve(strict=False)
        except OSError:
            return path.expanduser()

    def remap_unsafe_workspace(self, chat_id: int, workspace: Path) -> Path:
        """Project roots / and /root often make Cursor session/new fail (-32603); use a normal folder."""

        p = self._resolve_workspace_path(workspace)
        if p in {Path("/"), Path("/root")}:
            alt = self.config.storage.workspaces_root / f"chat-{chat_id}"
            log.warning(
                "workspace.remapped_unsafe",
                chat_id=chat_id,
                configured=str(workspace),
                resolved=str(p),
                using=str(alt),
            )
            return alt
        return workspace

    def lock_for(self, chat_id: int) -> asyncio.Lock:
        lock = self._locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[chat_id] = lock
        return lock

    def get(self, chat_id: int) -> ChatSession | None:
        return self.sessions.get(chat_id)

    def get_or_create(self, chat_id: int) -> ChatSession:
        sess = self.sessions.get(chat_id)
        canonical = self._workspace_for(chat_id)
        if sess is None:
            sess = ChatSession(
                chat_id=chat_id,
                workspace=canonical,
                mode=self.config.cursor.default_mode,
                model=self.config.cursor.default_model,
                sandbox=self.config.cursor.sandbox,
                yolo=self.config.cursor.force_writes,
            )
            self.sessions[chat_id] = sess
            log.info("session.create", chat_id=chat_id, workspace=str(canonical))
        else:
            rp = self._resolve_workspace_path(sess.workspace)
            if rp in {Path("/"), Path("/root")} and sess.workspace != canonical:
                log.info(
                    "session.workspace_heal",
                    chat_id=chat_id,
                    old=str(sess.workspace),
                    new=str(canonical),
                )
                sess.workspace = canonical
        return sess

    async def close(self, chat_id: int) -> None:
        sess = self.sessions.pop(chat_id, None)
        if not sess:
            return
        if sess.pending_prompt and not sess.pending_prompt.done():
            sess.pending_prompt.cancel()
        if sess.acp:
            await sess.acp.stop()
        log.info("session.close", chat_id=chat_id)

    async def close_all(self) -> None:
        await asyncio.gather(*(self.close(cid) for cid in list(self.sessions)))

    def _workspace_for(self, chat_id: int) -> Path:
        if self.config.cursor.workspace:
            base = Path(self.config.cursor.workspace).expanduser()
        else:
            base = self.config.storage.workspaces_root / f"chat-{chat_id}"
        base = self.remap_unsafe_workspace(chat_id, base)
        base.mkdir(parents=True, exist_ok=True)
        return base
