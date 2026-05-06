"""Configuration loading for cursor2telegram.

Sources, in order of precedence (later wins where it makes sense):
1. Environment variables for secrets and overrides.
2. ``/etc/cursor2telegram/config.toml`` (system).
3. ``$XDG_CONFIG_HOME/cursor2telegram/config.toml`` (user).
4. The path passed via ``--config``.
5. ``./config/local.toml`` (developer-only).

A real shipped install uses the env file ``/etc/cursor2telegram/env`` which
``systemd`` injects into the process environment.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - 3.11+ required by pyproject anyway
    import tomli as tomllib  # type: ignore[no-redef]


_DEFAULT_CONFIG_PATHS: tuple[Path, ...] = (
    Path("/etc/cursor2telegram/config.toml"),
    Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    / "cursor2telegram"
    / "config.toml",
    Path.cwd() / "config" / "local.toml",
)

DEFAULT_AGENT_BIN_CANDIDATES: tuple[str, ...] = (
    os.environ.get("CURSOR_AGENT_BIN", ""),
    str(Path.home() / ".local" / "bin" / "agent"),
    "/usr/local/bin/agent",
    "/usr/bin/agent",
    "agent",
)

DEFAULT_VOICE_SUMMARY_PROMPT = (
    "Summarize this Cursor/terminal result for a Telegram voice note in one short, useful sentence. "
    "Mention success/failure and the main outcome. Do not include secrets, tokens, or long paths."
)


@dataclass(frozen=True, slots=True)
class TelegramConfig:
    bot_token: str = ""
    allowed_user_ids: tuple[int, ...] = ()
    allowed_chat_ids: tuple[int, ...] = ()
    owner_user_id: int | None = None
    parse_mode: str = "HTML"
    drop_pending_updates: bool = True
    request_timeout_s: float = 30.0


@dataclass(frozen=True, slots=True)
class CursorConfig:
    agent_binary: str = ""
    api_key: str = ""
    auth_token: str = ""
    endpoint: str = ""
    insecure: bool = False
    default_mode: str = "agent"  # one of agent|plan|ask
    default_model: str = ""
    workspace: str = ""
    extra_args: tuple[str, ...] = ()
    force_writes: bool = True
    approve_mcps: bool = False
    sandbox: str = ""  # empty -> default; or "enabled"/"disabled"
    trust: bool = True
    use_worktree: bool = False


@dataclass(frozen=True, slots=True)
class PolicyConfig:
    """How the bot reacts to ACP permission/plan/question requests."""

    auto_approve_permissions: str = "prompt"  # prompt|allow-once|allow-always|reject-once
    auto_accept_plans: bool = False
    auto_answer_questions: bool = False
    require_confirmation_for: tuple[str, ...] = ("shell", "write", "delete", "network", "mcp")
    deny_outside_workspace_writes: bool = True


@dataclass(frozen=True, slots=True)
class UxConfig:
    stream_throttle_ms: int = 600
    max_message_chars: int = 3500  # below Telegram's 4096 to leave room for HTML escapes
    code_as_document_threshold: int = 1500
    use_message_drafts: bool = True
    show_tool_call_args: bool = True
    show_token_usage: bool = True
    notify_on_completion: bool = False


@dataclass(frozen=True, slots=True)
class VoiceConfig:
    enabled: bool = True
    provider: str = "openai"
    api_key: str = ""
    transcription_model: str = "whisper-1"
    summary_model: str = "gpt-4o-mini"
    tts_model: str = "tts-1"
    tts_voice: str = "alloy"
    tts_format: str = "opus"
    summary_enabled: bool = True
    summary_max_chars: int = 700
    summary_prompt: str = DEFAULT_VOICE_SUMMARY_PROMPT


@dataclass(frozen=True, slots=True)
class StorageConfig:
    state_dir: Path = Path("/var/lib/cursor2telegram")
    log_dir: Path = Path("/var/log/cursor2telegram")
    workspaces_root: Path = Path("/var/lib/cursor2telegram/workspaces")


@dataclass(frozen=True, slots=True)
class Config:
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    cursor: CursorConfig = field(default_factory=CursorConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    ux: UxConfig = field(default_factory=UxConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    config_path: Path | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def resolved_agent_binary(self) -> str:
        if self.cursor.agent_binary:
            return self.cursor.agent_binary
        for cand in DEFAULT_AGENT_BIN_CANDIDATES:
            if cand and (cand == "agent" or Path(cand).is_file()):
                return cand
        return "agent"

    def is_allowed(self, user_id: int | None, chat_id: int | None) -> bool:
        allowed_users = self.telegram.allowed_user_ids
        allowed_chats = self.telegram.allowed_chat_ids
        if not allowed_users and not allowed_chats:
            return False
        if user_id is not None and user_id in allowed_users:
            return True
        return chat_id is not None and chat_id in allowed_chats


def _coerce_int_tuple(value: Any) -> tuple[int, ...]:
    if value is None:
        return ()
    if isinstance(value, int):
        return (value,)
    if isinstance(value, str):
        items = [v.strip() for v in value.split(",") if v.strip()]
        return tuple(int(v) for v in items)
    if isinstance(value, list | tuple):
        return tuple(int(v) for v in value)
    raise TypeError(f"expected int/list, got {type(value).__name__}")


def _coerce_str_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        items = [v.strip() for v in value.split(",") if v.strip()]
        return tuple(items)
    if isinstance(value, list | tuple):
        return tuple(str(v) for v in value)
    raise TypeError(f"expected str/list, got {type(value).__name__}")


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def _merge(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    out = dict(a)
    for key, value in b.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: str | Path | None = None) -> Config:
    """Load configuration from disk + environment."""

    if path:
        paths: list[Path] = [Path(path)]
    else:
        paths = list(_DEFAULT_CONFIG_PATHS)

    raw: dict[str, Any] = {}
    chosen_path: Path | None = None
    for p in paths:
        data = _load_toml(p)
        if data:
            raw = _merge(raw, data)
            if chosen_path is None:
                chosen_path = p

    tg = raw.get("telegram", {}) or {}
    cu = raw.get("cursor", {}) or {}
    po = raw.get("policy", {}) or {}
    ux = raw.get("ux", {}) or {}
    vo = raw.get("voice", {}) or {}
    st = raw.get("storage", {}) or {}

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "") or tg.get("bot_token", "")
    api_key = os.environ.get("CURSOR_API_KEY", "") or cu.get("api_key", "")
    auth_token = os.environ.get("CURSOR_AUTH_TOKEN", "") or cu.get("auth_token", "")
    endpoint = os.environ.get("CURSOR_ENDPOINT", "") or cu.get("endpoint", "")
    agent_bin = os.environ.get("CURSOR_AGENT_BIN", "") or cu.get("agent_binary", "")

    telegram = TelegramConfig(
        bot_token=bot_token,
        allowed_user_ids=_coerce_int_tuple(tg.get("allowed_user_ids")),
        allowed_chat_ids=_coerce_int_tuple(tg.get("allowed_chat_ids")),
        owner_user_id=int(tg["owner_user_id"]) if tg.get("owner_user_id") not in (None, "") else None,
        parse_mode=str(tg.get("parse_mode", "HTML")),
        drop_pending_updates=bool(tg.get("drop_pending_updates", True)),
        request_timeout_s=float(tg.get("request_timeout_s", 30.0)),
    )

    cursor = CursorConfig(
        agent_binary=agent_bin,
        api_key=api_key,
        auth_token=auth_token,
        endpoint=endpoint,
        insecure=bool(cu.get("insecure", False)),
        default_mode=str(cu.get("default_mode", "agent")),
        default_model=str(cu.get("default_model", "")),
        workspace=str(cu.get("workspace", "")),
        extra_args=_coerce_str_tuple(cu.get("extra_args")),
        force_writes=bool(cu.get("force_writes", True)),
        approve_mcps=bool(cu.get("approve_mcps", False)),
        sandbox=str(cu.get("sandbox", "")),
        trust=bool(cu.get("trust", True)),
        use_worktree=bool(cu.get("use_worktree", False)),
    )

    policy = PolicyConfig(
        auto_approve_permissions=str(po.get("auto_approve_permissions", "prompt")),
        auto_accept_plans=bool(po.get("auto_accept_plans", False)),
        auto_answer_questions=bool(po.get("auto_answer_questions", False)),
        require_confirmation_for=_coerce_str_tuple(
            po.get("require_confirmation_for", ("shell", "write", "delete", "network", "mcp"))
        ),
        deny_outside_workspace_writes=bool(po.get("deny_outside_workspace_writes", True)),
    )

    ux_cfg = UxConfig(
        stream_throttle_ms=int(ux.get("stream_throttle_ms", 600)),
        max_message_chars=int(ux.get("max_message_chars", 3500)),
        code_as_document_threshold=int(ux.get("code_as_document_threshold", 1500)),
        use_message_drafts=bool(ux.get("use_message_drafts", True)),
        show_tool_call_args=bool(ux.get("show_tool_call_args", True)),
        show_token_usage=bool(ux.get("show_token_usage", True)),
        notify_on_completion=bool(ux.get("notify_on_completion", False)),
    )

    voice = VoiceConfig(
        enabled=bool(vo.get("enabled", True)),
        provider=str(vo.get("provider", "openai")),
        api_key=os.environ.get("OPENAI_API_KEY", "") or str(vo.get("api_key", "")),
        transcription_model=str(vo.get("transcription_model", "whisper-1")),
        summary_model=str(vo.get("summary_model", "gpt-4o-mini")),
        tts_model=str(vo.get("tts_model", "tts-1")),
        tts_voice=str(vo.get("tts_voice", "alloy")),
        tts_format=str(vo.get("tts_format", "opus")),
        summary_enabled=bool(vo.get("summary_enabled", True)),
        summary_max_chars=int(vo.get("summary_max_chars", 700)),
        summary_prompt=str(
            vo.get(
                "summary_prompt",
                DEFAULT_VOICE_SUMMARY_PROMPT,
            )
        ),
    )

    storage = StorageConfig(
        state_dir=Path(st.get("state_dir", "/var/lib/cursor2telegram")),
        log_dir=Path(st.get("log_dir", "/var/log/cursor2telegram")),
        workspaces_root=Path(st.get("workspaces_root", "/var/lib/cursor2telegram/workspaces")),
    )

    return Config(
        telegram=telegram,
        cursor=cursor,
        policy=policy,
        ux=ux_cfg,
        voice=voice,
        storage=storage,
        config_path=chosen_path,
        raw=raw,
    )
