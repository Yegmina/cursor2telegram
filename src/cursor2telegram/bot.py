"""Telegram bot front-end for Cursor CLI ACP."""

from __future__ import annotations

import asyncio
import contextlib
import json
import mimetypes
import os
import random
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx
from telegram import (
    BotCommand,
    BotCommandScopeDefault,
    Document,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction
from telegram.error import BadRequest
from telegram.ext import (
    AIORateLimiter,
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import __version__
from .acp import (
    AcpClient,
    AcpError,
    PERMISSION_ALLOW_ALWAYS,
    PERMISSION_ALLOW_ONCE,
    PERMISSION_REJECT_ONCE,
    initialize as acp_initialize,
    session_cancel as acp_session_cancel,
    session_load as acp_session_load,
    session_new as acp_session_new,
    session_prompt as acp_session_prompt,
)
from .config import Config
from .logging_setup import get_logger
from .policy import Policy
from .sessions import ChatSession, SessionManager
from .streaming import StreamAggregator, TextBuffer
from .telegram_ui import (
    PARSE_MODE,
    code_block,
    html_escape,
    kb_main_menu,
    kb_model_menu,
    kb_modes,
    kb_permission,
    kb_plan,
    kb_question,
    render_plan,
    render_question,
    render_tool_call,
    split_for_telegram,
)

log = get_logger(__name__)


# Pending interactive requests keyed by tool_call_id (server-side ACP).
class _PendingMap:
    def __init__(self) -> None:
        self._futures: dict[str, asyncio.Future[Any]] = {}

    def register(self, key: str) -> asyncio.Future[Any]:
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        self._futures[key] = fut
        return fut

    def resolve(self, key: str, value: Any) -> bool:
        fut = self._futures.pop(key, None)
        if fut and not fut.done():
            fut.set_result(value)
            return True
        return False

    def cancel_all(self) -> None:
        for fut in self._futures.values():
            if not fut.done():
                fut.cancel()
        self._futures.clear()


class BotApp:
    """High-level Telegram bot orchestrator."""

    BOT_COMMANDS: list[tuple[str, str]] = [
        ("start", "Welcome and quick actions"),
        ("help", "Show commands"),
        ("status", "Current session, mode, model, workspace"),
        ("sessions", "Show active session (alias for /status)"),
        ("new", "Start a new Cursor session"),
        ("cancel", "Cancel running prompt"),
        ("mode", "Switch mode: agent | plan | ask"),
        ("model", "Set or list models"),
        ("models", "List available models"),
        ("workspace", "Show or change workspace path"),
        ("yolo", "Toggle force/auto-approve writes"),
        ("sandbox", "Enable/disable sandbox"),
        ("permissions", "Show CLI permissions config"),
        ("mcp", "Manage MCP servers"),
        ("rules", "List Cursor rules in workspace"),
        ("files", "Browse workspace files"),
        ("get", "Download a file from workspace"),
        ("put", "Upload a file via reply (or /put path)"),
        ("run", "Run a shell command in workspace"),
        ("login", "Run cursor login (relay device URL)"),
        ("logout", "Run cursor logout"),
        ("whoami", "Show cursor account info"),
        ("about", "Versions and environment"),
        ("settings", "Inline settings menu"),
        ("ping", "Liveness probe"),
    ]

    def __init__(self, config: Config) -> None:
        self.config = config
        self.policy = Policy(config)
        self.sessions = SessionManager(config)
        self._pending = _PendingMap()
        self._app: Application | None = None
        self._aggregators: dict[int, StreamAggregator] = {}
        self._stream_msg_ids: dict[int, dict[str, int]] = {}

    # ---------------------------------------------------------------- runtime

    def build(self) -> Application:
        if not self.config.telegram.bot_token:
            raise RuntimeError(
                "TELEGRAM_BOT_TOKEN is not set. Provide it via env or [telegram] in config."
            )
        builder = (
            ApplicationBuilder()
            .token(self.config.telegram.bot_token)
            .rate_limiter(AIORateLimiter())
            .concurrent_updates(True)
        )
        app = builder.build()
        self._register_handlers(app)
        app.post_init = self._post_init
        app.post_shutdown = self._post_shutdown
        self._app = app
        return app

    async def _post_init(self, app: Application) -> None:
        commands = [BotCommand(cmd, desc) for cmd, desc in self.BOT_COMMANDS]
        with contextlib.suppress(Exception):
            await app.bot.set_my_commands(commands, scope=BotCommandScopeDefault())
        me = await app.bot.get_me()
        log.info("bot.ready", username=me.username, id=me.id)

    async def _post_shutdown(self, app: Application) -> None:
        self._pending.cancel_all()
        await self.sessions.close_all()

    def run(self) -> None:
        app = self.build()
        app.run_polling(
            drop_pending_updates=self.config.telegram.drop_pending_updates,
            stop_signals=None,
        )

    # ---------------------------------------------------------------- helpers

    async def _ensure_session(self, chat_id: int) -> ChatSession:
        sess = self.sessions.get_or_create(chat_id)
        if sess.acp and sess.acp.running and sess.session_id:
            return sess
        async with self.sessions.lock_for(chat_id):
            if sess.acp and sess.acp.running and sess.session_id:
                return sess
            await self._start_session(sess)
            return sess

    def _build_acp_command(self, sess: ChatSession) -> list[str]:
        bin_path = self.config.resolved_agent_binary()
        cmd: list[str] = [bin_path]
        if self.config.cursor.endpoint:
            cmd += ["-e", self.config.cursor.endpoint]
        if self.config.cursor.insecure:
            cmd += ["-k"]
        if self.config.cursor.api_key:
            cmd += ["--api-key", self.config.cursor.api_key]
        if self.config.cursor.auth_token:
            cmd += ["--auth-token", self.config.cursor.auth_token]
        sandbox = sess.sandbox or self.config.cursor.sandbox
        if sandbox in {"enabled", "disabled"}:
            cmd += ["--sandbox", sandbox]
        if self.config.cursor.approve_mcps:
            cmd += ["--approve-mcps"]
        if self.config.cursor.trust:
            cmd += ["--trust"]
        model_arg = self._agent_model_arg(sess.model)
        if model_arg:
            cmd += ["--model", model_arg]
        if sess.mode and sess.mode != "agent":
            cmd += ["--mode", sess.mode]
        cmd.extend(self.config.cursor.extra_args)
        cmd.append("acp")
        return cmd

    @staticmethod
    def _agent_model_arg(model_id: str) -> str:
        """Return a Cursor CLI --model value, or empty for ACP-only ids.

        ACP exposes rich ids like ``claude-opus-4-7[thinking=true,...]`` in
        session metadata, but the CLI currently accepts short model slugs for
        ``agent --model``. Passing the rich id makes the ACP process exit.
        """

        model_id = (model_id or "").strip()
        if not model_id or model_id in {"auto", "default", "default[]"}:
            return ""
        if "[" in model_id:
            return ""
        return model_id

    async def _preflight_login(self) -> tuple[bool, str]:
        """Return (ok, message). Cheap check before spawning ACP."""
        if self.config.cursor.api_key or self.config.cursor.auth_token:
            return True, ""
        rc, out, err = await self._run_agent(["status"], timeout=10)
        text = (out + "\n" + err).lower()
        if rc != 0:
            return False, (out or err or "agent status failed").strip()
        if "not logged in" in text or "not authenticated" in text:
            return False, (
                "Cursor CLI is not authenticated yet.\n\n"
                "On the server run as the service user:\n"
                "  sudo -u cursoragent -i agent login\n\n"
                "Or set CURSOR_API_KEY in /etc/cursor2telegram/env and restart "
                "the service."
            )
        return True, ""

    async def _start_session(self, sess: ChatSession) -> None:
        ok, msg = await self._preflight_login()
        if not ok:
            raise RuntimeError(msg)

        cmd = self._build_acp_command(sess)
        mcp_servers = self._load_acp_mcp_servers() if self.config.cursor.approve_mcps else []
        env = dict(os.environ)
        if self.config.cursor.api_key:
            env["CURSOR_API_KEY"] = self.config.cursor.api_key
        client = AcpClient(cmd, env=env, cwd=str(sess.workspace))
        sess.acp = client

        agg = StreamAggregator(throttle_ms=self.config.ux.stream_throttle_ms)
        self._aggregators[sess.chat_id] = agg

        client.on("session/update", lambda _m, p: agg.feed(p))
        client.on_request("session/request_permission", self._mk_permission_handler(sess))
        client.on_request("cursor/create_plan", self._mk_plan_handler(sess))
        client.on_request("cursor/ask_question", self._mk_question_handler(sess))
        client.on("cursor/update_todos", self._mk_todos_handler(sess))
        client.on("cursor/task", self._mk_task_handler(sess))
        client.on("cursor/generate_image", self._mk_image_handler(sess))

        await client.start()
        try:
            await asyncio.wait_for(
                acp_initialize(client, client_name="cursor2telegram", client_version=__version__),
                timeout=15,
            )
            try:
                await asyncio.wait_for(
                    client.request("authenticate", {"methodId": "cursor_login"}),
                    timeout=10,
                )
            except (AcpError, asyncio.TimeoutError):
                pass
            res = await asyncio.wait_for(
                acp_session_new(client, cwd=str(sess.workspace), mcp_servers=mcp_servers), timeout=20
            )
            sess.session_id = str(res.get("sessionId"))
            models = res.get("models") or {}
            modes = res.get("modes") or {}
            sess.extra_state["available_models"] = models.get("availableModels") or []
            sess.extra_state["current_model_id"] = models.get("currentModelId") or ""
            sess.extra_state["available_modes"] = modes.get("availableModes") or []
            sess.extra_state["current_mode_id"] = modes.get("currentModeId") or sess.mode
            log.info("session.started", chat_id=sess.chat_id, sid=sess.session_id)
        except Exception:
            await client.stop()
            sess.acp = None
            raise

    def _load_acp_mcp_servers(self) -> list[dict[str, Any]]:
        """Load Cursor MCP config and convert it to ACP's `mcpServers` array.

        Cursor's on-disk MCP config uses an object keyed by server name and env/
        headers as maps. ACP expects a list of server objects with env/headers as
        `{name, value}` arrays.
        """

        candidates = [
            Path.home() / ".cursor" / "mcp.json",
            Path.cwd() / ".cursor" / "mcp.json",
        ]
        for path in candidates:
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("mcp.config.read_failed", path=str(path), error=str(exc))
                continue
            raw_servers = data.get("mcpServers") or {}
            out: list[dict[str, Any]] = []
            for name, cfg in raw_servers.items():
                if not isinstance(cfg, dict):
                    continue
                server: dict[str, Any] = {"name": str(name)}
                if "command" in cfg:
                    server["command"] = str(cfg["command"])
                    if cfg.get("args") is not None:
                        server["args"] = [str(a) for a in cfg.get("args", [])]
                    if cfg.get("cwd"):
                        server["cwd"] = str(cfg["cwd"])
                    if isinstance(cfg.get("env"), dict):
                        server["env"] = [
                            {"name": str(k), "value": str(v)} for k, v in cfg["env"].items()
                        ]
                elif "url" in cfg:
                    server["type"] = str(cfg.get("type") or "http")
                    server["url"] = str(cfg["url"])
                    if isinstance(cfg.get("headers"), dict):
                        server["headers"] = [
                            {"name": str(k), "value": str(v)}
                            for k, v in cfg["headers"].items()
                        ]
                    else:
                        server["headers"] = []
                else:
                    continue
                out.append(server)
            log.info("mcp.config.loaded_for_acp", path=str(path), servers=[s["name"] for s in out])
            return out
        return []

    # ---------------------------------------------------------------- handlers

    def _register_handlers(self, app: Application) -> None:
        # Commands
        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler(["help", "h"], self.cmd_help))
        app.add_handler(CommandHandler("status", self.cmd_status))
        app.add_handler(CommandHandler("new", self.cmd_new))
        app.add_handler(CommandHandler("cancel", self.cmd_cancel))
        app.add_handler(CommandHandler("mode", self.cmd_mode))
        app.add_handler(CommandHandler("model", self.cmd_model))
        app.add_handler(CommandHandler("models", self.cmd_models))
        app.add_handler(CommandHandler("workspace", self.cmd_workspace))
        app.add_handler(CommandHandler("yolo", self.cmd_yolo))
        app.add_handler(CommandHandler("sandbox", self.cmd_sandbox))
        app.add_handler(CommandHandler("permissions", self.cmd_permissions))
        app.add_handler(CommandHandler("mcp", self.cmd_mcp))
        app.add_handler(CommandHandler("rules", self.cmd_rules))
        app.add_handler(CommandHandler("files", self.cmd_files))
        app.add_handler(CommandHandler("get", self.cmd_get))
        app.add_handler(CommandHandler("run", self.cmd_run))
        app.add_handler(CommandHandler("login", self.cmd_login))
        app.add_handler(CommandHandler("logout", self.cmd_logout))
        app.add_handler(CommandHandler("whoami", self.cmd_whoami))
        app.add_handler(CommandHandler("about", self.cmd_about))
        app.add_handler(CommandHandler("settings", self.cmd_settings))
        app.add_handler(CommandHandler("ping", self.cmd_ping))
        app.add_handler(CommandHandler("sessions", self.cmd_status))
        app.add_handler(CommandHandler("put", self.cmd_put))
        # Callback buttons
        app.add_handler(CallbackQueryHandler(self.on_callback))
        # Text + files
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))
        app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, self.on_file))
        # Catch-all unknown commands.
        app.add_handler(MessageHandler(filters.COMMAND, self.on_unknown_command))
        # Errors
        app.add_error_handler(self.on_error)

    async def cmd_put(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        await update.effective_message.reply_text(
            "Send any file or photo as a Telegram attachment; I'll save it under "
            "<code>_uploads/</code> in the current workspace.",
            parse_mode=PARSE_MODE,
        )

    async def cmd_ping(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        sess = self.sessions.get(update.effective_chat.id)
        running = bool(sess and sess.acp and sess.acp.running)
        await update.effective_message.reply_text(
            f"pong (acp={'running' if running else 'idle'}, v{__version__})"
        )

    async def on_unknown_command(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        text = update.effective_message.text or ""
        await update.effective_message.reply_text(
            f"Unknown command: <code>{html_escape(text.split()[0])}</code>. Try /help.",
            parse_mode=PARSE_MODE,
        )

    # ----- access guard

    async def _guard(self, update: Update) -> bool:
        user = update.effective_user
        chat = update.effective_chat
        check = self.policy.check_user(user.id if user else None, chat.id if chat else None)
        if not check.allowed:
            log.warning("access.denied", user=getattr(user, "id", None), chat=getattr(chat, "id", None))
            if update.effective_message:
                await update.effective_message.reply_text(
                    f"Access denied: {check.reason}.\n\nIf this is your bot, add your user id to "
                    f"<code>[telegram] allowed_user_ids</code> in the config.",
                    parse_mode=PARSE_MODE,
                )
            return False
        return True

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        err = context.error
        text = str(err) if err is not None else ""
        type_name = type(err).__name__ if err is not None else "Error"
        log.exception("bot.error", error=text, type=type_name)
        if isinstance(update, Update) and update.effective_message:
            with contextlib.suppress(Exception):
                body = f"Error ({type_name}): {html_escape(text or '(no message)')}"
                await update.effective_message.reply_text(body, parse_mode=PARSE_MODE)

    # ----- commands

    async def cmd_start(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        chat = update.effective_chat
        sess = self.sessions.get_or_create(chat.id)
        text = (
            f"<b>cursor2telegram v{__version__}</b>\n"
            f"Workspace: <code>{html_escape(str(sess.workspace))}</code>\n"
            f"Mode: <b>{sess.mode}</b>  Model: <b>{html_escape(sess.model or 'auto')}</b>\n\n"
            "Send a message to prompt the agent. Use /help to see all commands."
        )
        await update.effective_message.reply_text(
            text, parse_mode=PARSE_MODE, reply_markup=kb_main_menu()
        )

    async def cmd_help(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        lines = [f"/{c} — {d}" for c, d in self.BOT_COMMANDS]
        await update.effective_message.reply_text(
            "<b>Commands</b>\n" + "\n".join(lines), parse_mode=PARSE_MODE
        )

    async def cmd_status(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        sess = self.sessions.get_or_create(update.effective_chat.id)
        running = bool(sess.acp and sess.acp.running)
        text = (
            "<b>Status</b>\n"
            f"acp: {'running' if running else 'stopped'}\n"
            f"session: <code>{html_escape(sess.session_id or '-')}</code>\n"
            f"mode: <b>{sess.mode}</b>  model: <b>{html_escape(sess.model or 'auto')}</b>  "
            f"sandbox: <b>{sess.sandbox or '-'}</b>  yolo: <b>{'on' if sess.yolo else 'off'}</b>\n"
            f"workspace: <code>{html_escape(str(sess.workspace))}</code>"
        )
        await update.effective_message.reply_text(
            text, parse_mode=PARSE_MODE, reply_markup=kb_main_menu()
        )

    async def cmd_new(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        cid = update.effective_chat.id
        async with self.sessions.lock_for(cid):
            await self.sessions.close(cid)
            sess = self.sessions.get_or_create(cid)
            try:
                await self._start_session(sess)
            except Exception as exc:  # noqa: BLE001 - surface startup failures in Telegram
                if sess.acp:
                    with contextlib.suppress(Exception):
                        await sess.acp.stop()
                    sess.acp = None
                await update.effective_message.reply_text(
                    f"Could not start Cursor session: {html_escape(str(exc) or type(exc).__name__)}",
                    parse_mode=PARSE_MODE,
                )
                return
        await update.effective_message.reply_text(
            f"Started new session <code>{html_escape(sess.session_id or '?')}</code>",
            parse_mode=PARSE_MODE,
        )

    async def cmd_cancel(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        sess = self.sessions.get(update.effective_chat.id)
        if not sess or not sess.acp or not sess.session_id:
            await update.effective_message.reply_text("No active session.")
            return
        if sess.pending_prompt and not sess.pending_prompt.done():
            sess.pending_prompt.cancel()
        with contextlib.suppress(Exception):
            await acp_session_cancel(sess.acp, session_id=sess.session_id)
        await update.effective_message.reply_text("Cancellation requested.")

    async def cmd_mode(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        sess = self.sessions.get_or_create(update.effective_chat.id)
        args = ctx.args or []
        if not args:
            await update.effective_message.reply_text(
                f"Current mode: <b>{sess.mode}</b>",
                parse_mode=PARSE_MODE,
                reply_markup=kb_modes(sess.mode),
            )
            return
        new_mode = args[0].lower()
        if new_mode not in {"agent", "plan", "ask"}:
            await update.effective_message.reply_text("Mode must be one of: agent, plan, ask.")
            return
        sess.mode = new_mode
        await update.effective_message.reply_text(f"Mode set to <b>{new_mode}</b>", parse_mode=PARSE_MODE)

    async def cmd_model(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        chat_id = update.effective_chat.id
        sess = self.sessions.get_or_create(chat_id)
        if not ctx.args:
            if not sess.extra_state.get("available_models"):
                try:
                    sess = await self._ensure_session(chat_id)
                except Exception as exc:  # noqa: BLE001
                    await update.effective_message.reply_text(
                        "Current model: "
                        f"<b>{html_escape(sess.model or 'auto')}</b>\n"
                        "Could not refresh model list: "
                        f"{html_escape(str(exc) or type(exc).__name__)}",
                        parse_mode=PARSE_MODE,
                    )
                    return
            models = list(sess.extra_state.get("available_models") or [])
            token_map = {f"m{i}": str(m.get("modelId") or "") for i, m in enumerate(models[:12])}
            sess.extra_state["model_tokens"] = token_map
            current = sess.model or str(sess.extra_state.get("current_model_id") or "")
            if models:
                await update.effective_message.reply_text(
                    f"<b>Choose model</b>\nCurrent: <code>{html_escape(current or 'auto')}</code>\n"
                    "Changing model restarts ACP on the next prompt.",
                    parse_mode=PARSE_MODE,
                    reply_markup=kb_model_menu(models, active_model_id=current, token_map=token_map),
                )
                return
            await update.effective_message.reply_text(
                f"Current model: <b>{html_escape(sess.model or 'auto')}</b>\nUse /model &lt;name&gt; or /models.",
                parse_mode=PARSE_MODE,
            )
            return
        requested_model = ctx.args[0]
        sess.model = "" if requested_model in {"auto", "default", "default[]"} else requested_model
        if sess.acp:
            await sess.acp.stop()
            sess.acp = None
            sess.session_id = None
        await update.effective_message.reply_text(
            f"Model set to <b>{html_escape(sess.model or 'auto')}</b>. Next prompt will start a fresh ACP session.",
            parse_mode=PARSE_MODE,
        )

    async def cmd_models(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        rc, out, err = await self._run_agent(["models"], timeout=30)
        await self._send_text_or_doc(update, out or err or "(no output)")

    async def cmd_workspace(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        sess = self.sessions.get_or_create(update.effective_chat.id)
        if not ctx.args:
            await update.effective_message.reply_text(
                f"Workspace: <code>{html_escape(str(sess.workspace))}</code>", parse_mode=PARSE_MODE
            )
            return
        new_path = Path(" ".join(ctx.args)).expanduser().resolve()
        if not new_path.exists():
            new_path.mkdir(parents=True, exist_ok=True)
        if sess.acp:
            await self.sessions.close(sess.chat_id)
            sess = self.sessions.get_or_create(update.effective_chat.id)
        sess.workspace = new_path
        await update.effective_message.reply_text(
            f"Workspace set to <code>{html_escape(str(new_path))}</code> (next prompt will start a new session).",
            parse_mode=PARSE_MODE,
        )

    async def cmd_yolo(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        sess = self.sessions.get_or_create(update.effective_chat.id)
        if ctx.args:
            sess.yolo = ctx.args[0].lower() in {"on", "true", "yes", "1"}
        else:
            sess.yolo = not sess.yolo
        await update.effective_message.reply_text(f"YOLO/force = <b>{'on' if sess.yolo else 'off'}</b>", parse_mode=PARSE_MODE)

    async def cmd_sandbox(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        sess = self.sessions.get_or_create(update.effective_chat.id)
        if not ctx.args:
            await update.effective_message.reply_text(f"Sandbox: <b>{sess.sandbox or '-'}</b>", parse_mode=PARSE_MODE)
            return
        val = ctx.args[0].lower()
        if val in {"on", "enabled"}:
            sess.sandbox = "enabled"
        elif val in {"off", "disabled"}:
            sess.sandbox = "disabled"
        else:
            await update.effective_message.reply_text("Use /sandbox on|off")
            return
        await update.effective_message.reply_text(f"Sandbox set to <b>{sess.sandbox}</b>", parse_mode=PARSE_MODE)

    async def cmd_permissions(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        cfg_path = Path.home() / ".cursor" / "cli-config.json"
        text = ""
        if cfg_path.is_file():
            text = cfg_path.read_text(encoding="utf-8", errors="replace")
        else:
            text = "(no ~/.cursor/cli-config.json yet)"
        await self._send_text_or_doc(update, text, language="json", filename="cli-config.json")

    async def cmd_mcp(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        sub = (ctx.args[0] if ctx.args else "list").lower()
        rest = ctx.args[1:] if ctx.args else []
        rc, out, err = await self._run_agent(["mcp", sub, *rest], timeout=30)
        await self._send_text_or_doc(update, out or err or "(no output)")

    async def cmd_rules(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        sess = self.sessions.get_or_create(update.effective_chat.id)
        rules_dir = sess.workspace / ".cursor" / "rules"
        if not rules_dir.is_dir():
            await update.effective_message.reply_text("No .cursor/rules directory in this workspace.")
            return
        items = sorted(p.name for p in rules_dir.iterdir() if p.is_file())
        await update.effective_message.reply_text(
            "<b>Rules</b>\n" + ("\n".join(f"• {html_escape(n)}" for n in items) or "(empty)"),
            parse_mode=PARSE_MODE,
        )

    async def cmd_files(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        sess = self.sessions.get_or_create(update.effective_chat.id)
        rel = ctx.args[0] if ctx.args else "."
        target = (sess.workspace / rel).resolve()
        if not str(target).startswith(str(sess.workspace.resolve())):
            await update.effective_message.reply_text("Path escapes workspace.")
            return
        if not target.exists():
            await update.effective_message.reply_text("Path not found.")
            return
        if target.is_file():
            await self._send_file(update, target)
            return
        entries = []
        for entry in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
            try:
                size = entry.stat().st_size if entry.is_file() else "-"
            except OSError:
                size = "?"
            kind = "F" if entry.is_file() else "D"
            entries.append(f"{kind} {html_escape(entry.name)}  ({size})")
        body = "\n".join(entries) or "(empty)"
        await self._send_text_or_doc(update, body)

    async def cmd_get(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        if not ctx.args:
            await update.effective_message.reply_text("Usage: /get &lt;path&gt;", parse_mode=PARSE_MODE)
            return
        sess = self.sessions.get_or_create(update.effective_chat.id)
        target = (sess.workspace / " ".join(ctx.args)).resolve()
        if not str(target).startswith(str(sess.workspace.resolve())):
            await update.effective_message.reply_text("Path escapes workspace.")
            return
        if not target.is_file():
            await update.effective_message.reply_text("Not a file.")
            return
        await self._send_file(update, target)

    async def cmd_run(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        if not self.policy.is_owner(update.effective_user.id):
            await update.effective_message.reply_text("/run is owner-only.")
            return
        cmdline = " ".join(ctx.args)
        if not cmdline.strip():
            await update.effective_message.reply_text("Usage: /run &lt;cmd&gt;", parse_mode=PARSE_MODE)
            return
        sess = self.sessions.get_or_create(update.effective_chat.id)
        rc, out, err = await self._run_shell(cmdline, cwd=str(sess.workspace), timeout=120)
        body = (out or "") + (("\nSTDERR:\n" + err) if err else "") + f"\n[exit {rc}]"
        await self._send_text_or_doc(update, body)

    async def cmd_login(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        await update.effective_message.reply_text(
            "Run <code>agent login</code> on the server as <code>cursoragent</code> and follow the URL it prints. "
            "For unattended setups set <code>CURSOR_API_KEY</code> in /etc/cursor2telegram/env.",
            parse_mode=PARSE_MODE,
        )

    async def cmd_logout(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        rc, out, err = await self._run_agent(["logout"], timeout=15)
        await update.effective_message.reply_text(html_escape(out or err or f"exit {rc}"), parse_mode=PARSE_MODE)

    async def cmd_whoami(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        rc, out, err = await self._run_agent(["status"], timeout=15)
        await update.effective_message.reply_text(html_escape(out or err or f"exit {rc}"), parse_mode=PARSE_MODE)

    async def cmd_about(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        rc, out, err = await self._run_agent(["about"], timeout=15)
        body = (
            f"<b>cursor2telegram</b> v{__version__}\n"
            f"agent binary: <code>{html_escape(self.config.resolved_agent_binary())}</code>\n\n"
            + html_escape(out or err or "")
        )
        await self._send_text_or_doc(update, body, html=True)

    async def cmd_settings(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        sess = self.sessions.get_or_create(update.effective_chat.id)
        await update.effective_message.reply_text(
            f"<b>Settings</b>\nMode: {sess.mode}\nModel: {html_escape(sess.model or 'auto')}\n"
            f"Sandbox: {sess.sandbox or '-'}\nYOLO: {'on' if sess.yolo else 'off'}",
            parse_mode=PARSE_MODE,
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("agent", callback_data="mode:agent"),
                        InlineKeyboardButton("plan", callback_data="mode:plan"),
                        InlineKeyboardButton("ask", callback_data="mode:ask"),
                    ],
                    [
                        InlineKeyboardButton("YOLO toggle", callback_data="yolo:toggle"),
                        InlineKeyboardButton("Sandbox on", callback_data="sandbox:on"),
                        InlineKeyboardButton("Sandbox off", callback_data="sandbox:off"),
                    ],
                    [
                        InlineKeyboardButton("New session", callback_data="cmd:new"),
                        InlineKeyboardButton("Cancel", callback_data="cmd:cancel"),
                    ],
                ]
            ),
        )

    # ----- text + files

    async def on_text(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        text = update.effective_message.text or ""
        if not text.strip():
            return
        chat = update.effective_chat
        try:
            sess = await self._ensure_session(chat.id)
        except Exception as exc:  # noqa: BLE001 - keep Telegram UX actionable
            await update.effective_message.reply_text(
                f"Could not start Cursor session: {html_escape(str(exc) or type(exc).__name__)}",
                parse_mode=PARSE_MODE,
            )
            return
        await self._dispatch_prompt(update, sess, text)

    async def on_file(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        msg = update.effective_message
        chat = update.effective_chat
        sess = self.sessions.get_or_create(chat.id)
        upload_dir = sess.workspace / "_uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        if msg.document:
            doc: Document = msg.document
            file = await doc.get_file()
            target = upload_dir / (doc.file_name or f"upload-{int(time.time())}")
        elif msg.photo:
            photo = msg.photo[-1]
            file = await photo.get_file()
            target = upload_dir / f"photo-{int(time.time())}.jpg"
        else:
            return
        await file.download_to_drive(custom_path=str(target))
        await msg.reply_text(
            f"Saved to <code>{html_escape(str(target.relative_to(sess.workspace)))}</code>",
            parse_mode=PARSE_MODE,
        )

    # ----- callbacks

    async def on_callback(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        data = query.data or ""
        chat_id = query.message.chat.id if query.message else None
        if not chat_id:
            return
        if not await self._guard(update):
            return
        sess = self.sessions.get_or_create(chat_id)
        parts = data.split(":")
        head = parts[0]

        if head == "mode" and len(parts) == 2:
            new_mode = parts[1]
            if new_mode in {"agent", "plan", "ask"}:
                sess.mode = new_mode
                await query.edit_message_reply_markup(reply_markup=kb_modes(sess.mode))
                await query.message.reply_text(f"Mode → <b>{new_mode}</b>", parse_mode=PARSE_MODE)
            return

        if head == "model" and len(parts) >= 3 and parts[1] == "set":
            token = ":".join(parts[2:])
            model_id = "" if token == "auto" else str((sess.extra_state.get("model_tokens") or {}).get(token, ""))
            if token != "auto" and not model_id:
                await query.message.reply_text("That model option expired. Tap Model again to refresh.")
                return
            if model_id in {"auto", "default", "default[]"}:
                model_id = ""
            sess.model = model_id
            if sess.acp:
                await sess.acp.stop()
                sess.acp = None
                sess.session_id = None
            label = model_id or "auto"
            with contextlib.suppress(BadRequest):
                await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(
                f"Model → <code>{html_escape(label)}</code>\nNext prompt starts a fresh ACP session.",
                parse_mode=PARSE_MODE,
            )
            return

        if head == "yolo" and len(parts) == 2 and parts[1] == "toggle":
            sess.yolo = not sess.yolo
            await query.message.reply_text(f"YOLO = <b>{'on' if sess.yolo else 'off'}</b>", parse_mode=PARSE_MODE)
            return

        if head == "sandbox" and len(parts) == 2:
            sess.sandbox = "enabled" if parts[1] == "on" else "disabled"
            await query.message.reply_text(f"Sandbox = <b>{sess.sandbox}</b>", parse_mode=PARSE_MODE)
            return

        if head == "cmd" and len(parts) == 2:
            handler = self._callback_command_handlers().get(parts[1])
            if handler:
                await handler(update, _ctx)
            else:
                await query.message.reply_text(
                    f"Button not implemented yet: <code>{html_escape(parts[1])}</code>",
                    parse_mode=PARSE_MODE,
                )
            return

        if head == "perm" and len(parts) >= 3:
            decision = parts[1]
            tool_call_id = ":".join(parts[2:])
            value = {
                "once": PERMISSION_ALLOW_ONCE,
                "always": PERMISSION_ALLOW_ALWAYS,
                "reject": PERMISSION_REJECT_ONCE,
            }.get(decision, PERMISSION_REJECT_ONCE)
            self._pending.resolve(f"perm:{tool_call_id}", value)
            with contextlib.suppress(BadRequest):
                await query.edit_message_reply_markup(reply_markup=None)
            return

        if head == "plan" and len(parts) >= 3:
            decision = parts[1]
            tool_call_id = ":".join(parts[2:])
            self._pending.resolve(f"plan:{tool_call_id}", decision == "accept")
            with contextlib.suppress(BadRequest):
                await query.edit_message_reply_markup(reply_markup=None)
            return

        if head == "q" and len(parts) >= 4:
            tool_call_id = parts[1]
            question_id = parts[2]
            option_id = ":".join(parts[3:])
            self._pending.resolve(f"q:{tool_call_id}:{question_id}", option_id)
            with contextlib.suppress(BadRequest):
                await query.edit_message_reply_markup(reply_markup=None)
            return

    def _callback_command_handlers(self) -> dict[str, Any]:
        """Handlers available from inline main-menu buttons.

        Keep this in sync with :func:`kb_main_menu`; tests assert that every
        `cmd:*` button has a route here.
        """

        return {
            "status": self.cmd_status,
            "new": self.cmd_new,
            "cancel": self.cmd_cancel,
            "mode": self.cmd_mode,
            "model": self.cmd_model,
            "workspace": self.cmd_workspace,
            "files": self.cmd_files,
            "mcp": self.cmd_mcp,
            "help": self.cmd_help,
        }

    # ----- prompt dispatch

    async def _dispatch_prompt(self, update: Update, sess: ChatSession, text: str) -> None:
        if sess.pending_prompt and not sess.pending_prompt.done():
            await update.effective_message.reply_text(
                "A prompt is already running. Use /cancel first or wait for it to finish."
            )
            return

        await update.effective_chat.send_action(ChatAction.TYPING)
        full_text = self._compose_prompt(sess, text)
        agg = self._aggregators.get(sess.chat_id)
        if agg is None:
            agg = StreamAggregator(throttle_ms=self.config.ux.stream_throttle_ms)
            self._aggregators[sess.chat_id] = agg

        async def runner() -> None:
            assert sess.acp and sess.session_id
            renderer = asyncio.create_task(self._render_stream(update, sess, agg))
            try:
                result = await acp_session_prompt(
                    sess.acp, session_id=sess.session_id, text=full_text
                )
                stop_reason = result.get("stopReason") if isinstance(result, dict) else None
                if stop_reason and self.config.ux.notify_on_completion:
                    await update.effective_chat.send_message(
                        f"<i>done — {html_escape(str(stop_reason))}</i>", parse_mode=PARSE_MODE
                    )
            except AcpError as e:
                await update.effective_chat.send_message(
                    f"acp error: {html_escape(str(e))}", parse_mode=PARSE_MODE
                )
            finally:
                await agg.close()
                with contextlib.suppress(asyncio.CancelledError):
                    await renderer

        sess.pending_prompt = asyncio.create_task(runner(), name=f"prompt-{sess.chat_id}")
        sess.touch()

    def _compose_prompt(self, sess: ChatSession, text: str) -> str:
        prelude = []
        if sess.mode and sess.mode != "agent":
            prelude.append(f"[mode={sess.mode}]")
        if sess.model:
            prelude.append(f"[model={sess.model}]")
        if sess.yolo:
            prelude.append("[force/yolo=on]")
        if prelude:
            return " ".join(prelude) + "\n" + text
        return text

    async def _render_stream(
        self, update: Update, sess: ChatSession, agg: StreamAggregator
    ) -> None:
        chat = update.effective_chat
        buf = TextBuffer(throttle_s=self.config.ux.stream_throttle_ms / 1000.0)
        current_msg_id: int | None = None
        current_msg_text: str = ""
        draft_id = random.randint(1, 2_147_483_647)
        draft_enabled = self.config.ux.use_message_drafts and chat.type == "private"
        draft_failed = False
        sent_files: set[str] = set()

        async def send_new_files_from(value: Any) -> None:
            for file_path in self._extract_file_paths(value):
                key = str(file_path)
                if key in sent_files:
                    continue
                sent_files.add(key)
                await self._send_file_to_chat(chat, file_path)

        async def flush_text(force: bool = False) -> None:
            nonlocal current_msg_id, current_msg_text, draft_failed
            if not (force or buf.should_flush()):
                return
            chunk = buf.force_flush() if force else buf.flush()
            if not chunk:
                return
            new_text = current_msg_text + chunk
            if draft_enabled and not draft_failed and len(new_text) <= self.config.ux.max_message_chars:
                draft_ok = await self._send_message_draft(
                    chat_id=chat.id,
                    draft_id=draft_id,
                    text=new_text,
                )
                if draft_ok:
                    current_msg_text = new_text
                    return
                draft_failed = True
            if len(new_text) > self.config.ux.max_message_chars:
                if current_msg_id:
                    pass
                msg = await chat.send_message(chunk, parse_mode=None)
                current_msg_id = msg.message_id
                current_msg_text = chunk
                return
            if current_msg_id is None:
                msg = await chat.send_message(chunk, parse_mode=None)
                current_msg_id = msg.message_id
                current_msg_text = chunk
            else:
                current_msg_text = new_text
                try:
                    await chat.get_bot().edit_message_text(
                        chat_id=chat.id, message_id=current_msg_id, text=current_msg_text
                    )
                except BadRequest:
                    msg = await chat.send_message(chunk, parse_mode=None)
                    current_msg_id = msg.message_id
                    current_msg_text = chunk

        async def end_text_block() -> None:
            nonlocal current_msg_id, current_msg_text
            await flush_text(force=True)
            if draft_enabled and not draft_failed and current_msg_text:
                await chat.send_message(current_msg_text, parse_mode=None)
            if current_msg_text:
                await send_new_files_from(current_msg_text)
            current_msg_id = None
            current_msg_text = ""

        last_periodic = time.monotonic()
        async for ev in agg.events():
            if ev.kind == "text_chunk":
                buf.append(ev.text)
                now = time.monotonic()
                if buf.should_flush() or (now - last_periodic) >= 1.5:
                    await flush_text()
                    last_periodic = now
            elif ev.kind == "text_block_end":
                await end_text_block()
            elif ev.kind == "tool_call_start":
                await end_text_block()
                args = ev.data.get("args") or {}
                name = ev.data.get("name") or "?"
                category = self.policy.categorize_tool(name, args)
                body = render_tool_call(name, args if self.config.ux.show_tool_call_args else None)
                await chat.send_message(body, parse_mode=PARSE_MODE)
                if self.policy.should_confirm_tool(category) and not sess.yolo:
                    await chat.send_message(
                        f"Confirm <b>{html_escape(category)}</b> action above?",
                        parse_mode=PARSE_MODE,
                        reply_markup=kb_permission(ev.data.get("id")),
                    )
            elif ev.kind == "tool_call_end":
                result = ev.data.get("result")
                if result:
                    text = self._format_tool_result(result)
                    if text:
                        await self._send_text_or_doc_chat(chat, text)
                    await send_new_files_from(result)
            elif ev.kind == "todos_update":
                raw = ev.data.get("raw") or {}
                todos = raw.get("todos") or []
                if todos:
                    body = "<b>Todos</b>\n" + "\n".join(
                        f"{self._todo_marker(t.get('status'))} {html_escape(str(t.get('content', '')))}"
                        for t in todos
                    )
                    await chat.send_message(body, parse_mode=PARSE_MODE)
            elif ev.kind == "stop":
                await end_text_block()
                reason = ev.data.get("reason") or "stop"
                await chat.send_message(f"<i>stop: {html_escape(str(reason))}</i>", parse_mode=PARSE_MODE)
            elif ev.kind == "raw":
                log.debug("stream.raw", kind=ev.data.get("kind"))
        await end_text_block()

    @staticmethod
    def _todo_marker(status: str | None) -> str:
        return {
            "completed": "[x]",
            "in_progress": "[~]",
            "cancelled": "[-]",
            "pending": "[ ]",
        }.get(str(status or ""), "[ ]")

    def _format_tool_result(self, result: Any) -> str:
        if isinstance(result, dict):
            if any(k in result for k in ("stdout", "stderr", "exitCode")):
                stdout = str(result.get("stdout") or "")
                stderr = str(result.get("stderr") or "")
                exit_code = result.get("exitCode")
                parts = []
                if stdout:
                    parts.append(stdout.rstrip())
                if stderr:
                    parts.append("STDERR:\n" + stderr.rstrip())
                if exit_code is not None:
                    parts.append(f"[exit {exit_code}]")
                return "\n".join(parts)
            text = result.get("text") or result.get("output") or ""
            if not text and "content" in result:
                cont = result["content"]
                if isinstance(cont, list) and cont:
                    text = " ".join(str(c.get("text", "")) for c in cont if isinstance(c, dict))
            return str(text)
        return str(result or "")

    def _extract_file_paths(self, value: Any) -> list[Path]:
        """Find generated local files in tool results.

        Browser screenshots commonly return `{"saved": "/path/file.png"}`.
        Other tools often use `path`, `filePath`, `filename`, or nested content.
        """

        candidates: list[str] = []

        def walk(v: Any) -> None:
            if isinstance(v, dict):
                for key, item in v.items():
                    if key in {"path", "filePath", "filename", "saved", "downloadPath"}:
                        if isinstance(item, str):
                            candidates.append(item)
                    walk(item)
            elif isinstance(v, list | tuple):
                for item in v:
                    walk(item)
            elif isinstance(v, str):
                candidates.extend(
                    re.findall(
                        r"(/[^\s'\"<>]+\.(?:png|jpe?g|gif|webp|mp4|mov|webm|mkv|mp3|wav|ogg|pdf|zip|txt|json|csv|log))",
                        v,
                        flags=re.IGNORECASE,
                    )
                )

        walk(value)
        out: list[Path] = []
        seen: set[str] = set()
        for candidate in candidates:
            path = Path(candidate).expanduser().resolve(strict=False)
            if str(path) in seen or not path.is_file():
                continue
            seen.add(str(path))
            out.append(path)
        return out

    async def _send_message_draft(self, *, chat_id: int, draft_id: int, text: str) -> bool:
        """Use Telegram Bot API 9.5 native draft streaming when available.

        python-telegram-bot may lag new Bot API methods, so this calls the raw
        HTTP endpoint directly and falls back silently when unsupported.
        """

        token = self.config.telegram.bot_token
        if not token or not text:
            return False
        url = f"https://api.telegram.org/bot{token}/sendMessageDraft"
        payload = {
            "chat_id": chat_id,
            "draft_id": draft_id,
            "text": text[-self.config.ux.max_message_chars :],
        }
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.post(url, json=payload)
            if resp.status_code == 404:
                log.info("telegram.draft_stream.unsupported")
                return False
            if resp.status_code >= 400:
                log.warning(
                    "telegram.draft_stream.failed",
                    status_code=resp.status_code,
                    body=resp.text[:200],
                )
                return False
            return True
        except Exception as exc:  # noqa: BLE001 - draft streaming is optional
            log.warning("telegram.draft_stream.error", error=str(exc))
            return False

    # ----- ACP request handlers (server-to-client)

    def _mk_permission_handler(self, sess: ChatSession):
        async def handler(_method: str, params: dict[str, Any]) -> dict[str, Any]:
            tool_call_id = str(params.get("toolCallId") or params.get("tool_call_id") or "_")
            description = (
                params.get("description")
                or params.get("title")
                or params.get("name")
                or "Tool wants permission"
            )
            policy_default = self.policy.default_permission_decision()
            if sess.yolo or policy_default in {"allow-once", "allow-always", "reject-once"}:
                decision = policy_default if not sess.yolo else PERMISSION_ALLOW_ONCE
                return {"outcome": {"outcome": "selected", "optionId": decision}}
            chat = self._app.bot if self._app else None
            if not chat:
                return {"outcome": {"outcome": "selected", "optionId": PERMISSION_REJECT_ONCE}}
            await chat.send_message(
                chat_id=sess.chat_id,
                text=f"<b>Permission request</b>\n{html_escape(str(description))}",
                parse_mode=PARSE_MODE,
                reply_markup=kb_permission(tool_call_id),
            )
            fut = self._pending.register(f"perm:{tool_call_id}")
            try:
                decision = await asyncio.wait_for(fut, timeout=300)
            except asyncio.TimeoutError:
                decision = PERMISSION_REJECT_ONCE
            return {"outcome": {"outcome": "selected", "optionId": decision}}

        return handler

    def _mk_plan_handler(self, sess: ChatSession):
        async def handler(_method: str, params: dict[str, Any]) -> dict[str, Any]:
            tool_call_id = str(params.get("toolCallId") or "_")
            body = render_plan(params)
            chat = self._app.bot if self._app else None
            if not chat:
                return {"outcome": {"outcome": "rejected", "reason": "no chat"}}
            await chat.send_message(
                chat_id=sess.chat_id, text=body, parse_mode=PARSE_MODE, reply_markup=kb_plan(tool_call_id)
            )
            if self.config.policy.auto_accept_plans:
                return {"outcome": {"outcome": "accepted"}}
            fut = self._pending.register(f"plan:{tool_call_id}")
            try:
                accepted = await asyncio.wait_for(fut, timeout=600)
            except asyncio.TimeoutError:
                accepted = False
            return {"outcome": {"outcome": "accepted" if accepted else "rejected"}}

        return handler

    def _mk_question_handler(self, sess: ChatSession):
        async def handler(_method: str, params: dict[str, Any]) -> dict[str, Any]:
            tool_call_id = str(params.get("toolCallId") or "_")
            body = render_question(params)
            answers: list[dict[str, Any]] = []
            chat = self._app.bot if self._app else None
            if not chat:
                return {"outcome": {"outcome": "skipped"}}
            for q in params.get("questions") or []:
                qid = str(q.get("id") or "")
                opts = q.get("options") or []
                await chat.send_message(
                    chat_id=sess.chat_id,
                    text=body,
                    parse_mode=PARSE_MODE,
                    reply_markup=kb_question(tool_call_id, qid, opts),
                )
                if self.config.policy.auto_answer_questions and opts:
                    chosen = str(opts[0].get("id"))
                else:
                    fut = self._pending.register(f"q:{tool_call_id}:{qid}")
                    try:
                        chosen = await asyncio.wait_for(fut, timeout=300)
                    except asyncio.TimeoutError:
                        chosen = "__skip"
                if chosen == "__skip":
                    return {"outcome": {"outcome": "skipped"}}
                answers.append({"questionId": qid, "selectedOptionIds": [chosen]})
            return {"outcome": {"outcome": "answered", "answers": answers}}

        return handler

    def _mk_todos_handler(self, sess: ChatSession):
        async def handler(_method: str, params: dict[str, Any]) -> None:
            todos = params.get("todos") or []
            if not todos or not self._app:
                return
            body = "<b>Todos</b>\n" + "\n".join(
                f"{self._todo_marker(t.get('status'))} {html_escape(str(t.get('content', '')))}"
                for t in todos
            )
            await self._app.bot.send_message(chat_id=sess.chat_id, text=body, parse_mode=PARSE_MODE)

        return handler

    def _mk_task_handler(self, sess: ChatSession):
        async def handler(_method: str, params: dict[str, Any]) -> None:
            if not self._app:
                return
            await self._app.bot.send_message(
                chat_id=sess.chat_id,
                text=(
                    f"<b>subagent</b> {html_escape(str(params.get('subagentType', 'task')))}: "
                    f"{html_escape(str(params.get('description', '')))}"
                ),
                parse_mode=PARSE_MODE,
            )

        return handler

    def _mk_image_handler(self, sess: ChatSession):
        async def handler(_method: str, params: dict[str, Any]) -> None:
            if not self._app:
                return
            file_path = params.get("filePath")
            description = params.get("description", "image")
            if file_path and Path(file_path).is_file():
                with open(file_path, "rb") as f:
                    await self._app.bot.send_photo(
                        chat_id=sess.chat_id, photo=f, caption=str(description)
                    )
            else:
                await self._app.bot.send_message(
                    chat_id=sess.chat_id,
                    text=f"image generated: {html_escape(str(description))} {html_escape(str(file_path or ''))}",
                    parse_mode=PARSE_MODE,
                )

        return handler

    # ----- shell helpers

    async def _run_agent(self, args: list[str], *, timeout: float = 30) -> tuple[int, str, str]:
        cmd = [self.config.resolved_agent_binary()]
        if self.config.cursor.approve_mcps:
            cmd.append("--approve-mcps")
        cmd.extend(args)
        return await self._run_shell(shlex.join(cmd), cwd=None, timeout=timeout)

    async def _run_shell(
        self, cmdline: str, *, cwd: str | None, timeout: float = 30
    ) -> tuple[int, str, str]:
        loop = asyncio.get_event_loop()

        def runner() -> tuple[int, str, str]:
            try:
                proc = subprocess.run(  # noqa: S602 - intentional shell exec for /run
                    cmdline,
                    shell=True,
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
                return proc.returncode, proc.stdout, proc.stderr
            except subprocess.TimeoutExpired:
                return 124, "", f"timeout after {timeout}s"

        return await loop.run_in_executor(None, runner)

    # ----- output helpers

    async def _send_text_or_doc(
        self,
        update: Update,
        text: str,
        *,
        language: str = "",
        filename: str = "output.txt",
        html: bool = False,
    ) -> None:
        await self._send_text_or_doc_chat(
            update.effective_chat, text, language=language, filename=filename, html=html
        )

    async def _send_text_or_doc_chat(
        self,
        chat,
        text: str,
        *,
        language: str = "",
        filename: str = "output.txt",
        html: bool = False,
    ) -> None:
        if not text:
            return
        if len(text) > self.config.ux.code_as_document_threshold:
            from io import BytesIO

            buf = BytesIO(text.encode("utf-8"))
            buf.name = filename
            await chat.send_document(document=buf, filename=filename)
            return
        if html:
            for part in split_for_telegram(text, self.config.ux.max_message_chars):
                await chat.send_message(part, parse_mode=PARSE_MODE)
            return
        body = code_block(text, language) if not html else text
        for part in split_for_telegram(body, self.config.ux.max_message_chars):
            await chat.send_message(part, parse_mode=PARSE_MODE)

    async def _send_file(self, update: Update, target: Path) -> None:
        await self._send_file_to_chat(update.effective_chat, target)

    async def _send_file_to_chat(self, chat, target: Path, caption: str | None = None) -> None:
        target = target.expanduser().resolve(strict=False)
        if not target.is_file():
            await chat.send_message(f"File not found: <code>{html_escape(str(target))}</code>", parse_mode=PARSE_MODE)
            return
        caption = caption or target.name
        suffix = target.suffix.lower()
        mime, _ = mimetypes.guess_type(str(target))

        with target.open("rb") as f:
            if suffix in {".png", ".jpg", ".jpeg", ".webp"}:
                await chat.send_photo(photo=f, caption=caption)
            elif suffix == ".gif":
                await chat.send_animation(animation=f, caption=caption, filename=target.name)
            elif suffix in {".mp4", ".mov", ".webm", ".mkv"}:
                await chat.send_video(video=f, caption=caption, filename=target.name, supports_streaming=True)
            elif suffix in {".mp3", ".m4a", ".wav", ".ogg", ".flac"}:
                if suffix == ".ogg" and mime == "audio/ogg":
                    await chat.send_voice(voice=f, caption=caption, filename=target.name)
                else:
                    await chat.send_audio(audio=f, caption=caption, filename=target.name)
            else:
                await chat.send_document(document=f, filename=target.name, caption=caption)
