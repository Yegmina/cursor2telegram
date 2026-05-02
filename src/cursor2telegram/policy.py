"""Access control + per-action confirmation policy."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .config import Config

PermissionDecision = Literal["allow-once", "allow-always", "reject-once", "prompt"]


@dataclass(frozen=True, slots=True)
class AccessCheck:
    allowed: bool
    reason: str = ""


class Policy:
    """Centralizes "who can do what" decisions for the bot."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def check_user(self, user_id: int | None, chat_id: int | None) -> AccessCheck:
        cfg = self.config.telegram
        if not cfg.allowed_user_ids and not cfg.allowed_chat_ids:
            return AccessCheck(False, "no allowlist configured; refuse by default")
        if user_id is not None and user_id in cfg.allowed_user_ids:
            return AccessCheck(True)
        if chat_id is not None and chat_id in cfg.allowed_chat_ids:
            return AccessCheck(True)
        return AccessCheck(False, "user/chat not in allowlist")

    def is_owner(self, user_id: int | None) -> bool:
        owner = self.config.telegram.owner_user_id
        if owner is None:
            allowed = self.config.telegram.allowed_user_ids
            if not allowed:
                return False
            owner = allowed[0]
        return user_id is not None and user_id == owner

    def default_permission_decision(self) -> PermissionDecision:
        decision = self.config.policy.auto_approve_permissions
        if decision in ("allow-once", "allow-always", "reject-once", "prompt"):
            return decision  # type: ignore[return-value]
        return "prompt"

    def should_confirm_tool(self, category: str) -> bool:
        return category in self.config.policy.require_confirmation_for

    def categorize_tool(self, name: str, args: dict | None = None) -> str:
        n = (name or "").lower()
        if any(k in n for k in ("shell", "run", "exec", "terminal", "bash")):
            return "shell"
        if any(k in n for k in ("delete", "rm", "remove")):
            return "delete"
        if any(k in n for k in ("write", "edit", "patch", "create_file", "writefile")):
            return "write"
        if any(k in n for k in ("fetch", "http", "web_", "curl", "download")):
            return "network"
        if "mcp" in n or n.startswith("mcp_") or n.startswith("call_"):
            return "mcp"
        if any(k in n for k in ("read", "open", "list", "glob", "grep", "search")):
            return "read"
        return "other"

    def workspace_contains(self, path: str | Path) -> bool:
        ws = Path(self.config.cursor.workspace or "").resolve(strict=False)
        if not ws or str(ws) == "/":
            return True
        try:
            target = Path(path).expanduser().resolve(strict=False)
        except OSError:
            return False
        try:
            target.relative_to(ws)
            return True
        except ValueError:
            return False
